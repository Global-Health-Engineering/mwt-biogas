"""Interactive Y4 movement test. Writes only the mapped AIO command."""

import json
import subprocess
from pathlib import Path

import revpimodio2

ROOT = Path(__file__).resolve().parents[1]


def write_command(name, value):
    subprocess.run(
        ["piTest", "-w", f"{name},{value}"],
        check=True,
    )


def main():
    with open(ROOT / "config/io_mapping.json") as file:
        mapping = json.load(file)["control_valve_Y4"]

    rpi = revpimodio2.RevPiModIO(
        autorefresh=False,
        monitoring=True,
    )

    original = None
    attempted_write = False

    def read_values():
        if not rpi.readprocimg():
            raise RuntimeError("Could not read process image")

        command = rpi.io[mapping["command"]].value
        feedback = rpi.io[mapping["feedback"]].value
        input_status = rpi.io[mapping["feedback_status"]].value
        output_status = rpi.io[mapping["command_status"]].value

        print(
            f"command={command}  feedback={feedback}"
            f"  input_status=0x{input_status:02X}"
            f"  output_status=0x{output_status:02X}",
            flush=True,
        )

        return command, input_status, output_status

    try:
        original, _, _ = read_values()
        if not 4000 <= original <= 20000:
            original = 4000

        print("\nY4 movement test: 4, 8, 12, 16, 20, then 4 mA.")
        print("The original command will be restored on exit.")
        print("Wait for movement to finish before recording each result.")

        input("\nPress Enter to begin, or Ctrl+C to cancel...")

        for milliamps in (4, 8, 12, 16, 20, 4):
            input(f"\nPress Enter to command {milliamps} mA...")
            attempted_write = True
            write_command(mapping["command"], milliamps * 1000)

            input("Wait until the valve settles, then press Enter...")
            command, input_status, output_status = read_values()

            if command != milliamps * 1000:
                raise RuntimeError("Command readback differs from request")

            if input_status or output_status:
                raise RuntimeError(
                    "AIO status indicates an error; stop and inspect"
                )

            observation = input(
                "Physical position / observation, then Enter: "
            )
            print(f"RECORDED: {milliamps} mA | {observation}")

    except (KeyboardInterrupt, EOFError):
        print("\nTest stopped.")
    finally:
        try:
            if attempted_write and original is not None:
                print(f"\nRestoring original command: {original}")
                write_command(mapping["command"], original)
        finally:
            rpi.cleanup()


if __name__ == "__main__":
    main()