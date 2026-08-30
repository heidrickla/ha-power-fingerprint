"""Virtual circuits: large appliances inferred from a whole-house meter.

Weaker than the rest of the integration and never to be presented as the same
thing. With a clamp per breaker the separation is done in hardware; with one
meter every appliance is superimposed on a single trace and small loads are not
in it at all. Reliable above roughly a kilowatt, unreliable below - see the
README for the measured recovery rates.

A load is identified by its on-step and matching off-step. `segment()` is not
usable here: it finds runs by watching a trace fall back to idle, and a
whole-house trace never does.
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

         `floor_w` is the baseline the appliance sat on, NOT its own minimum -
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

     DERIVED FROM THE USER'S OWN TRACE, NOT A CONSTANT. A one-bedroom flat
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

     THE SELF-CHECK THAT NEEDS NO GROUND TRUTH. A floor set too low picks up
    the house breathing: shifts appear that never come back down, overlapping
    loads interleave, and the proportion that pair into a clean run collapses.
    Too high and there is nothing to pair. The floor where pairing stays clean
    is the floor where the meter can actually separate appliances - and a house
    with one meter and no clamps can compute this for itself.
    """
    found = steps(samples, floor_w)
    if not found:
        return 0.0
    _events, unpaired = pair_steps(samples, floor_w)
    return (len(found) - len(unpaired)) / len(found)


def resolvable_floor(samples: list[Sample], absolute_min_w: float = 20.0) -> float:
    """The smallest load this meter can separate: its own measured noise floor.

    No minimum constant. A floor taken from one install destroys accuracy on
    another - raising it filters out single appliances, not noise, because what
    survives is the moments several loads moved together.

    Not tuned by pair rate either: that is anti-correlated with accuracy, since
    fewer and larger events pair more tidily while meaning less.
    """
    return max(noise_floor(samples), absolute_min_w)


def residual(
    mains: list[Sample], known: list[list[float]], grid: list[float] | None = None
) -> list[float]:
    """The aggregate with every self-metered device's own draw taken out.

     A DEVICE THAT METERS ITSELF NEEDS NO INFERENCE AT ALL - it is already a
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
    """Where the aggregate shifts level by more than `floor_w`.

    Compares the median of the `window` samples before a point against the
    median after it, not adjacent samples: a compressor ramps over half a
    minute, so a 2 kW load arrives as several small deltas and no single one
    clears the bar. Each run of adjacent candidates collapses to its largest
    shift, so one appliance yields one step.
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

     MOST RECENT MATCHING UP-STEP FIRST, NOT THE OLDEST. Appliances nest: the
    oven goes on, the kettle goes on and off inside it, then the oven goes off.
    Matching oldest-first pairs the oven's start with the kettle's stop and
    invents a run that never happened. Taking the most recent unmatched up-step
    of the right size handles nesting the way a stack does.

    Magnitude has to match, not just direction. Two unrelated appliances
    switching in the same window is ordinary, and direction alone would pair
    them happily.

    Returns (events, unpaired).  THE UNPAIRED LIST IS NOT A FAILURE AND MUST
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
