"""Pull large appliances out of a whole-house meter, with no per-circuit clamps.

⛔ WEAKER THAN THE REST OF THIS PROJECT, ON PURPOSE. With a clamp per breaker
the separation is done in hardware. With one meter every appliance is layered
on one trace and small loads are simply not in it. Measured against a house
that has both: 1000 W and above is recovered 100% of the time from the mains
alone, 300-1000 W about 80%, and below that it falls away fast.

So this finds the dryer, the oven, the air conditioner, the well pump. It does
not find lamps, and it says so rather than inventing them.

    python tools/virtual_circuits.py --days 3
    python tools/virtual_circuits.py --days 3 --validate emporiavue

`--validate` is only possible on a house that ALSO has real clamps: it checks
each inferred appliance against the per-circuit truth, which is the only honest
way to find out whether any of this works.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha

analysis = _ha.load_module("analysis")
fingerprint = _ha.load_module("fingerprint")
virtual = _ha.load_module("virtual")
attribution = _ha.load_module("attribution")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--mains", default="sensor.whole_panel_total_power")
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument(
        "--floor", type=float, default=None, help="override the resolvable floor, W"
    )
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="show the floor curve instead of picking one",
    )
    ap.add_argument(
        "--subtract",
        action="store_true",
        help="remove self-metering devices from the aggregate before inferring",
    )
    ap.add_argument(
        "--validate",
        default=None,
        help="substring naming real circuit sensors, to check the answers against",
    )
    args = ap.parse_args()

    _ha.power_sensors()
    rows = _ha.history(args.mains, args.days)
    if not rows:
        print("ABORT: no mains history - UNREAD, not 'nothing happened'.")
        return 2

    if args.sweep:
        print(
            f"{'floor W':>9} {'steps':>7} {'paired':>7} {'rate':>6} {'shapes':>7}"
            f" {'best match':>11}"
        )
        base = virtual.noise_floor(rows)
        for mult in (1, 2, 3, 5, 7, 10, 14, 20, 28):
            f = base * mult
            found = virtual.steps(rows, f)
            evs, unp = virtual.pair_steps(rows, f)
            rate = (len(found) - len(unp)) / len(found) if found else 0.0
            shapes = "-"
            best = "-"
            if evs:
                feats = [e.as_features() for e in evs]
                lbl = fingerprint.cluster(
                    fingerprint.normalize(feats), threshold=args.threshold
                )
                shapes = str(len(set(lbl)))
                if args.validate:
                    best = f"{100 * _best_match_rate(evs, args, f):.0f}%"
            print(
                f"{f:9.0f} {len(found):7d} {len(evs):7d} "
                f"{rate:6.0%} {shapes:>7} {best:>11}"
            )
        return 0

    # ⭐ Devices that meter themselves are known exactly and need no inference.
    # Taking them out of the aggregate first is the cheapest possible help:
    # every watt subtracted is a watt that can no longer be mistaken for part
    # of something else.
    if args.subtract:
        grid_s = round(analysis.sample_interval(rows) or 6)
        start, end = rows[0][0], rows[-1][0]
        base = attribution.resample(rows, grid_s, start, end)
        known, names = [], []
        for e in _ha.power_sensors():
            if args.validate and args.validate in e:
                continue
            if "whole_panel" in e or "phase" in e or e.endswith("total_power"):
                continue
            t = attribution.resample(_ha.history(e, args.days), grid_s, start, end)
            if t and max(t) - min(t) > 1.0:
                known.append(t)
                names.append(e)
        residual_trace = virtual.residual(rows, known, grid=base)
        removed = sum(base) - sum(residual_trace)
        print(
            f"subtracted {len(names)} self-metering devices "
            f"({removed * grid_s / 3600 / 1000:.1f} kWh of the aggregate)"
        )
        rows = [
            (start + __import__("datetime").timedelta(seconds=grid_s * i), val)
            for i, val in enumerate(residual_trace)
        ]

    floor = args.floor or virtual.resolvable_floor(rows)
    noise = virtual.noise_floor(rows)
    print(f"mains: {len(rows)} samples over {args.days}d")
    print(f"measured noise floor {noise:.0f} W -> resolving loads above {floor:.0f} W")

    events, unpaired = virtual.pair_steps(rows, floor)
    print(f"{len(events)} paired appliance runs, {len(unpaired)} unpaired steps")
    if not events:
        print("nothing above the floor - this meter cannot support virtual circuits")
        return 0

    features = [e.as_features() for e in events]
    labels = fingerprint.cluster(
        fingerprint.normalize(features), threshold=args.threshold
    )
    shapes = fingerprint.summarize("virtual", features, labels)

    print(f"\n=== {len(shapes)} virtual circuits ===")
    for shape, count in zip(
        shapes, Counter(labels).most_common(len(shapes)), strict=False
    ):
        members = [e for e, lb in zip(events, labels, strict=True) if lb == count[0]]
        avg = sum(m.magnitude_w for m in members) / len(members)
        mins = sum(m.duration_s for m in members) / len(members) / 60.0
        print(
            f"  {shape.label:<12} {len(members):3d} runs "
            f"~{avg:6.0f} W  ~{mins:5.1f} min"
        )

        if args.validate:
            # Which real circuit was actually moving during these runs? The
            # answer this whole approach is guessing at.
            # ⛔ COINCIDENCE IS NOT EVIDENCE. The air conditioners here run
            # 42% of the time, so every inferred run overlaps one of them and
            # a plain overlap test scored four different shapes at 85-97%
            # against BOTH. The circuit's own step has to MATCH THE MAGNITUDE
            # of the inferred appliance - the same lesson the active probe
            # taught, for the same reason.
            hits: Counter[str] = Counter()
            for m in members:
                # ⚠ Take the BEST matching overlapping event, not the first
                # one. Breaking on first overlap scored real matches at 6%
                # because a busy circuit's earliest overlapping event is
                # usually the wrong size.
                best_name, best_ratio = None, 0.0
                for circuit in _validation_circuits(args.validate):
                    for ev in _circuit_events(circuit, args.days):
                        if ev.start > m.end or m.start > ev.end:
                            continue
                        size = abs(ev.peak_w - ev.floor_w)
                        if size <= 0:
                            continue
                        ratio = min(size, m.magnitude_w) / max(size, m.magnitude_w)
                        if ratio > best_ratio:
                            best_name, best_ratio = circuit, ratio
                if best_name and best_ratio >= 0.6:
                    hits[best_name] += 1
            for name, n in hits.most_common(2):
                pct = 100 * n / len(members)
                short = name.replace("sensor.", "")[:42]
                print(f"        {pct:3.0f}% match the magnitude of {short}")
    return 0


def _best_match_rate(events: list, args, floor: float) -> float:
    """How often an inferred run matches a real circuit's step in magnitude.

    ⛔ Only meaningful on a house that ALSO has clamps. It is the honest tuning
    signal, and the reason a clamp-less house cannot simply be told its own
    best floor - it has nothing to check against.
    """
    hits = 0
    for m in events:
        best = 0.0
        for circuit in _validation_circuits(args.validate):
            for ev in _circuit_events(circuit, args.days):
                if ev.start > m.end or m.start > ev.end:
                    continue
                size = abs(ev.peak_w - ev.floor_w)
                if size <= 0:
                    continue
                best = max(best, min(size, m.magnitude_w) / max(size, m.magnitude_w))
        if best >= 0.6:
            hits += 1
    return hits / len(events) if events else 0.0


_CIRCUIT_CACHE: dict[str, list] = {}


def _validation_circuits(substr: str) -> list[str]:
    if "list" not in _CIRCUIT_CACHE:
        _CIRCUIT_CACHE["list"] = [
            e
            for e in _ha.power_sensors()
            if substr in e
            and "phase" not in e
            and not e.endswith("total_power")
            and "whole_panel" not in e
        ]
    return _CIRCUIT_CACHE["list"]


def _circuit_events(circuit: str, days: int) -> list:
    if circuit not in _CIRCUIT_CACHE:
        _CIRCUIT_CACHE[circuit] = analysis.segment(_ha.history(circuit, days))
    return _CIRCUIT_CACHE[circuit]


if __name__ == "__main__":
    raise SystemExit(main())
