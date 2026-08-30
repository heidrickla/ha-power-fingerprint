"""Work out which circuit each self-metering device sits on.

Devices that meter themselves - smart dimmers, metered outlets, a PDU with
per-outlet reporting - are already labelled by their entity id. Correlating
each one against every circuit finds the circuit it is wired to, which gives
labelled fingerprints for free and makes shared circuits tractable by
subtraction.

    python tools/attribute.py --days 3
    python tools/attribute.py --days 3 --circuit-filter emporiavue

A match must clear BOTH bars: agreement AND containment. Two lamps driven by
one automation agree without sharing a breaker, but a device cannot draw more
than the circuit feeding it, so containment breaks the coincidence.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha

attribution = _ha.load_module("attribution")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    # 12 s matches the Emporia reporting cadence. DO NOT RAISE THIS CASUALLY.
    # At 60 s with one cell of slack, a matching-magnitude step landing anywhere
    # within +/-60 s counted, and on a busy circuit that happens by chance often
    # enough to matter: one circuit collected devices from three unrelated areas
    # (Master Bathroom, Guest Bathroom, Front Porch). At 12 s those became
    # honestly unplaced instead.
    ap.add_argument("--step", type=int, default=12, help="grid seconds")
    ap.add_argument(
        "--circuit-filter",
        default="emporiavue",
        help="substring identifying the circuit sensors",
    )
    ap.add_argument("--min-r", type=float, default=0.5)
    ap.add_argument("--devices", nargs="*", help="limit to these device sensors")
    args = ap.parse_args()

    every = _ha.power_sensors()
    circuits = [
        e
        for e in every
        if args.circuit_filter in e
        and "phase" not in e
        and not e.endswith("total_power")
    ]
    devices = args.devices or [
        e for e in every if args.circuit_filter not in e and "whole_panel" not in e
    ]
    print(
        f"{len(devices)} metered devices vs {len(circuits)} circuits, "
        f"{args.days}d on a {args.step}s grid"
    )

    start, end = _ha.window(args.days)

    def grid(entity):
        return attribution.resample(
            _ha.history(entity, args.days), args.step, start, end
        )

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

    # Self-check only. AREAS ARE REPORTED, NEVER SCORED. Circuit identity is
    # what this tool exists to learn, so feeding names back into the scoring
    # would be circular. Areas are here to make a wrong answer visible - which
    # is exactly how the 60 s grid was caught.
    areas = {}
    for d in dtraces:
        try:
            areas[d] = _ha.template("{{ area_name('" + d + "') }}")
        except Exception:
            areas[d] = ""

    result = attribution.assign(dtraces, ctraces, min_r=args.min_r)
    matched = {k: v for k, v in result.items() if v["circuit"]}

    print(f"=== {len(matched)} of {len(dtraces)} devices placed on a circuit ===")
    for dev, info in sorted(
        matched.items(),
        key=lambda kv: -max(float(kv[1]["step_match"]), float(kv[1]["r"])),
    ):
        # margin 0.0 means there was no runner-up at all, which is the
        # STRONGEST case, not the weakest. Say so rather than printing 0.00.
        m = float(info["margin"])
        mtxt = "sole candidate" if m == 0.0 else f"margin={m:.2f}"
        short = dev.replace("sensor.", "")[:44]
        circ = str(info["circuit"]).replace("sensor.", "")[:32]
        step = float(info["step_match"])
        print(
            f"   {short:<44} -> {circ:<32} step={step:.2f} "
            f"{mtxt:<15} {areas.get(dev, '')}"
        )

    # Devices from one area should land on the same circuit. Two areas sharing
    # a circuit is ordinary; three is a smell.
    per_circuit: dict[str, set[str]] = {}
    for dev, info in matched.items():
        per_circuit.setdefault(str(info["circuit"]), set()).add(areas.get(dev) or "?")
    suspect = {c: a for c, a in per_circuit.items() if len(a) >= 3}
    if suspect:
        print("\n=== WARNING: circuits claiming 3+ areas (likely over-assigned) ===")
        for c, a in suspect.items():
            print(f"   {c.replace('sensor.', '')[:40]:<40} {', '.join(sorted(a))}")

    unplaced = [d for d, v in result.items() if not v["circuit"]]
    if unplaced:
        print()
        print(f"=== {len(unplaced)} not placed ===")
        for d in sorted(unplaced):
            why = result[d].get("reason", "")
            print(f"   {d.replace('sensor.', '')[:52]:<52} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
