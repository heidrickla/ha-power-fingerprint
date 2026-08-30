"""Work out which circuit each self-metering device sits on.

Devices that meter themselves - smart dimmers, metered outlets, a PDU with
per-outlet reporting - are already labelled by their entity id. Correlating
each one against every circuit finds the circuit it is wired to, which gives
labelled fingerprints for free and makes shared circuits tractable by
subtraction.

    python tools/attribute.py --days 3
    python tools/attribute.py --days 3 --circuit-filter emporiavue

A match must clear BOTH bars: correlation AND containment. Two lamps driven by
one automation correlate without sharing a breaker, but a device cannot draw
more than the circuit feeding it, so containment breaks the coincidence.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha  # noqa: E402

attribution = _ha.load_module("attribution")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--step", type=int, default=60, help="grid seconds")
    ap.add_argument("--circuit-filter", default="emporiavue",
                    help="substring identifying the circuit sensors")
    ap.add_argument("--min-r", type=float, default=0.5)
    ap.add_argument("--devices", nargs="*", help="limit to these device sensors")
    args = ap.parse_args()

    every = _ha.power_sensors()
    circuits = [
        e for e in every
        if args.circuit_filter in e and "phase" not in e and not e.endswith("total_power")
    ]
    devices = args.devices or [
        e for e in every if args.circuit_filter not in e and "whole_panel" not in e
    ]
    print(f"{len(devices)} metered devices vs {len(circuits)} circuits, "
          f"{args.days}d on a {args.step}s grid")

    start, end = _ha.window(args.days)

    def grid(entity):
        return attribution.resample(_ha.history(entity, args.days), args.step, start, end)

    ctraces = {c: grid(c) for c in circuits}
    # CONTROL: if the circuits came back empty every assignment below would be
    # a false negative rather than a real "no match".
    filled = sum(1 for v in ctraces.values() if v)
    print(f"control: {filled}/{len(ctraces)} circuit traces non-empty")
    if not filled:
        print("ABORT: no circuit history - UNREAD, not 'no matches'.")
        return 2

    dtraces = {}
    for d in devices:
        t = grid(d)
        # A device that never moved carries no information to correlate.
        if t and max(t) - min(t) > 1.0:
            dtraces[d] = t
    print(f"{len(dtraces)} devices actually varied during the window\n")

    result = attribution.assign(dtraces, ctraces, min_r=args.min_r)
    matched = {k: v for k, v in result.items() if v["circuit"]}
    print(f"=== {len(matched)} of {len(dtraces)} devices placed on a circuit ===")
    for dev, info in sorted(matched.items(), key=lambda kv: -max(float(kv[1]["step_match"]), float(kv[1]["r"]))):
        print("   %-52s -> %-38s step=%.2f r=%.2f margin=%.2f" % (
            dev.replace("sensor.", "")[:52],
            str(info["circuit"]).replace("sensor.", "")[:38],
            info["step_match"], info["r"], info["margin"]))

    unplaced = [d for d, v in result.items() if not v["circuit"]]
    if unplaced:
        print(f"\n=== {len(unplaced)} not placed (no circuit cleared both bars) ===")
        for d in sorted(unplaced):
            print("   " + d.replace("sensor.", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
