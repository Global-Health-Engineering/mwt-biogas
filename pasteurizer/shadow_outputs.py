"""Output writer for shadow runs: live inputs, outputs disabled.

The controller hands every arbitrated frame to this writer as it would to the
hardware writer. The writer keeps the frame in memory and nothing else: this
module imports no hardware library, opens no device, starts no process and
cannot reach a RevPi output.

`outputs_enabled = False` tells the controller that an accepted frame was NOT
written physically. The controller then still runs its supervision on the
command (for example Y4 position timeout, transfer no-flow), but reports
every frame as "not written - outputs disabled": the status has no written
frame, the log records OUTPUT_NOT_WRITTEN instead of OUTPUT_WRITTEN, and the
shutdown result says the safe frame was not written.
"""

from pasteurizer.output_commands import OutputCommands

NOT_WRITTEN_REASON = "OUTPUTS_DISABLED"


class ShadowOutputWriter:
    outputs_enabled = False

    def __init__(self):
        self.last_frame: OutputCommands | None = None   # last command, never written
        self.frames_received = 0

    def write(self, frame: OutputCommands) -> bool:
        """Keep the frame. True means: accepted for the shadow record, not written."""
        if not isinstance(frame, OutputCommands):
            raise TypeError("Expected an OutputCommands instance")
        self.last_frame = frame
        self.frames_received += 1
        return True
