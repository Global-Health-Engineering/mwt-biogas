"""Read-only monitor for verifying the physical I/O mapping."""

import time
from datetime import datetime

import revpimodio2


GROUPS = {
    "DIO inputs": [f"I_{n}" for n in range(1, 15)],
    "DIO counters 1 and 2": ["Counter_1", "Counter_2"],
    "DI inputs": (
        [f"I_{n}_i06" for n in range(1, 15)]
        + ["I_15", "I_16"]
    ),
    "AIO1": [
        "InputValue_1", "InputStatus_1",
        "RTDValue_1", "RTDStatus_1",
        "RTDValue_2", "RTDStatus_2",
    ],
    "AIO2": [
        "cmr361_signal", "cmr361_status",
        "huba691_signal", "huba691_status",
        "RTDValue_1_i04", "RTDStatus_1_i04",
        "RTDValue_2_i04", "RTDStatus_2_i04",
    ],
    "AIO3": [
        "RTDValue_1_i05", "RTDStatus_1_i05",
        "RTDValue_2_i05", "RTDStatus_2_i05",
    ],
}


def main():
    # Uses the current controller configuration, not the saved snapshot.
    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    previous = {}
    try:
        # Resolve every name before starting the monitor.
        channels = {
            group: [(name, rpi.io[name]) for name in names]
            for group, names in GROUPS.items()
        }

        print("READ-ONLY monitor. Raw values; Ctrl+C to stop.")

        while True:
            if not rpi.readprocimg():
                raise RuntimeError("Could not read the process image")

            for group, items in channels.items():
                changes = []
                for name, channel in items:
                    value = channel.value
                    if name not in previous or value != previous[name]:
                        changes.append(f"{name}={value}")
                        previous[name] = value

                if changes:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    print(
                        f"{timestamp} [{group}] " + "  ".join(changes),
                        flush=True,
                    )

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        rpi.cleanup()


if __name__ == "__main__":
    main()