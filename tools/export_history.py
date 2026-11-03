"""Export the historian database to CSV (read-only; safe while the controller runs).

    python3 tools/export_history.py events  exports/events.csv
    python3 tools/export_history.py samples exports/samples.csv --since 2026-10-01 --until 2026-10-08
    python3 tools/export_history.py events  exports/alarms.csv --kind ALARM_RAISED --kind ALARM_CLEARED

Times are ISO 8601, UTC unless a zone is given. Default database:
logs/historian.sqlite.
"""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pasteurizer.historian import DEFAULT_DATABASE  # noqa: E402
from pasteurizer.historian.csv_export import export_csv  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("table", choices=("events", "samples"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--since", help="ISO time, inclusive")
    parser.add_argument("--until", help="ISO time, exclusive")
    parser.add_argument("--kind", action="append", help="event kind (repeatable)")
    args = parser.parse_args()
    if not args.database.exists():
        sys.exit(f"No historian database at {args.database}")
    rows = export_csv(args.database, args.table, args.output,
                      since=args.since, until=args.until, kinds=args.kind)
    print(f"{rows} rows written to {args.output}")


if __name__ == "__main__":
    main()
