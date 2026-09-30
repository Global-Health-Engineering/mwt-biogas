import subprocess
import time


def set_output(name, value):
    subprocess.run(
        ["piTest", "-w", f"{name},{value}"],
        check=True,
        timeout=5,
    )


observations = []

try:
    for number in range(1, 15):
        name = f"O_{number}"

        command = input(
            f"\n{name}: Enter to pulse for 1 second, "
            "'s' to skip, 'q' to quit: "
        ).strip().lower()

        if command == "q":
            break
        if command == "s":
            observations.append((name, "SKIPPED"))
            continue

        try:
            print(f"{name} ON", flush=True)
            set_output(name, 1)
            time.sleep(1)
        finally:
            set_output(name, 0)
            print(f"{name} OFF", flush=True)

        observed = input(
            "What operated? Enter device/relay name, "
            "or 'unclear': "
        ).strip()

        observations.append((name, observed or "unclear"))

except KeyboardInterrupt:
    print("\nTest interrupted.")

finally:
    print("\n--- OBSERVED MAPPING ---")
    for name, observed in observations:
        print(f"{name}: {observed}")