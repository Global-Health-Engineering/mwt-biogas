"""Manage GUI logins (run on the RevPi; the users file never leaves it).

    python3 tools/manage_users.py add NAME --role COMMISSIONING
    python3 tools/manage_users.py add NAME --role OPERATOR
    python3 tools/manage_users.py remove NAME
    python3 tools/manage_users.py list

OPERATOR: START/STOP, acknowledge, reset. COMMISSIONING: additionally manual
equipment requests, level overrides and the Y3/Y4 valve test (selector in
MANUAL). The password is asked twice and stored only as a PBKDF2 hash in
secrets/users.json (owner read/write only, not in git). Restart nothing:
the GUI reads the file at every login.
"""

import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.auth import MIN_PASSWORD, ROLES, UserStore  # noqa: E402

DEFAULT_USERS = ROOT / "secrets" / "users.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--users", type=Path, default=DEFAULT_USERS)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a user or change a password/role")
    add.add_argument("name")
    add.add_argument("--role", choices=ROLES, required=True)
    remove = sub.add_parser("remove")
    remove.add_argument("name")
    sub.add_parser("list")
    args = parser.parse_args()
    store = UserStore(args.users)

    if args.command == "list":
        for name, entry in sorted(store.load().items()):
            print(f"{name:20s} {entry['role']}")
    elif args.command == "remove":
        print("Removed" if store.remove_user(args.name) else "No such user")
    else:
        first = getpass.getpass(f"Password for {args.name} (min. {MIN_PASSWORD} characters): ")
        if getpass.getpass("Repeat: ") != first:
            raise SystemExit("Passwords differ; nothing changed")
        try:
            store.set_user(args.name, args.role, first)
        except ValueError as error:
            raise SystemExit(str(error))
        print(f"{args.name} saved as {args.role} in {args.users}")


if __name__ == "__main__":
    main()
