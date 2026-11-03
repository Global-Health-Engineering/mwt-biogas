"""Run the plant controller against the simulator and print a timeline.

    python3 tools/simulate_plant.py            # cold start to fallback treatment
    python3 tools/simulate_plant.py --minutes 20
    python3 tools/simulate_plant.py --historian logs/simulation.sqlite

All plant numbers are PRETEND values (see pasteurizer/simulated_io.py). This
shows controller behaviour only; it says nothing about real treatment.
"""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from scenarios.harness import FAST, Sim  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=8.0)
    parser.add_argument("--bath", type=float, default=40.0, help="initial bath (PT5) in C")
    parser.add_argument("--historian", type=Path, default=None,
                        help="record events and samples into this SQLite file")
    args = parser.parse_args()

    sim = Sim(params=FAST, bath=args.bath, database=args.historian)
    last = None
    print("  time   state               Y3   Y4 %   flow L/min  PT3 C  PT5 C  lights  alarms")
    for _ in range(int(args.minutes * 60 / 0.2)):
        sim.run(0.2)
        st = sim.status
        o = sim.plant.outputs
        key = (st.state, o.pasteurizer_isolation_open, o.control_valve_percent,
               tuple(a.code for a in st.alarms))
        if key != last:
            last = key
            t = sim.clock.monotonic() - 1000.0
            lights = "".join(c if on else "-" for c, on in
                             (("G", st.lights["green"]), ("Y", st.lights["yellow"]),
                              ("R", st.lights["red"])))
            print(f"{t:6.1f}s  {st.state:18s}  {'open' if o.pasteurizer_isolation_open else '  - '}"
                  f"  {o.control_valve_percent:5.1f}  {sim.plant.treatment_flow_l_min:9.2f}"
                  f"  {sim.plant.pt3_c():5.1f}  {sim.plant.bath_c:5.1f}   {lights}   "
                  f"{','.join(a.code for a in st.alarms)}")
    transitions = len(sim.events("STATE_TRANSITION"))
    lost = sim.ctl.shutdown("simulation finished").records_not_stored
    if args.historian is None:
        print(f"\nEvent log: {len(sim.history.events)} events, {len(sim.history.samples)} "
              f"samples ({transitions} state transitions)")
    else:
        print(f"\nHistorian: {args.historian} ({transitions} state transitions this run, "
              f"{lost} records not stored)")


if __name__ == "__main__":
    main()
