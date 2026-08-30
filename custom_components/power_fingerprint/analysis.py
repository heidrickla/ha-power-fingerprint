"""Signal analysis for appliance power fingerprinting.

Everything here is pure functions over (timestamp, watts) samples so it can be
unit-tested without Home Assistant.

Three design decisions are baked in, each one learned from a real failure while
prototyping this against a live 36-circuit Emporia Vue install:

1.  THRESHOLDS MUST BE PER-CIRCUIT AND MAKE NO DUTY-CYCLE ASSUMPTION.
    A first pass used `idle * 3`, which scored zero runs on two circuits
    drawing 663 W and 353 W continuously - on a high-baseline circuit that
    lands above the 99th percentile. The fix to a fixed percentile was no
    better: the 40th percentile assumes a circuit is mostly off, and on a
    furnace at a 68% duty cycle it lands INSIDE a run, reporting zero runs
    across 98,849 samples. Both failures are silent. Otsu's method is used
    instead because it assumes nothing about how often the load is on.

2.  MINIMUM RUN LENGTH MUST NOT BE A GLOBAL CONSTANT.
    The same pass discarded a garbage disposal with a 423 W peak because it
    used a 2-minute floor and a disposal runs for about twenty seconds. Short
    loads are legitimate appliances, not noise.

3.  THE FLOOR WHILE RUNNING IS A FIRST-CLASS FEATURE.
    A gas dryer and a washing machine on one shared circuit have very similar
    peaks and are not separable on peak, mean or duration. They separate
    immediately on the *minimum* while running: the washer drops to near zero
    between fill, soak and spin, while the dryer holds a steady 350-400 W floor.
    Most published feature sets omit this. It is the single most useful
    discriminator found so far, and it came from the homeowner, not from the
    data.

A note on what this cannot do. At the ~12 s sample interval an Emporia Vue
reports, a 30-second appliance is two or three data points. Kettles, microwaves
and disposals sit at or below the resolution limit and will be detected
unreliably or not at all. That is a property of the meter, not of the code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

Sample = tuple[datetime, float]


def percentile(values: list[float], pct: float) -> float:
    """Plain nearest-rank percentile. No numpy dependency on purpose - this
    runs inside Home Assistant and the input is a few thousand floats."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(len(ordered) * pct / 100.0)
    return ordered[min(idx, len(ordered) - 1)]


def circuit_floor(samples: list[Sample], pct: float = 5.0) -> float:
    """The draw this circuit never goes below - its permanent standby load.

    Deliberately the 5th percentile rather than the minimum: a single dropout
    or a zero reading during a meter restart would drag a true minimum to 0 and
    silently under-report standby.
    """
    return percentile([w for _, w in samples], pct)


def on_threshold(samples: list[Sample], margin_w: float = 15.0) -> float:
    """Where "off" ends and "an appliance is running" begins, for THIS circuit.

    Uses Otsu's method - the split that minimises variance within the two
    resulting groups - rather than any fixed percentile.

    ⛔ A PERCENTILE DOES NOT WORK HERE AND THE FAILURE IS SILENT. An earlier
    version used the 40th percentile as "quiet", which assumes a circuit is off
    most of the time. On a furnace running a 68% duty cycle the 40th percentile
    lands *inside* a run, so the threshold sat above the load and the circuit
    reported ZERO runs across 98,849 samples. Otsu makes no assumption about
    duty cycle, which is exactly the property needed: the same code has to work
    for a dishwasher at 3% and a furnace at 68%.
    """
    values = [w for _, w in samples]
    if not values:
        return margin_w
    lo, hi = min(values), max(values)
    if hi - lo < margin_w:
        # Effectively flat: nothing here is switching on and off.
        return hi + margin_w

    bins = 64
    width = (hi - lo) / bins
    hist = [0] * bins
    for w in values:
        hist[min(int((w - lo) / width), bins - 1)] += 1

    total = len(values)
    best_split, best_var = 0, -1.0
    w_bg = 0
    sum_bg = 0.0
    sum_all = sum(i * hist[i] for i in range(bins))
    for i in range(bins):
        w_bg += hist[i]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / w_bg
        mean_fg = (sum_all - sum_bg) / w_fg
        # Between-class variance; maximising it minimises within-class variance.
        var = w_bg * w_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var, best_split = var, i

    otsu = lo + (best_split + 1) * width
    # Never sit below the circuit's own standby floor, or an always-on circuit
    # would report itself as permanently running.
    return max(otsu, circuit_floor(samples) + margin_w)


