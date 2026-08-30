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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--mains", default="sensor.whole_panel_total_power")
    ap.add_argument("--threshold", type=float, default=0.9)
    ap.add_argument(
        "--floor", type=float, default=None, help="override the resolvable floor, W"
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
