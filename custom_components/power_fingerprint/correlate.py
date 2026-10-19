"""Which entities' state changes line up with a shape's runs.

A run starts or stops within seconds of whatever switched it, so the recorder's
state changes are evidence of what a shape is. Each series (one entity entering
one state) is scored against the runs' start and end times and against what the
same series would score by chance. Pure stdlib, loaded by path in the tests.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

SKIP_STATES = frozenset({"unavailable", "unknown", "", "None"})
FIRED = "fired"
CHANGED = "changed"
# An entity with more distinct states than this in the window (an address, a
# media title) is scored as "changed" rather than as one series per state.
MAX_STATES = 12
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T")

# Shifts that keep the series' own rhythm and break its alignment with the runs.
# Hours move it to another part of the day; whole days keep the time of day, so
# a sensor that is busy whenever people are home scores its true chance there.
HOUR_OFFSETS = (3.0, 7.0, 13.0, 19.0)
DAY_OFFSETS = (-3.0, -2.0, -1.0, 1.0, 2.0, 3.0)

# Below this many expected false matches a series is reported as `clear`, up to
# 1 as `weak`; anything larger is what chance alone produces.
CLEAR = 0.01
WEAK = 1.0
MIN_MATCHED = 2

CONTEXT = "context"
SWITCH = "switch"
METER = "meter"
LOAD = "load"
SENSOR = "sensor"
KINDS = (LOAD, SWITCH, SENSOR)

# Binary sensors that report the state of a load rather than of a room.
_LOAD_BINARY = frozenset({"power", "plug", "running", "battery_charging"})
_SWITCH_DOMAINS = frozenset({"light", "switch", "fan"})
_CONTEXT_DOMAINS = frozenset(
    {
        "alarm_control_panel",
        "button",
        "camera",
        "event",
        "input_boolean",
        "input_button",
        "lock",
        "person",
        "scene",
        "script",
    }
)
_CHARGING = frozenset({"charging", "ac", "usb", "wireless"})

# A sensor runs on a battery or a wall adapter; either way it draws a few watts
# at most and cannot be the load behind a run larger than this.
SENSOR_MAX_W = 5.0
# Above this a heating element or motor is the likelier load; it is where to
# look first, not a limit, since a large incandescent installation exceeds it.
HIGH_DRAW_W = 600.0


def transitions(rows: Iterable[tuple[float, str]]) -> dict[str, list[float]]:
    """State changes of one entity, grouped by the state entered.

    `rows` are (timestamp, state) in time order. Changes into or out of
    unavailable and unknown are restarts and dropouts, not events.
    """
    rows = list(rows)
    many = len({s for _, s in rows if s not in SKIP_STATES}) > MAX_STATES
    out: dict[str, list[float]] = {}
    prev: str | None = None
    for ts, state in rows:
        if (
            prev is not None
            and state != prev
            and state not in SKIP_STATES
            and prev not in SKIP_STATES
        ):
            if many:
                key = CHANGED
            elif _TIMESTAMP.match(state):
                key = FIRED
            else:
                key = state
            out.setdefault(key, []).append(ts)
        prev = state
    return out


def power_edges(
    samples: Iterable[tuple[float, float]], threshold_w: float
) -> dict[str, list[float]]:
    """When a device's own power reading crosses `threshold_w`, up and down."""
    out: dict[str, list[float]] = {"on": [], "off": []}
    was: bool | None = None
    for ts, watts in samples:
        now = watts > threshold_w
        if was is not None and now != was:
            out["on" if now else "off"].append(ts)
        was = now
    return {k: v for k, v in out.items() if v}


def hits(times: list[float], edges: list[float], half_s: float) -> int:
    """How many edges have an event in `times` within `half_s` either side."""
    k = 0
    for edge in edges:
        i = bisect.bisect_left(times, edge - half_s)
        if i < len(times) and times[i] <= edge + half_s:
            k += 1
    return k


def _shifted_rate(
    times: list[float],
    edges: list[float],
    half_s: float,
    shift_s: float,
    start: float,
    end: float,
) -> float | None:
    """Hit rate of the edges moved by `shift_s`, over those still in the window."""
    moved = [e + shift_s for e in edges if start <= e + shift_s <= end]
    if not moved:
        return None
    return hits(times, moved, half_s) / len(moved)


def chance(
    times: list[float],
    edges: list[float],
    half_s: float,
    start: float,
    end: float,
) -> float:
    """The hit rate this series would get from timing alone; the highest of three.

    From its own event rate; at the same edges moved by hours (lowest of the
    shifts, as `analysis.control` takes it); and at the same time of day on
    other days (their mean), which is what a sensor that follows the household
    scores.
    """
    span = max(end - start, 1.0)
    base = 1.0 - math.exp(-len(times) / span * 2.0 * half_s)
    hourly = [
        r
        for h in HOUR_OFFSETS
        if (r := _shifted_rate(times, edges, half_s, h * 3600.0, start, end))
        is not None
    ]
    daily = [
        r
        for d in DAY_OFFSETS
        if (r := _shifted_rate(times, edges, half_s, d * 86400.0, start, end))
        is not None
    ]
    return max(
        base,
        min(hourly) if hourly else 0.0,
        sum(daily) / len(daily) if daily else 0.0,
    )


def binomial_tail(n: int, k: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), in log space so large n cannot overflow."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    lp, lq = math.log(p), math.log1p(-p)
    lgn = math.lgamma(n + 1)
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(
            lgn - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        )
    return min(total, 1.0)


