"""Work out which circuit each self-metering device sits on.

Devices that meter themselves - smart dimmers, metered outlets, a PDU with
per-outlet reporting - are already labelled by their entity id. Correlating
each one against every circuit finds the circuit it is wired to, which gives
labelled fingerprints for free and makes shared circuits tractable by
subtraction.

    python tools/attribute.py --days 3 --circuit-filter emporiavue

The circuit filter has no default: it is the one thing only you know, and
guessing it wrong returns zero circuits, which reads as "no matches" rather
than as a mistake. Run without it to see the candidates in your own install.

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
analysis = _ha.load_module("analysis")


def _common_prefixes(entities: list[str], top: int = 8) -> list[tuple[str, int]]:
    """The leading words of the visible power sensors, most common first.

    Purely an aid for the "what is my panel called" message - a user should not
    have to go and read their own entity list to answer it.
    """
    counts: dict[str, int] = {}
    for e in entities:
        word = e.split(".", 1)[-1].split("_", 1)[0]
        counts[word] = counts.get(word, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def _measure_step(raw: dict[str, list], fallback: int = 12) -> int:
    """The meter's reporting cadence, rounded to a whole second.

    Takes the median across circuits of each circuit's own median gap, so one
    circuit that dropped out for an hour cannot set the grid for the rest.
    Falls back only when there is nothing to measure - a fallback that fires
    on real data would be hiding an empty history, which the control check
    below reports honestly instead.
    """
    intervals = []
    for rows in raw.values():
        seconds = analysis.sample_interval(rows or [])
        if seconds:
            intervals.append(seconds)
    if not intervals:
        return fallback
    intervals.sort()
    median = intervals[len(intervals) // 2]
    return max(1, round(median))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    # THE GRID MUST BE ABOUT ONE REPORTING INTERVAL WIDE, AND THAT INTERVAL IS
    # A PROPERTY OF YOUR METER. It is measured from the history below rather
    # than assumed, because both errors are real and neither announces itself.
    # Too wide: at 60 s with one cell of slack, a matching-magnitude step
    # landing anywhere within +/-60 s counted, and on a busy circuit that
    # happens by chance often enough that one circuit collected devices from
    # three unrelated areas (Master Bathroom, Guest Bathroom, Front Porch).
    # Too narrow: a genuine coincidence falls between cells and the device goes
    # unplaced. Pass --step only to override the measurement.
    ap.add_argument(
        "--step",
        type=int,
        default=None,
        help="grid seconds (default: measured from the circuit history)",
    )
    ap.add_argument(
        "--circuit-filter",
        default=None,
        help="substring identifying your panel's circuit sensors, e.g. emporiavue",
    )
    ap.add_argument("--min-r", type=float, default=0.5)
    ap.add_argument("--devices", nargs="*", help="limit to these device sensors")
    args = ap.parse_args()

    every = _ha.power_sensors()
    if not args.circuit_filter:
        # No default here on purpose. The old default named one vendor, which
        # silently returned zero circuits on anybody else's panel and read as
        # "no matches" rather than "you have not told me what your panel is".
        print("--circuit-filter is required: which substring names your circuits?")
        print("")
        print(f"{len(every)} power sensors visible. Common leading words:")
        print("")
        for word, n in _common_prefixes(every):
            print(f"   {n:>4}  {word}")
        print("")
        print("e.g. --circuit-filter emporiavue")
        return 2
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
    start, end = _ha.window(args.days)

    # Fetch the circuits first, measure what the meter actually does, and only
    # then choose the grid. The measurement needs real history, so it cannot
    # happen at argument-parsing time.
    raw_circuits = {c: _ha.history(c, args.days) for c in circuits}
    step = args.step or _measure_step(raw_circuits)
    how = "given" if args.step else "measured"
    print(
        f"{len(devices)} metered devices vs {len(circuits)} circuits, "
        f"{args.days}d on a {step}s grid ({how})"
    )

    def grid(entity, rows=None):
        rows = _ha.history(entity, args.days) if rows is None else rows
        return attribution.resample(rows, step, start, end)

    ctraces = {c: grid(c, rows) for c, rows in raw_circuits.items()}
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

    # Reported separately from the unplaced. "I looked and found nothing" and
    # "there was nothing here to look at" are different answers, and lumping a
    # rack PDU in with a genuinely ambiguous light misrepresents both.
    constant = [
        d for d, v in result.items() if str(v.get("reason", "")).startswith("constant")
    ]
    if constant:
        print("")
        print(
            f"=== {len(constant)} devices hold a constant draw and cannot be traced ==="
        )
        print("    No movement to correlate, and containment alone proves nothing.")
        for d in sorted(constant):
            t = dtraces.get(d) or []
            band = f"{min(t):.0f}-{max(t):.0f} W" if t else "-"
            print(f"   {d.replace('sensor.', '')[:52]:<52} {band}")

    unplaced = [
        d
        for d, v in result.items()
        if not v["circuit"] and not str(v.get("reason", "")).startswith("constant")
    ]
    if unplaced:
        print()
        print(f"=== {len(unplaced)} not placed ===")
        for d in sorted(unplaced):
            why = result[d].get("reason", "")
            print(f"   {d.replace('sensor.', '')[:52]:<52} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
