"""Start the read-only browser GUI (a separate process from the controller).

    python tools/run_gui.py                                  # http://127.0.0.1:8080
    python tools/run_gui.py --database logs/simulation.sqlite
    python tools/run_gui.py --bind 0.0.0.0                   # reachable over Tailscale

It reads the controller's localhost status publisher and the historian
database (read-only). It cannot command the plant, and stopping it does not
affect the controller.
"""

import argparse
from pathlib import Path
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
                             "every network interface, not only Tailscale, with no login")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    link = ControllerLink(args.controller)
    link.start()
    server = GuiServer(link, args.database, host=args.bind, port=args.port)
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: --bind {args.bind} makes this GUI reachable from other computers "
              f"({'every network interface, not only Tailscale' if args.bind in ('0.0.0.0', '::') else 'that interface'}). "
              "There is no login.")
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
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        link.stop()


if __name__ == "__main__":
    main()
