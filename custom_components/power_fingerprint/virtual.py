"""Virtual circuits: separating large appliances without per-circuit clamps.

⛔ THIS IS A DIFFERENT AND WEAKER PROBLEM THAN THE REST OF THIS INTEGRATION,
AND IT MUST NEVER BE PRESENTED AS THE SAME ONE. With a clamp on each breaker
the separation is already done in hardware and the code only has to name what
it sees. With one whole-house meter every appliance is superimposed on a single
trace, and small loads are simply not in it any more.

⭐ MEASURED, NOT ASSUMED. On the development install - which has both a
whole-panel meter and 27 real circuits to check against - every event the real
clamps saw was tested for whether the mains trace showed it at all:

    load step        recovered from the mains alone
    0-25 W                50%          <- and most of that is coincidence
    25-100 W              60%
    100-300 W             70%
    300-1000 W            80%
    1000 W and above     100%

So a virtual circuit is honest for a dryer, an oven, an air conditioner, a well
pump, an EV charger. It is not honest for a lamp, and this module refuses
rather than pretending otherwise.

⛔ `segment()` IS THE WRONG PRIMITIVE HERE AND FAILS QUIETLY. It finds runs by
watching a trace fall back to idle, which a per-circuit trace does and a
whole-house trace never does - the house always draws something. Run it on the
mains and 27 circuits' worth of activity collapses into about ninety enormous
"events" spanning hours. On an aggregate a load is identified by its ON STEP
and the matching OFF STEP later, which is what this module does instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

Sample = tuple[datetime, float]


@dataclass
class VirtualEvent:
    """One appliance's run, inferred from a step up and a matching step down."""

    start: datetime
    end: datetime
    magnitude_w: float
    baseline_w: float

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    def as_features(self) -> dict[str, float]:
        """The same feature names the per-circuit path uses, so a virtual event
        can be clustered and matched by the existing machinery.

        ⚠ `floor_w` is the baseline the appliance sat on, NOT its own minimum -
        on an aggregate the two are not the same thing and cannot be made so.
        """
        return {
            "peak_w": self.baseline_w + self.magnitude_w,
            "floor_w": self.baseline_w,
            "mean_w": self.baseline_w + self.magnitude_w,
            "duration_s": self.duration_s,
            "energy_wh": self.magnitude_w * self.duration_s / 3600.0,
            "plateaus": 1.0,
            "duty_above_half_peak": 1.0,
        }


def noise_floor(samples: list[Sample], percentile: float = 95.0) -> float:
    """How big a step has to be before it stands out from the aggregate.

    The 95th percentile of absolute sample-to-sample movement. Everything
    below this is the house breathing - fridges cycling, lights, chargers -
    and a "virtual circuit" built from it would be noise with a name.

    ⭐ DERIVED FROM THE USER'S OWN TRACE, NOT A CONSTANT. A one-bedroom flat
    and a house with two air conditioners have wildly different floors, and a
    number tuned on one is wrong for the other in a way nothing reports.
    """
    if len(samples) < 3:
        return 0.0
    deltas = sorted(abs(b - a) for (_, a), (_, b) in pairwise(samples))
    idx = min(int(len(deltas) * percentile / 100.0), len(deltas) - 1)
    return deltas[idx]


def pair_rate(samples: list[Sample], floor_w: float) -> float:
    """What fraction of detected level shifts found a partner.

    ⭐ THE SELF-CHECK THAT NEEDS NO GROUND TRUTH. A floor set too low picks up
    the house breathing: shifts appear that never come back down, overlapping
    loads interleave, and the proportion that pair into a clean run collapses.
    Too high and there is nothing to pair. The floor where pairing stays clean
    is the floor where the meter can actually separate appliances - and a house
    with one meter and no clamps can compute this for itself.
    """
    found = steps(samples, floor_w)
    if not found:
        return 0.0
    events, unpaired = pair_steps(samples, floor_w)
    return (len(found) - len(unpaired)) / len(found)


def resolvable_floor(samples: list[Sample], absolute_min_w: float = 20.0) -> float:
    """The smallest load this meter can separate: its own measured noise floor.

    ⛔ DO NOT ADD A MINIMUM CONSTANT. An earlier version clamped this at 300 W,
    taken from one install's recovery curve, and it cost more than half the
    accuracy on that very install. Swept against 27 real clamps, the fraction
    of inferred runs that magnitude-match a real circuit:

        floor 69 W (the measured noise floor)  ->  69%   <- best
        floor 138 W                            ->  52%
        floor 344 W                            ->  29%
        floor 963 W                            ->  19%
        floor 1927 W                           ->   0%

    Higher is worse, monotonically, and the reason is not subtle once seen:
    raising the floor does not filter out noise, it filters out SINGLE
    APPLIANCES. What survives at 2 kW on a house with two air conditioners and
    two furnaces is the moments several of them moved together, and a
    combination matches no individual circuit by construction.

    ⚠ AND DO NOT TUNE THIS BY PAIR RATE. That was the obvious ground-truth-free
    self-check, and on real data it is ANTI-CORRELATED with accuracy: it climbs
    to 97% at the 1927 W floor that scores 0%. Fewer, larger events pair tidily
    with each other while meaning less. `pair_rate` stays as a diagnostic.

    `absolute_min_w` guards only against a dead-flat trace yielding zero.
    """
    return max(noise_floor(samples), absolute_min_w)