@dataclass
class Match:
    """One series lined up against one edge of a shape's runs."""

    entity_id: str
    transition: str
    edge: str
    matched: int
    runs: int
    events: int
    chance: float
    expected_false: float

    @property
    def verdict(self) -> str:
        if self.expected_false < CLEAR and self.matched > MIN_MATCHED:
            return "clear"
        return "weak"

    def to_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "transition": self.transition,
            "edge": self.edge,
            "matched": self.matched,
            "runs": self.runs,
            "events": self.events,
            "chance": round(self.chance, 4),
            "expected_false": float(f"{self.expected_false:.3g}"),
            "verdict": self.verdict,
        }


def score(
    series: dict[tuple[str, str], list[float]],
    starts: list[float],
    ends: list[float],
    half_s: float,
    start: float,
    end: float,
) -> list[Match]:
    """Every series that lines up with the runs better than chance, best first.

    `expected_false` is the binomial tail times the number of tests made, so a
    house with thousands of entities does not report the few that line up by
    luck. A series needs two matched runs and two events of its own.
    """
    n = len(starts)
    if n == 0:
        return []
    tests = max(1, 2 * len(series))
    out: list[Match] = []
    for (entity_id, transition), times in series.items():
        if len(times) < MIN_MATCHED:
            continue
        for edge, points in (("start", starts), ("end", ends)):
            k = hits(times, points, half_s)
            if k < MIN_MATCHED:
                continue
            c = chance(times, points, half_s, start, end)
            expected = binomial_tail(n, k, c) * tests
            if expected <= WEAK:
                out.append(
                    Match(entity_id, transition, edge, k, n, len(times), c, expected)
                )
    out.sort(key=lambda m: (m.expected_false, -m.matched))
    return out


def role(
    domain: str,
    device_class: str | None,
    transition: str,
    battery: bool,
    kind: str | None = None,
) -> str:
    """What a series says about the load: the load itself, a switch for one,
    its meter, or context such as a room's motion.

    `kind` is the device's stored kind (`load`, `switch`, `sensor`) and wins
    over the guess from the domain.
    """
    if kind == SENSOR:
        return CONTEXT
    if domain == "sensor" and transition in ("on", "off") and device_class == "power":
        return METER
    if kind in (LOAD, SWITCH):
        return kind
    if domain in _SWITCH_DOMAINS:
        return SWITCH
    if domain == "binary_sensor":
        return LOAD if device_class in _LOAD_BINARY else CONTEXT
    if domain == "sensor":
        return LOAD if transition in _CHARGING else CONTEXT
    if domain == "device_tracker":
        # A phone moves with a person; a desktop or a TV joins the network when
        # it powers up.
        return CONTEXT if battery else LOAD
    if domain in _CONTEXT_DOMAINS:
        return CONTEXT
    return LOAD


def _ratio(watts: float, draw_w: float) -> str:
    ratio = watts / draw_w
    if ratio < 0.5:
        return "too_small"
    if ratio > 1.5:
        return "too_large"
    return "fits"


def fit(
    series_role: str,
    draw_w: float,
    measured_w: float | None,
    kind: str | None,
    typical_w: float | None,
    max_w: float | None,
    step_w: float | None = None,
) -> str:
    """Whether a device's power can account for a shape drawing `draw_w`.

    The device's own meter decides when it has one; then a stored rating, for a
    load its typical and maximum draw and for a switch its maximum load; then
    `step_w`, the circuit's change at the entity's own events. A switch draws
    whatever is plugged in, so its rating can only rule it out.
    """
    if series_role == CONTEXT:
        return "fits" if draw_w <= SENSOR_MAX_W else "not_a_load"
    if draw_w <= 0:
        return "unknown"
    if measured_w is not None:
        return _ratio(measured_w, draw_w)
    if max_w is not None and draw_w > max_w * 1.25:
        return "too_small"
    if kind != SWITCH and series_role != SWITCH:
        if typical_w is not None and draw_w < typical_w * 0.3:
            return "too_large"
        if max_w is not None or typical_w is not None:
            return "fits"
    if step_w is not None:
        return _ratio(abs(step_w), draw_w)
    return "unknown"


def step_at(
    times: list[float],
    watts: list[float],
    events: list[float],
    settle_s: float = 15.0,
    span_s: float = 60.0,
    limit: int = 200,
) -> float | None:
    """The circuit's median change across a series' events, in watts.

    Before is the minute ending at the event; after is the minute starting
    `settle_s` later, leaving the meter's averaging time to catch up. The
    recorder keeps a reading only when it changes, so an empty minute takes the
    reading in force at its end.
    """
    deltas: list[float] = []
    for t in events[-limit:]:
        b0, b1 = bisect.bisect_left(times, t - span_s), bisect.bisect_right(times, t)
        a0 = bisect.bisect_left(times, t + settle_s)
        a1 = bisect.bisect_right(times, t + settle_s + span_s)
        before = watts[b0:b1] if b1 > b0 else watts[b1 - 1 : b1] if b1 else []
        after = watts[a0:a1] if a1 > a0 else watts[a1 - 1 : a1] if a1 else []
        if before and after:
            deltas.append(median(after) - median(before))
    return round(median(deltas), 1) if deltas else None


def draw_hint(draw_w: float) -> str | None:
    """Where to look first for a shape of this size."""
    if draw_w > HIGH_DRAW_W:
        return "heating element or motor likelier"
    return None
