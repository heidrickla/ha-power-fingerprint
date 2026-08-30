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

A note on what this cannot do. At the ~6 s sample interval measured on the
development install, a 30-second appliance is five data points, and a shorter
one is fewer. Kettles, microwaves and disposals sit at or near the resolution
limit and will be detected unreliably or not at all. That is a property of the
meter, not of the code - and it is why the cadence is measured rather than
assumed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

Sample = tuple[datetime, float]


# ⛔ EVERY THRESHOLD IN THIS MODULE IS DENOMINATED IN WATTS, AND A SENSOR THAT
# REPORTS KILOWATTS FAILS SILENTLY IN THE WORST WAY.
#
# Home Assistant's `device_class: power` says nothing about the unit. Emporia
# Vue, Shelly EM and IotaWatt report W; SolarEdge, Powerwall, many Modbus
# meters and most inverters report kW. Feed kW into `on_threshold` and the
# 15 W margin becomes a 15 kW margin, so nothing is ever above threshold and
# every circuit reports zero runs forever - a clean-looking empty rather than
# an error. The 5 W silent-CT check inverts the same way: a real 4 kW circuit
# arrives as 4.0 and gets reported as a dead clamp.
#
# So units are converted once, at ingestion, and everything downstream may
# assume watts.
_TO_WATTS: dict[str, float] = {
    "W": 1.0,
    "mW": 0.001,
    "kW": 1000.0,
    "MW": 1_000_000.0,
    "GW": 1_000_000_000.0,
    "TW": 1_000_000_000_000.0,
    # Home Assistant's UnitOfPower includes thermal BTU/h, which a heat pump or
    # a boiler integration will genuinely report.
    "BTU/h": 0.29307107,
}


def to_watts(value: float | None, unit: str | None) -> float | None:
    """Convert one reading to watts, or None if the unit is not power.

    A missing unit is treated as watts. That is the pragmatic call rather than
    the pedantic one: template sensors and many custom integrations omit
    `unit_of_measurement` entirely, and refusing them would reject working
    setups, whereas the units that are actually ambiguous - kW against W - are
    both always declared.

    Returns None rather than raising, so one mislabelled sensor drops out of
    the analysis instead of taking the whole coordinator refresh with it.
    """
    if value is None:
        return None
    if unit is None or unit == "":
        return float(value)
    factor = _TO_WATTS.get(unit.strip())
    if factor is None:
        return None
    return float(value) * factor


def sample_interval(samples: list[Sample]) -> float | None:
    """The meter's actual reporting cadence, in seconds - measured, not assumed.

    ⭐ MEASURE THIS RATHER THAN HARDCODING IT. The step-matching window in
    `attribution` has to be about one reporting interval wide: too narrow and
    a real coincidence falls between cells, too wide and chance coincidences
    on a busy circuit start matching. Both failures were observed. An Emporia
    Vue publishes every ~6 s, a Shelly EM every ~1 s, a cloud-polled meter every
    60 s or worse, so a constant that is right for one is wrong for the rest.

    The MEDIAN gap, not the mean: recorder history has gaps where the meter
    dropped out or Home Assistant restarted, and a handful of hour-long holes
    drag a mean far past anything the meter actually does.
    """
    if len(samples) < 3:
        return None
    times = sorted(t for t, _ in samples)
    gaps = [
        (b - a).total_seconds()
        for a, b in pairwise(times)
        if (b - a).total_seconds() > 0
    ]
    if not gaps:
        return None
    ordered = sorted(gaps)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


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


@dataclass
class Cadence:
    """How often one named appliance actually runs, learned from its own runs.

    Not a schedule and not a spec sheet. A fridge that cycles every 40 minutes
    and a dryer used twice a week both get a description of themselves, which
    is the only fair basis for saying one of them has gone quiet.
    """

    runs: int
    median_gap_s: float
    p90_gap_s: float

    def to_dict(self) -> dict[str, float]:
        return {
            "runs": self.runs,
            "median_gap_s": round(self.median_gap_s, 1),
            "p90_gap_s": round(self.p90_gap_s, 1),
        }


def cadence(starts: list[datetime], min_runs: int = 5) -> Cadence | None:
    """Learn an appliance's rhythm from when it ran.

    Returns None below `min_runs`, because four gaps cannot distinguish "runs
    weekly" from "ran four times and stopped", and an absence alert built on
    that would be noise. Refusing to characterise is the correct output here,
    not a wide guess.
    """
    if len(starts) < min_runs:
        return None
    ordered = sorted(starts)
    gaps = [(b - a).total_seconds() for a, b in pairwise(ordered)]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < min_runs - 1:
        return None
    return Cadence(
        runs=len(ordered),
        median_gap_s=percentile(gaps, 50),
        p90_gap_s=percentile(gaps, 90),
    )