@dataclass
class Event:
    """One continuous run of something on one circuit."""

    start: datetime
    end: datetime
    samples: list[float] = field(repr=False, default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def peak_w(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def mean_w(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    @property
    def floor_w(self) -> float:
        """Minimum while running. See design note 3 - this is the feature that
        separates a dryer from a washer on a shared circuit."""
        return min(self.samples) if self.samples else 0.0

    @property
    def energy_wh(self) -> float:
        return self.mean_w * self.duration_s / 3600.0

    def plateaus(self, bucket_w: float = 50.0) -> int:
        """How many distinct power levels this run sits at.

        A two-state device (a furnace blower) shows 2. A multi-stage device (a
        dishwasher moving through fill, heat, wash, drain) shows a dozen or
        more. Cheap, and surprisingly discriminating.
        """
        return len({round(w / bucket_w) for w in self.samples})

    def duty_above(self, watts: float) -> float:
        """Fraction of the run spent above a given level. Distinguishes a load
        that merely touches a peak from one that sustains it."""
        if not self.samples:
            return 0.0
        return sum(1 for w in self.samples if w > watts) / len(self.samples)

    def as_features(self) -> dict[str, float]:
        """The feature vector a matcher compares against learned fingerprints."""
        return {
            "duration_s": round(self.duration_s, 1),
            "peak_w": round(self.peak_w, 1),
            "mean_w": round(self.mean_w, 1),
            "floor_w": round(self.floor_w, 1),
            "energy_wh": round(self.energy_wh, 2),
            "plateaus": self.plateaus(),
            "duty_above_half_peak": round(self.duty_above(self.peak_w / 2), 3),
            "hour_of_day": self.start.hour,
        }


def segment(
    samples: list[Sample],
    threshold_w: float | None = None,
    min_duration_s: float = 30.0,
    gap_tolerance_s: float = 60.0,
) -> list[Event]:
    """Split a circuit's trace into discrete appliance runs.

    `gap_tolerance_s` bridges brief dips below threshold so a washer's soak
    phase does not shatter one wash into fifteen "events". `min_duration_s`
    defaults low enough to keep short loads - see design note 2.

    ⛔ AN EVENT KEEPS EVERY SAMPLE INSIDE ITS TIME SPAN, INCLUDING THE ONES
    BELOW THRESHOLD. This matters more than it looks. If an event held only its
    above-threshold samples, `floor_w` would be pinned at the threshold by
    construction and could never be low - which would destroy the one feature
    that separates a washer from a dryer (design note 3). A test asserts this:
    a washer dipping to 60 W between phases must report a 60 W floor, not a
    threshold-shaped one.
    """
    if not samples:
        return []
    thr = on_threshold(samples) if threshold_w is None else threshold_w

    events: list[Event] = []
    start_i: int | None = None
    last_on_i: int | None = None

    for i, (ts, w) in enumerate(samples):
        if w > thr:
            if start_i is None:
                start_i = i
            last_on_i = i
        elif start_i is not None and last_on_i is not None:
            if (ts - samples[last_on_i][0]).total_seconds() > gap_tolerance_s:
                _emit(events, samples, start_i, last_on_i, min_duration_s)
                start_i, last_on_i = None, None

    if start_i is not None and last_on_i is not None:
        _emit(events, samples, start_i, last_on_i, min_duration_s)
    return events


def _emit(
    events: list[Event],
    samples: list[Sample],
    start_i: int,
    end_i: int,
    min_duration_s: float,
) -> None:
    start, end = samples[start_i][0], samples[end_i][0]
    if (end - start).total_seconds() < min_duration_s:
        return
    # Inclusive slice of the whole span - see the note in segment().
    events.append(Event(start, end, [w for _, w in samples[start_i : end_i + 1]]))


# --------------------------------------------------------------------------
# The three checks below need NO labelled fingerprints. They work on install.
# --------------------------------------------------------------------------


def coverage(mains_w: float, circuit_w: dict[str, float]) -> dict[str, float]:
    """How much of the panel the circuit CTs actually account for.

    On a well-clamped panel the circuits sum to within a few percent of the
    mains. A sudden change means a clamp came off, a CT reversed, or a load
    appeared somewhere unmonitored. This is the cheapest possible watchdog on
    the measurement chain itself, and it is worth having because a CT reading
    zero is otherwise indistinguishable from an appliance that is switched off.
    """
    total = sum(circuit_w.values())
    remainder = mains_w - total
    return {
        "mains_w": round(mains_w, 1),
        "circuits_w": round(total, 1),
        "unmonitored_w": round(remainder, 1),
        "coverage_pct": round(100.0 * total / mains_w, 1) if mains_w else 0.0,
    }


def standby_ranking(
    floors: dict[str, float], price_per_kwh: float
) -> list[dict[str, float | str]]:
    """Rank circuits by permanent draw, with an annual cost.

    Note this reports always-on load, not waste. A server rack sitting at a
    flat 640 W is a legitimate 640 W. Ranking is the useful output; deciding
    what is waste is the owner's call, so this deliberately does not editorialise.
    """
    rows = [
        {
            "circuit": name,
            "floor_w": round(w, 1),
            "annual_kwh": round(w * 8.766, 1),
            "annual_cost": round(w * 8.766 * price_per_kwh, 2),
        }
        for name, w in floors.items()
    ]
    return sorted(rows, key=lambda r: -float(r["floor_w"]))


def contradictions(
    pairs: list[tuple[str, str, bool, float]], min_expected_w: float = 5.0
) -> list[dict[str, object]]:
    """Find switches that claim to be on while their circuit draws nothing.

    `pairs` is (switch_entity, circuit_entity, switch_is_on, circuit_watts).

    Catches a welded or failed relay, a dead lamp, a smart plug that reports
    state without actually switching, and a tripped breaker under a device that
    still shows its last known state. This is "verify by effect" as a
    background service: the state machine says one thing, the current clamp
    says another, and the clamp is the one measuring reality.
    """
    out: list[dict[str, object]] = []
    for switch, circuit, is_on, watts in pairs:
        if is_on and watts < min_expected_w:
            out.append(
                {
                    "switch": switch,
                    "circuit": circuit,
                    "watts": round(watts, 1),
                    "reason": "switch reports on, circuit draws nothing",
                }
            )
    return out


def parse_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse `switch.x: sensor.y` lines into pairs, ignoring blanks and junk.

    Free text rather than a structured selector because a Home Assistant config
    flow has no native pair-list widget, and a malformed line should be skipped
    rather than break setup.
    """
    pairs: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        left, _, right = line.partition(":")
        left, right = left.strip(), right.strip()
        if "." in left and "." in right:
            pairs.append((left, right))
    return pairs
