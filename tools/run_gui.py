"""Start the browser GUI (a separate process from the controller).

    python tools/run_gui.py                                  # http://127.0.0.1:8080
    python tools/run_gui.py --database logs/controller.sqlite
    python tools/run_gui.py --bind 0.0.0.0                   # reachable over Tailscale

It reads the controller's localhost status publisher and the historian
database (read-only). Without a login it is read-only; logged-in users send
commands through the controller's command server, which checks and logs them
again. When a session ends (logout, expiry, GUI stop) the GUI withdraws that
user's manual requests and overrides in the controller; if that message is
lost they expire there anyway. Stopping the GUI never stops the controller.
"""

import argparse
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.link import ControllerLink  # noqa: E402
from gui.server import GuiServer  # noqa: E402
from pasteurizer.publisher import ADDRESS_IN_USE, bind_error_category  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--controller", default="http://127.0.0.1:8081")
    parser.add_argument("--database", type=Path, default=ROOT / "logs" / "simulation.sqlite")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="default 127.0.0.1 (this computer only); 0.0.0.0 listens on "
                             "every network interface, not only Tailscale, without HTTPS")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--users", type=Path, default=ROOT / "secrets" / "users.json",
                        help="logins (tools/manage_users.py); without the file: read-only")
    parser.add_argument("--command-token", type=Path, default=ROOT / "logs" / "command.token",
                        help="written by the running controller")
    args = parser.parse_args()

    link = ControllerLink(args.controller)
    link.start()
    users = args.users if args.users.exists() else None
    server = GuiServer(link, args.database, host=args.bind, port=args.port,
                       users=users, command_token=args.command_token)
    print(f"Logins: {args.users}" if users else
          f"No users file ({args.users}): read-only. Add users with tools/manage_users.py.")
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: --bind {args.bind} makes this GUI reachable from other computers "
              f"({'every network interface, not only Tailscale' if args.bind in ('0.0.0.0', '::') else 'that interface'}). "
              "Logins travel unencrypted (no HTTPS); use the SSH tunnel instead.")
    try:
        server.start()
    except OSError as error:
        link.stop()
        hint = ("Is another GUI already running? Use --port to pick another."
                if bind_error_category(error) == ADDRESS_IN_USE else "")
        raise SystemExit(f"GUI not started: cannot listen on {args.bind}:{args.port} "
                         f"({bind_error_category(error)}, {error}). {hint}")
    print(f"GUI: http://{'127.0.0.1' if args.bind == '0.0.0.0' else args.bind}:{server.port}/ "
          f"(controller {args.controller}, history {args.database}). Ctrl+C stops the GUI only.")
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()           # ends every session: their manual requests are withdrawn
        link.stop()


if __name__ == "__main__":
    main()