def residual(
    mains: list[Sample], known: list[list[float]], grid: list[float] | None = None
) -> list[float]:
    """The aggregate with every self-metered device's own draw taken out.

    ⭐ A DEVICE THAT METERS ITSELF NEEDS NO INFERENCE AT ALL - it is already a
    virtual circuit, exactly known. Its value here is second: removing its
    trace from the aggregate makes everything left over easier to separate.
    Every watt subtracted is a watt that can no longer be mistaken for part of
    something else.

    This is the same subtraction the per-circuit attribution path uses when it
    locks in a confirmed device, for the same reason - a load that has been
    explained must stop being available to explain anything else.
    """
    base = list(grid if grid is not None else [v for _, v in mains])
    for trace in known:
        for i in range(min(len(base), len(trace))):
            base[i] -= trace[i]
    return [max(0.0, v) for v in base]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def steps(
    samples: list[Sample], floor_w: float, window: int = 8
) -> list[tuple[int, float]]:
    """Where the aggregate shifts LEVEL by more than `floor_w`.

    ⛔ NOT THE DIFFERENCE BETWEEN ADJACENT SAMPLES. That was the first version
    and it finds almost nothing, in a way that looks like the house is quiet
    rather than like the detector is wrong.

    Measured on the development install: two air conditioners drawing 2000-2900 W
    produced **two** adjacent-sample steps above 1000 W in two days. A compressor
    does not appear between one six-second sample and the next - it ramps over
    half a minute, so a 2000 W load arrives as five 400 W deltas and no single
    delta clears the bar. Clustering those fragments produced four "virtual
    circuits" averaging 380-530 W that were all the same two air conditioners
    chopped up.

    So compare the median of the `window` samples BEFORE a point against the
    median of the `window` samples AFTER it. A load that takes half a minute to
    come up still shifts the level by its full size, and the medians ignore the
    ramp in between. Only the largest shift in each run of consecutive
    candidates is kept, so one appliance yields one step rather than a smear.
    """
    n = len(samples)
    if n < 2 * window + 1:
        return []
    raw: list[tuple[int, float]] = []
    for i in range(window, n - window):
        before = _median([v for _, v in samples[i - window : i]])
        after = _median([v for _, v in samples[i : i + window]])
        delta = after - before
        if abs(delta) >= floor_w:
            raw.append((i, delta))

    # Collapse each run of adjacent candidates to its single largest shift -
    # otherwise one appliance start is reported `window` times over.
    out: list[tuple[int, float]] = []
    run: list[tuple[int, float]] = []
    for item in raw:
        if run and item[0] == run[-1][0] + 1 and (item[1] > 0) == (run[-1][1] > 0):
            run.append(item)
            continue
        if run:
            out.append(max(run, key=lambda s: abs(s[1])))
        run = [item]
    if run:
        out.append(max(run, key=lambda s: abs(s[1])))
    return out


def pair_steps(
    samples: list[Sample],
    floor_w: float,
    tolerance: float = 0.3,
    max_duration_s: float = 6 * 3600.0,
) -> tuple[list[VirtualEvent], list[tuple[int, float]]]:
    """Match each step up with the step down that ends the same appliance.

    ⭐ MOST RECENT MATCHING UP-STEP FIRST, NOT THE OLDEST. Appliances nest: the
    oven goes on, the kettle goes on and off inside it, then the oven goes off.
    Matching oldest-first pairs the oven's start with the kettle's stop and
    invents a run that never happened. Taking the most recent unmatched up-step
    of the right size handles nesting the way a stack does.

    Magnitude has to match, not just direction. Two unrelated appliances
    switching in the same window is ordinary, and direction alone would pair
    them happily.

    Returns (events, unpaired). ⛔ THE UNPAIRED LIST IS NOT A FAILURE AND MUST
    BE REPORTED. A step with no matching partner means something started and
    did not stop within the window, or two loads overlapped so closely that the
    aggregate never separated them. Silently dropping those turns "I could not
    tell" into "it did not happen".
    """
    open_ups: list[tuple[int, float]] = []
    events: list[VirtualEvent] = []
    used: set[int] = set()

    for idx, delta in steps(samples, floor_w):
        if delta > 0:
            open_ups.append((idx, delta))
            continue
        want = -delta
        for pos in range(len(open_ups) - 1, -1, -1):
            up_idx, up_delta = open_ups[pos]
            ratio = min(want, up_delta) / max(want, up_delta)
            if ratio < 1.0 - tolerance:
                continue
            duration = (samples[idx][0] - samples[up_idx][0]).total_seconds()
            if duration <= 0 or duration > max_duration_s:
                continue
            events.append(
                VirtualEvent(
                    start=samples[up_idx][0],
                    end=samples[idx][0],
                    magnitude_w=(up_delta + want) / 2.0,
                    # What the rest of the house was drawing underneath it.
                    baseline_w=samples[up_idx - 1][1] if up_idx else samples[0][1],
                )
            )
            used.add(up_idx)
            used.add(idx)
            del open_ups[pos]
            break

    unpaired = [s for s in steps(samples, floor_w) if s[0] not in used]
    return sorted(events, key=lambda e: e.start), unpaired
