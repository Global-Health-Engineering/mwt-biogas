import subprocess
import time


def set_output(name, value):
    subprocess.run(
        ["piTest", "-w", f"{name},{value}"],
        check=True,
    )


for name, colour in [
    ("O_5", "GREEN"),
    ("O_3", "YELLOW"),
    ("O_4", "RED"),
]:
    input(f"Press Enter to test {colour} LED ({name})...")

    try:
        set_output(name, 1)
        time.sleep(2)
    finally:
        set_output(name, 0)