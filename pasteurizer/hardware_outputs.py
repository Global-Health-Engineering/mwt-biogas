"""Layer 1: write one complete command frame to the RevPi outputs.

The only module (with hardware.py, the reader) that knows RevPi channel
names for outputs. Every frame is written whole: each mapped digital output
and the Y4 analogue command, then one process-image write. Mapping:

    OutputCommands field -> equipment_roles.json command_outputs
                         -> io_mapping.json digital_outputs (O_1..O_14)
    control_valve_percent -> io_mapping.json control_valve_Y4.command
                             (raw = closed + percent x (open - closed))

The writer opens the RevPi with monitoring=False (writing allowed), writes the
all-off frame as soon as it is opened, and again when closed. If the process
dies without closing, the outputs keep their last value (piControl keeps the
process image): the runner's exit handling, tools/safe_outputs.py as systemd
ExecStopPost and, still to be chosen, the RevPi hardware watchdog cover that.
"""

from dataclasses import fields
import threading

from pasteurizer.output_commands import CONTROL_VALVE_COMMAND, OutputCommands

ALL_OFF = OutputCommands()          # every output off/closed, lights off


def output_channels(config) -> dict[str, str]:
    """OutputCommands field -> RevPi digital output channel (O_n)."""
    digital = config.io_mapping["digital_outputs"]
    channels = {}
    for field in fields(OutputCommands):
        if field.name == CONTROL_VALVE_COMMAND:
            continue
        equipment = config.command_outputs[field.name]
        if equipment not in digital:
            raise ValueError(f"{field.name}: {equipment!r} has no digital output in io_mapping.json")
        channels[field.name] = digital[equipment]
    if len(set(channels.values())) != len(channels):
        raise ValueError("Two commands share one digital output")
    return channels


def y4_raw(config, percent: float) -> int:
    valve = config.valve
    return round(valve.command_closed_raw
                 + percent / 100.0 * (valve.command_open_raw - valve.command_closed_raw))


class HardwareOutputWriter:
    outputs_enabled = True

    def __init__(self, config):
        import revpimodio2      # only on the RevPi

        self.config = config
        self.channels = output_channels(config)
        self.y4_channel = config.io_mapping["control_valve_Y4"]["command"]
        self.rpi = revpimodio2.RevPiModIO(autorefresh=False, monitoring=False)
        try:
            self.io = {name: self.rpi.io[name]
                       for name in (*self.channels.values(), self.y4_channel)}
        except Exception:
            self.rpi.cleanup()
            raise
        self.last_frame: OutputCommands | None = None
        self._lock = threading.Lock()      # the watchdog thread may write ALL_OFF
        self.inhibited = False
        if not self.write(ALL_OFF):
            self.rpi.cleanup()
            raise RuntimeError("Could not write the all-off frame at start")

    def write(self, frame: OutputCommands) -> bool:
        """Write the whole frame; True only when the process image write succeeded."""
        if not isinstance(frame, OutputCommands):
            raise TypeError("Expected an OutputCommands instance")
        with self._lock:
            applied = ALL_OFF if self.inhibited else frame
            for command, channel in self.channels.items():
                self.io[channel].value = bool(getattr(applied, command))
            self.io[self.y4_channel].value = y4_raw(self.config, applied.control_valve_percent)
            ok = bool(self.rpi.writeprocimg())
            if ok:
                self.last_frame = applied
            # Inhibited: everything stays off and the frame counts as NOT written.
            return ok and not self.inhibited

    def inhibit(self, lock_timeout_s=0.5) -> bool:
        """Latch every output off (watchdog trip); later frames are not written.

        Called from the watchdog thread. If the scan thread is stuck inside a
        write (holding the lock), the latch is still set at once and the
        all-off write is skipped; returns whether all-off was written now.
        """
        self.inhibited = True               # plain assignment: no lock needed
        if not self._lock.acquire(timeout=lock_timeout_s):
            return False
        try:
            for channel in self.channels.values():
                self.io[channel].value = False
            self.io[self.y4_channel].value = y4_raw(self.config, 0.0)
            ok = bool(self.rpi.writeprocimg())
            if ok:
                self.last_frame = ALL_OFF
            return ok
        except Exception:
            return False
        finally:
            self._lock.release()

    def close(self):
        """All outputs off, then release the RevPi."""
        try:
            self.write(ALL_OFF)
        finally:
            self.rpi.cleanup()
