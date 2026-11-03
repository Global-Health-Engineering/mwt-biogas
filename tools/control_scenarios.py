"""Run the controller scenarios (control spec section 16).

Runs the full scan cycle against the plant simulator with a fake clock.
All plant numbers are PRETEND values; passing these scenarios is not
approval for hardware release.

    python3 tools/control_scenarios.py            # all scenarios
    python3 tools/control_scenarios.py 9 19       # selected numbers
"""

from pathlib import Path
import sys
import traceback

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS.parent))
sys.path.insert(0, str(TOOLS))

from scenarios import SCENARIOS  # noqa: E402


def main(selected):
    failures = 0
    for number in sorted(SCENARIOS):
        if selected and number not in selected:
            continue
        title, func = SCENARIOS[number]
        try:
            func()
            print(f"PASS {number:2d}  {title}")
        except Exception:
            failures += 1
            print(f"FAIL {number:2d}  {title}")
            traceback.print_exc(limit=3)
    print(f"\n{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main({int(a) for a in sys.argv[1:]}) else 0)
