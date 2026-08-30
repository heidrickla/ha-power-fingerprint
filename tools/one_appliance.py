"""Can the mains find ONE named appliance that only a CT can see?

The cleanest possible test of virtual circuits: point it at a circuit carrying
a single unmetered load and ask whether the whole-house trace contains its runs.

⛔ ALWAYS PRINTS THE CHANCE BASELINE. A busy aggregate offers a candidate event
every few minutes, so a short appliance cycle overlaps one constantly. On the
development install a garage refrigerator scored 91% - against a 40% floor that
shifting the same runs in time produced out of nothing at all. A match rate
without its control is not a result.

    python tools/one_appliance.py --circuit sensor.circuit_25_power
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha

analysis = _ha.load_module("analysis")
virtual = _ha.load_module("virtual")

_ap = argparse.ArgumentParser(description=__doc__)
_ap.add_argument("--days", type=int, default=2)
_ap.add_argument("--circuit", required=True, help="a circuit carrying one load")
_ap.add_argument("--mains", default="sensor.whole_panel_total_power")
_args = _ap.parse_args()
DAYS, GARAGE, MAINS = _args.days, _args.circuit, _args.mains
_ha.power_sensors()

rows = _ha.history(GARAGE, DAYS)
events = analysis.segment(rows)
print("=== circuit 25 (Garage), ground truth ===")
print(f"  {len(rows)} samples, {len(events)} runs over {DAYS}d")
sizes = sorted((e.peak_w - e.floor_w) for e in events)
if events:
    durs = sorted(e.duration_s / 60 for e in events)
    print(
        print(
            f"  step size    p25={sizes[len(sizes) // 4]:.0f}  "
            f"median={sizes[len(sizes) // 2]:.0f}  "
            f"p75={sizes[3 * len(sizes) // 4]:.0f} W"
        )
    )
    print(
        print(
            f"  duration     p25={durs[len(durs) // 4]:.0f}   "
            f"median={durs[len(durs) // 2]:.0f}   "
            f"p75={durs[3 * len(durs) // 4]:.0f} min"
        )
    )
    print(
        print(
            f"  floor {analysis.circuit_floor(rows):.0f} W   "
            f"on-threshold {analysis.on_threshold(rows):.0f} W"
        )
    )

mains = _ha.history(MAINS, DAYS)
floor = virtual.resolvable_floor(mains)
inferred, unpaired = virtual.pair_steps(mains, floor)
print()
print(f"=== the mains, floor {floor:.0f} W: {len(inferred)} inferred runs ===")

# Does the mains see the fridge? For each garage run, is there an inferred run
# that overlaps it AND matches its magnitude?
hit = near = 0
for ev in events:
    size = ev.peak_w - ev.floor_w
    if size < floor:
        continue
    near += 1
    for m in inferred:
        if m.start > ev.end or ev.start > m.end:
            continue
        ratio = min(size, m.magnitude_w) / max(size, m.magnitude_w)
        if ratio >= 0.6:
            hit += 1
            break
print(
    f"  of {near} garage runs above the floor, {hit} were found in the mains "
    f"({100 * hit / near if near else 0:.0f}%)"
)
below = len(events) - near
print(
    f"  {below} runs were below the {floor:.0f} W floor and unfindable by construction"
)

# ⛔ CONTROL. 528 inferred runs over 2 days is a lot of candidates, and a
# magnitude-matched overlap can happen by chance. Shift every garage run by
# offsets that preserve its size and duration but destroy its timing: whatever
# still "matches" is what coincidence alone buys.
from datetime import timedelta

print()
print("=== control: the same test with the garage runs shifted in time ===")
for hours in (3, 7, 13, 19):
    chance = 0
    for ev in events:
        size = ev.peak_w - ev.floor_w
        if size < floor:
            continue
        s0, s1 = ev.start + timedelta(hours=hours), ev.end + timedelta(hours=hours)
        for m in inferred:
            if m.start > s1 or s0 > m.end:
                continue
            if min(size, m.magnitude_w) / max(size, m.magnitude_w) >= 0.6:
                chance += 1
                break
    print(
        f"  shifted +{hours:2d}h: {chance:3d}/{near} matched by chance "
        f"({100 * chance / near if near else 0:.0f}%)"
    )
print()
print(f"  real {100 * hit / near:.0f}%  -  chance is the number above.")
print("  The gap between them is the only part that is evidence.")
