"""Switch every mapped RevPi output off (Y4 command closed). Writes outputs.

    python3 tools/safe_outputs.py
    python3 tools/safe_outputs.py --state logs/controller_state.json   # as ExecStopPost

Runs as systemd ExecStopPost (deploy/mwt-biogas-controller.service), which
systemd starts after the controller process has ended for any reason: orderly
stop, error, in-process watchdog, systemd watchdog, kill. It is also the tool
to run by hand after a controller ended outside systemd without its own
shutdown; run by hand it is recovery, not automatic protection.

With --state, and when systemd reports that the service did not end
successfully (SERVICE_RESULT other than "success"), the abnormal stop is
recorded in the state file: the next start latches WATCHDOG_RESTART and
waits for a GUI reset. Without SERVICE_RESULT (run by hand) nothing is marked.

Refuses while a controller holds logs/controller.lock, so it can never fight
a running one.
"""

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.config import load_config  # noqa: E402
from pasteurizer.persistence import mark_abnormal_stop  # noqa: E402
from pasteurizer.run_lock import RunLock  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=ROOT / "logs" / "controller.lock")
    parser.add_argument("--state", type=Path, default=None,
                        help="state file to mark when systemd reports a failed stop")
    args = parser.parse_args()
    lock = RunLock(args.lock)
    if not lock.acquire():
        raise SystemExit("A controller is running (lock held); stop it first. Nothing written.")
    try:
        from pasteurizer.hardware_outputs import HardwareOutputWriter
        writer = HardwareOutputWriter(load_config())    # writes all off when opened
        writer.close()                                  # and again when closed
        print(f"All outputs off: {', '.join(sorted(writer.channels.values()))}, "
              f"{writer.y4_channel} closed", flush=True)
    finally:
        result = os.environ.get("SERVICE_RESULT")
        if args.state is not None and result not in (None, "", "success"):
            print(f"Service ended with {result} (exit {os.environ.get('EXIT_CODE')}"
                  f"/{os.environ.get('EXIT_STATUS')}): abnormal stop "
                  f"{mark_abnormal_stop(args.state)} in {args.state}", flush=True)
        lock.release()


if __name__ == "__main__":
    main()