def cadences_by_cluster(
    labels: list[int], starts: list[datetime], min_runs: int = 5
) -> dict[int, dict[str, float]]:
    """Learn one rhythm per cluster, given each event's label and start time.

    Lives here rather than in `fingerprint` because `cadence` does, and the
    pure modules deliberately do not import one another - they are loaded by
    path. The HA layer joins the two.
    """
    grouped: dict[int, list[datetime]] = {}
    for label, start in zip(labels, starts, strict=True):
        grouped.setdefault(label, []).append(start)
    out: dict[int, dict[str, float]] = {}
    for label, times in grouped.items():
        rhythm = cadence(times, min_runs=min_runs)
        if rhythm is not None:
            out[label] = rhythm.to_dict()
    return out


def absence(
    cadence_: Cadence | None,
    silent_s: float,
    blind_s: float = 0.0,
    patience: float = 2.0,
) -> tuple[str, str]:
    """Has an expected appliance gone quiet? Returns (state, reason).

    ⭐ EVERY OTHER CHECK IN THIS INTEGRATION ALERTS ON TOO MUCH. The expensive
    failures are silence: the fridge that stopped cycling, the sump pump that
    never ran through a storm, the freezer nobody opened for a fortnight. None
    of those trip a threshold, because nothing exceeded anything.

    ⛔ BLIND TIME IS NOT SILENCE, AND CONFLATING THEM IS THE ONE FAILURE THAT
    MATTERS HERE. If the circuit sensor was unavailable for six hours, the
    appliance may well have run during them. Reporting that as "it has not run"
    is a fabricated observation - the integration would be asserting something
    it did not see. `blind_s` is subtracted from the silence before judging,
    and when too much of the window was blind the answer is `unknown`, which is
    the honest state and not a failure of the check.

    Three states, and only one of them is an alert:
      ok       - ran within the expected window
      overdue  - genuinely silent for `patience` x its own p90 gap
      unknown  - not enough was observed to say either way
    """
    if cadence_ is None:
        return "unknown", "not enough runs to know its rhythm"
    observed = silent_s - blind_s
    if observed < 0:
        observed = 0.0
    limit = cadence_.p90_gap_s * patience
    if blind_s > silent_s * 0.5:
        return "unknown", f"{blind_s / 3600:.1f}h of the window was unobserved"
    if observed > limit:
        return "overdue", (
            f"silent {observed / 3600:.1f}h against a {limit / 3600:.1f}h limit "
            f"({patience:g} x its own p90 gap)"
        )
    return "ok", f"last ran {observed / 3600:.1f}h ago"


@dataclass
class Evidence:
    """A score, what the same score would be by chance, and the gap.

    ⛔ A MATCH RATE WITHOUT ITS CONTROL IS NOT A RESULT, AND THIS PROJECT
    LEARNED THAT THE EXPENSIVE WAY THREE TIMES IN ONE NIGHT:

      * four "virtual circuits" each matched BOTH air conditioners at 85-97%,
        because those run 42% of the time and everything coincides with them;
      * a garage refrigerator scored 91% against the mains - and 78% when its
        runs were shifted three hours into a time they did not happen;
      * an 18-device probe sweep at `probes: 1` returned every placement as
        `measured`, and six of seven collapsed when three probes had to agree.

    Each was caught by a human remembering to check. Nobody installing this has
    that human, so the check belongs here: `lift` is the only number that is
    evidence, and `verdict` refuses to dress a coincidence up as a finding.
    """

    measured: float
    chance: float

    @property
    def lift(self) -> float:
        return self.measured - self.chance

    @property
    def verdict(self) -> str:
        if self.measured < 0.5:
            return "no"
        if self.lift < 0.15:
            return "chance"
        if self.lift < 0.35:
            return "weak"
        return "clear"

    def to_dict(self) -> dict[str, float | str]:
        return {
            "measured": round(self.measured, 3),
            "chance": round(self.chance, 3),
            "lift": round(self.lift, 3),
            "verdict": self.verdict,
        }


def control(
    score: Callable[[float], float],
    offsets_h: tuple[float, ...] = (3.0, 7.0, 13.0, 19.0),
) -> Evidence:
    """Score the real alignment, then score it again at times it did not happen.

    `score` is called with an offset in hours: 0 for the truth, and each of
    `offsets_h` for a world where the same events happened at a different time.
    Whatever still scores is what coincidence alone buys.

    ⭐ SEVERAL OFFSETS, AND THE LOWEST WINS. A three-hour shift still overlaps
    the house's own daily rhythm - on the development install it scored 78%
    against a real 91% - while nineteen hours scored 40%. Taking the minimum
    across a spread is what stops autocorrelation being mistaken for signal.
    """
    measured = score(0.0)
    chances = [score(off) for off in offsets_h]
    return Evidence(measured=measured, chance=min(chances) if chances else 0.0)


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
    rows: list[dict[str, float | str]] = [
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
