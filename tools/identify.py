"""Offline circuit identifier.

Pulls history for each circuit, segments it into appliance runs, clusters the
runs by shape, and prints what it found in plain English so a human can name
them. Optionally writes the named result out as a fingerprint library for the
integration to match against.

    python tools/identify.py --all
    python tools/identify.py --circuits sensor.circuit_21_power --days 7 --out lib.json

History fetching lives in `_ha.py`, including the `end_time` trap - read that
module's docstring before changing how the window is requested.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha

_analysis = _ha.load_module("analysis")
_fp = _ha.load_module("fingerprint")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--circuits", nargs="*", help="entity ids; omit with --all")
    ap.add_argument("--all", action="store_true", help="every power sensor")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-duration", type=float, default=30.0)
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="cluster cut distance in weighted log-power space; lower splits more",
    )
    ap.add_argument("--out", help="write the fingerprint library here")
    args = ap.parse_args()

    circuits = args.circuits or (_ha.power_sensors() if args.all else [])
    if not circuits:
        ap.error("give --circuits or --all")

    # CONTROL: if history comes back empty for everything, the window or the
    # token is wrong and every "no events" below would be a false negative.
    probe = sum(len(_ha.history(c, args.days)) for c in circuits[:3])
    print(f"control: {probe} samples across the first {min(3, len(circuits))} circuits")
    if probe == 0:
        print("ABORT: no history at all - this is UNREAD, not 'nothing ran'.")
        return 2

    library = []
    for entity in circuits:
        samples = _ha.history(entity, args.days)
        events = _analysis.segment(samples, min_duration_s=args.min_duration)
        name = entity.replace("sensor.", "")
        if not events:
            print(f"\n{name}: no runs in {args.days}d ({len(samples)} samples)")
            continue

        rows = [e.as_features() for e in events]
        labels = _fp.cluster(_fp.normalize(rows), threshold=args.threshold)
        fps = _fp.summarize(entity, rows, labels)

        print(f"\n{name}: {len(events)} runs -> {len(fps)} distinct shape(s)")
        for fp in fps:
            share = 100.0 * fp.count / len(events)
            print(f"   [{fp.label}] {fp.count} runs ({share:.0f}%) - {fp.describe()}")
        library.extend(fps)

    if args.out:
        _fp.save_library(library, args.out)
        print(f"\nwrote {len(library)} fingerprints to {args.out}")
        print("Rename each 'unnamed_N' label to the appliance, then feed it back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
