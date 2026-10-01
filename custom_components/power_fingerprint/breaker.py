"""Breaker trips: kill a circuit and see what dies.

The only causal test in the integration. Correlation and probes show that two
things move together; cutting the breaker proves a device is fed by that
circuit, because it stops.

The circuit's own clamp identifies which breaker was flipped. If no clamp drops
there is nothing to attribute casualties to, and the result is a refusal rather
than a mapping built from whatever else changed.

`TripDetector` applies this to a live stream of readings. A trip is only ever
opened by one eligible circuit falling to the noise floor: devices dropping on
their own open nothing, whatever their number.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# A circuit is "dead" when it falls below this. Not zero: a CT reads a few
# hundred milliwatts of noise with nothing on it, and demanding exactly zero
# would miss every real kill.
DEAD_W = 2.0

# How far a device's own reading must fall to count as having died. Relative,
# because a 1000 W load dropping to 5 W has plainly died and a 6 W lamp
# dropping to 5 W has not.
DEAD_FRACTION = 0.1

# Another circuit that was live this long before the fall and reads dead by the
# close fell with it. The meter averages, so a cut reaches zero over one or two
# reports (~6 s each on the development meter), and its two units report at
# different offsets.
TOGETHER_S = 30.0

# How long after the circuit comes back a device can still be noticed going.
# Measured on a 7 s flip: a Wi-Fi chime was marked unavailable 7 s after the
# circuit read live again, 20 s after it read dead.
GRACE_S = 30.0

# An outage still dead after this is reported without an end, so a real trip
# is known while the breaker is still off.
CAP_S = 120.0

# A device counts only if it was available for this long before the circuit
# died. Some entities go unavailable every minute or two on their own (radar
# distance sensors with no target), and one of those dropping inside the
# window says nothing about the breaker.
STABLE_S = 300.0

# Eligibility reads this much hourly history, and refuses to judge on less.
LOOKBACK_H = 720
MIN_HISTORY_H = 168


@dataclass
class WalkResult:
    """What one breaker flip proved."""

    circuit: str | None
    circuit_before_w: float
    circuit_after_w: float
    confirmed: list[str] = field(default_factory=list)
    suspected: list[str] = field(default_factory=list)
    unaffected: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "circuit": self.circuit,
            "circuit_before_w": round(self.circuit_before_w, 1),
            "circuit_after_w": round(self.circuit_after_w, 1),
            "confirmed": sorted(self.confirmed),
            "suspected": sorted(self.suspected),
            "unaffected_count": len(self.unaffected),
            "reason": self.reason,
        }


def fallen(
    before: Mapping[str, float],
    after: Mapping[str, float],
    dead_w: float = DEAD_W,
    alive_w: float | None = None,
) -> list[str]:
    """Circuits drawing above `alive_w` before and at `dead_w` or below after."""
    alive = dead_w if alive_w is None else alive_w
    return sorted(
        name
        for name, was in before.items()
        if was > alive and after.get(name, was) <= dead_w
    )


def dead_circuit(
    before: dict[str, float],
    after: dict[str, float],
    dead_w: float = DEAD_W,
    alive_w: float | None = None,
) -> tuple[str | None, str]:
    """Which circuit went dead, working it out rather than being told.

    A circuit qualifies only if it was drawing something beforehand and is at
    the noise floor now - a circuit that was already idle proves nothing by
    staying idle, and treating it as the answer would let a user "identify" a
    breaker they never touched.

    `alive_w` raises what "drawing something" means. A circuit idling at the
    noise floor crosses `dead_w` every few seconds without anything happening;
    measured here, one did so 1,512 times in three days.

    MORE THAN ONE MATCH IS A REFUSAL, NOT A CHOICE. Two circuits going dead
    together means a double-pole breaker, a main, or something else switched at
    the same moment, and picking the larger would be inventing a fact.
    """
    candidates = fallen(before, after, dead_w, alive_w)
    if not candidates:
        return None, (
            "no monitored circuit went dead - either the breaker that was "
            "flipped has no clamp on it, or it was already idle"
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(candidates))
        return None, (
            f"{len(candidates)} circuits went dead together ({names}) - a "
            "double-pole breaker or a main, so which one fed what is not decidable"
        )
    return candidates[0], "the circuit's own clamp confirms it went dead"


def classify_devices(
    before: dict[str, float | None],
    after: dict[str, float | None],
    mesh_routers: frozenset[str] = frozenset(),
    dead_fraction: float = DEAD_FRACTION,
) -> tuple[list[str], list[str], list[str]]:
    """Sort devices into confirmed dead, suspected, and unaffected.

    "WENT UNAVAILABLE" IS WEAKER EVIDENCE THAN "READS ZERO". A device
    reporting 0 W was measured. A device that merely vanished might be on the
    circuit, or might be a Zigbee or Z-Wave node whose PARENT was on the
    circuit - kill one mains powered router and a dozen unrelated battery
    sensors go quiet with it.

    So: a measured collapse is `confirmed`, a disappearance is `suspected`, and
    anything known to route for others stays `suspected` however it looks.
    """
    confirmed: list[str] = []
    suspected: list[str] = []
    unaffected: list[str] = []

    for name, was in before.items():
        now = after.get(name)
        if was is None:
            # Nothing to compare against; its disappearance means little.
            if now is None:
                suspected.append(name)
            else:
                unaffected.append(name)
            continue
        if now is None:
            suspected.append(name)
            continue
        if was <= 0.0:
            unaffected.append(name)
            continue
        if now <= was * dead_fraction:
            (suspected if name in mesh_routers else confirmed).append(name)
        else:
            unaffected.append(name)
    return confirmed, suspected, unaffected


def walk(
    circuits_before: dict[str, float],
    circuits_after: dict[str, float],
    devices_before: dict[str, float | None],
    devices_after: dict[str, float | None],
    mesh_routers: frozenset[str] = frozenset(),
    alive_w: float | None = None,
) -> WalkResult:
    """One breaker flip, start to finish."""
    circuit, reason = dead_circuit(circuits_before, circuits_after, alive_w=alive_w)
    confirmed, suspected, unaffected = classify_devices(
        devices_before, devices_after, mesh_routers
    )
    if circuit is None:
        # Without a confirmed circuit there is nothing to attribute the
        # casualties TO. Report them, attribute none of them.
        return WalkResult(
            circuit=None,
            circuit_before_w=0.0,
            circuit_after_w=0.0,
            confirmed=[],
            suspected=sorted(confirmed + suspected),
            unaffected=unaffected,
            reason=reason,
        )
    return WalkResult(
        circuit=circuit,
        circuit_before_w=circuits_before.get(circuit, 0.0),
        circuit_after_w=circuits_after.get(circuit, 0.0),
        confirmed=confirmed,
        suspected=suspected,
        unaffected=unaffected,
        reason=reason,
    )


def describe(
    circuit: str,
    friendly_name: str,
    now_w: float,
    attributed: list[str],
    standby_w: float = 0.0,
    observed_hours: float = 0.0,
    ever_seen_idle: bool | None = None,
) -> dict[str, object]:
    """What is measurably true about a circuit, for someone about to flip it.

    Reports, does not judge. The person at the panel knows their own house; what
    they cannot see is what the meter recorded, so that is what this returns.

    `standby_w` is the number worth reading twice - a circuit that never falls
    below several hundred watts has something on it that never stops, and that
    holds from the first day without anything being named.
    """
    return {
        "circuit": circuit,
        "name": friendly_name,
        "now_w": round(now_w, 1),
        # Never falls below this. The most informative number here, and the
        # only one that works before anything has been identified.
        "standby_w": round(standby_w, 1),
        "observed_hours": round(observed_hours, 1),
        "ever_seen_idle": ever_seen_idle,
        "known_devices": sorted(attributed),
    }


# --- automatic detection ----------------------------------------------------


@dataclass(frozen=True)
class Eligibility:
    """Whether one circuit is watched for trips, and why."""

    circuit: str
    eligible: bool
    floor_w: float | None
    observed_hours: int
    last_idle: float | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "floor_w": None if self.floor_w is None else round(self.floor_w, 1),
            "observed_hours": self.observed_hours,
            "ever_seen_idle": self.last_idle is not None,
            "reason": self.reason,
        }


def _covered(
    hour: float, trips: Sequence[tuple[float, float | None]], now: float
) -> bool:
    for start, end in trips:
        stop = now if end is None else end
        if hour < stop and hour + 3600.0 > start:
            return True
    return False


def eligibility(
    circuit: str,
    hourly_min: Mapping[float, float],
    trips: Sequence[tuple[float, float | None]],
    floor_w: float,
    now: float,
    lookback_h: int = LOOKBACK_H,
    min_hours: int = MIN_HISTORY_H,
    dead_w: float = DEAD_W,
) -> Eligibility:
    """Whether this circuit going dead can only mean its breaker went.

    A circuit that reaches the noise floor in normal operation - a dedicated
    appliance circuit, whose appliance switching off reads exactly like its
    breaker going - cannot be told apart from a trip, so it is not watched.

    Judged on hourly minima keyed by hour start, which catch every reading
    however brief: a percentile floor would pass a circuit that idles 4% of the
    time. Hours covered by a recorded trip are left out, or a circuit would
    lose its eligibility by tripping once. `floor_w` is the lowest permanent
    draw that stays clear of what an idle circuit reads.
    """
    horizon = now - lookback_h * 3600.0
    judged = {
        hour: low
        for hour, low in hourly_min.items()
        if hour >= horizon and not _covered(hour, trips, now)
    }
    hours = len(judged)
    if hours < min_hours:
        return Eligibility(
            circuit,
            False,
            None,
            hours,
            None,
            f"{hours} hours of history, {min_hours} needed",
        )
    lowest = min(judged.values())
    idle = [hour for hour, low in judged.items() if low <= dead_w]
    if idle:
        return Eligibility(
            circuit,
            False,
            lowest,
            hours,
            max(idle),
            f"reached {lowest:.1f} W in {len(idle)} of {hours} hours",
        )
    if lowest <= floor_w:
        return Eligibility(
            circuit,
            False,
            lowest,
            hours,
            None,
            f"never below {lowest:.1f} W, within the {floor_w:.0f} W an idle "
            "circuit can read",
        )
    return Eligibility(
        circuit,
        True,
        lowest,
        hours,
        None,
        f"never below {lowest:.1f} W in {hours} hours",
    )


TRIP = "trip"
TOGETHER = "together"
LOST = "lost"
UNSEEN = "unseen"

# Records older than this are dropped, keeping the last one before it so the
# state at the edge is still known. Covers the stable period, the longest an
# incident stays open and the together window.
_HORIZON_S = STABLE_S + CAP_S + GRACE_S + TOGETHER_S + 60.0


@dataclass(frozen=True)
class _Record:
    t: float
    was: bool
    now: bool
    watts: float | None
    was_watts: float | None = None


@dataclass
class _Incident:
    circuit: str
    start: float
    last_live: float
    end: float | None = None
    lost: float | None = None


@dataclass(frozen=True)
class TripOutcome:
    """One closed incident: a trip, or the reason it is not one."""

    circuit: str
    start: float
    end: float | None
    last_live: float
    kind: str
    result: WalkResult
    together: list[str]

    @property
    def accepted(self) -> bool:
        return self.kind == TRIP

    @property
    def reason(self) -> str:
        if self.kind == LOST:
            return "the circuit's own reading became unreadable before it came back"
        if self.kind == UNSEEN:
            return "the circuit was not seen drawing power before it went dead"
        return self.result.reason

    def to_dict(self) -> dict[str, object]:
        trip = self.accepted
        return {
            "circuit": self.circuit,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "confirmed": sorted(self.result.confirmed) if trip else [],
            "suspected": sorted(self.result.suspected) if trip else [],
            "together": list(self.together),
            "reason": self.reason,
        }


def _prune_readings(log: deque[tuple[float, float | None]], edge: float) -> None:
    while len(log) >= 2 and log[1][0] <= edge:
        log.popleft()


def _prune_records(log: deque[_Record], edge: float) -> None:
    while len(log) >= 2 and log[1].t <= edge:
        log.popleft()


class TripDetector:
    """Opens an incident when one eligible circuit dies, and judges it on close.

    Fed in time order: circuit readings (`None` when unreadable), and for
    every other entity its availability changes and, for power meters, each
    reading. Nothing opens before `armed_from`, or from a reading that was not
    a number on both sides of the fall.

    An incident closes `GRACE_S` after the circuit reads live again, or
    `CAP_S` after it died if it is still dead. Then every circuit is compared
    across the together window, and every entity across the outage:

    - one circuit fell from above `alive_w` to `DEAD_W` or below: a trip;
    - two or more did: refused, a double-pole breaker or a main;
    - devices count only if available for `STABLE_S` before the circuit's last
      live reading, and dropped between then and the close.
    """

    def __init__(
        self,
        circuits: Iterable[str],
        alive_w: float,
        armed_from: float = 0.0,
        dead_w: float = DEAD_W,
    ) -> None:
        self.circuits = frozenset(circuits)
        self.eligible: frozenset[str] = frozenset()
        self.alive_w = alive_w
        self.dead_w = dead_w
        self.armed_from = armed_from
        self._circuit_log: dict[str, deque[tuple[float, float | None]]] = {}
        self._device_log: dict[str, deque[_Record]] = {}
        self._open: _Incident | None = None

    @property
    def open_circuit(self) -> str | None:
        return self._open.circuit if self._open else None

    @property
    def open_start(self) -> float | None:
        """When the open incident's circuit first read dead."""
        return self._open.start if self._open else None

    def _edge(self, stamp: float) -> float:
        """Oldest time a record is kept for; an open incident's needs come first."""
        edge = stamp - _HORIZON_S
        if self._open is not None:
            edge = min(edge, self._open.last_live - STABLE_S - TOGETHER_S)
        return edge

    def circuit(self, entity: str, watts: float | None, t: float) -> None:
        """One reading of a monitored circuit."""
        log = self._circuit_log.setdefault(entity, deque())
        prev = log[-1] if log else None
        log.append((t, watts))
        _prune_readings(log, self._edge(t))
        incident = self._open
        if incident is not None:
            if entity == incident.circuit and incident.lost is None:
                if watts is None:
                    # Once it has read live again the outage is measured; a
                    # later unreadable report does not undo that.
                    if incident.end is None:
                        incident.lost = t
                elif watts > self.dead_w:
                    if incident.end is None:
                        incident.end = t
                else:
                    incident.end = None
            return
        if (
            t < self.armed_from
            or entity not in self.eligible
            or prev is None
            or prev[1] is None
            or prev[1] <= self.dead_w
            or watts is None
            or watts > self.dead_w
        ):
            return
        self._open = _Incident(entity, t, prev[0])

    def device(
        self,
        entity: str,
        t: float,
        available: bool,
        was_available: bool,
        watts: float | None = None,
        was_watts: float | None = None,
    ) -> None:
        """An availability change, or a power meter's reading.

        `was_watts` is the reading this one replaces, so a meter's draw before
        an outage is known even when its first report arrives inside it.
        """
        log = self._device_log.setdefault(entity, deque())
        log.append(_Record(t, was_available, available, watts, was_watts))
        _prune_records(log, self._edge(t))

    def due(self) -> float | None:
        """When the open incident should be closed, if one is open."""
        incident = self._open
        if incident is None:
            return None
        if incident.lost is not None:
            return incident.lost
        if incident.end is not None:
            return incident.end + GRACE_S
        return incident.start + CAP_S

    def close(self, now: float) -> TripOutcome | None:
        """Judge the open incident once it is due."""
        due = self.due()
        incident = self._open
        if incident is None or due is None or now < due:
            return None
        self._open = None
        edge = incident.start - TOGETHER_S
        before: dict[str, float] = {}
        after: dict[str, float] = {}
        for name, readings in self._circuit_log.items():
            at_edge = _value_at(readings, edge)
            if at_edge is None:
                continue
            lows = [w for t, w in readings if edge < t <= due and w is not None]
            before[name] = at_edge
            after[name] = min(lows) if lows else at_edge
        devices_before: dict[str, float | None] = {}
        devices_after: dict[str, float | None] = {}
        for name, records in self._device_log.items():
            window = [r for r in records if incident.last_live < r.t <= due]
            if not window or not _stable(records, incident.last_live):
                continue
            # None where there was no reading: an entity with no meter, or a
            # meter whose value at that moment is not known.
            reading = _reading_at(records, incident.last_live)
            lows = [r.watts for r in window if r.watts is not None]
            devices_before[name] = reading
            if any(r.was and not r.now for r in window):
                devices_after[name] = None
            elif reading is not None:
                devices_after[name] = min(lows, default=reading)
            else:
                # Still present, with nothing to compare: classify_devices
                # reads a None before and a value after as unaffected.
                devices_after[name] = math.inf
        # One computation decides both the kind and the attribution.
        dead = fallen(before, after, self.dead_w, self.alive_w)
        if incident.lost is not None:
            kind = LOST
        elif dead == [incident.circuit]:
            kind = TRIP
        elif len(dead) > 1:
            kind = TOGETHER
        else:
            kind = UNSEEN
        _, why = dead_circuit(before, after, self.dead_w, self.alive_w)
        confirmed, suspected, unaffected = classify_devices(
            devices_before, devices_after
        )
        if kind != TRIP:
            suspected, confirmed = sorted(confirmed + suspected), []
        result = WalkResult(
            circuit=incident.circuit if kind == TRIP else None,
            circuit_before_w=before.get(incident.circuit, 0.0),
            circuit_after_w=after.get(incident.circuit, 0.0),
            confirmed=confirmed,
            suspected=suspected,
            unaffected=unaffected,
            reason=why,
        )
        return TripOutcome(
            circuit=incident.circuit,
            start=incident.start,
            end=None if incident.lost is not None else incident.end,
            last_live=incident.last_live,
            kind=kind,
            result=result,
            together=[name for name in dead if name != incident.circuit],
        )


def _value_at(
    readings: Iterable[tuple[float, float | None]], stamp: float
) -> float | None:
    """The reading in force at `stamp`, or None if unreadable or unknown."""
    value: float | None = None
    for t, watts in readings:
        if t > stamp:
            break
        value = watts
    return value


def _reading_at(records: Iterable[_Record], stamp: float) -> float | None:
    """A meter's reading at `stamp`, from the record before it or the one after."""
    seen = False
    value: float | None = None
    for record in records:
        if record.t > stamp:
            if not seen:
                value = record.was_watts if record.was else None
            break
        seen = True
        value = record.watts if record.now else None
    return value


def _stable(records: Iterable[_Record], last_live: float) -> bool:
    """Available for all of `STABLE_S` before the circuit's last live reading."""
    start = last_live - STABLE_S
    state: bool | None = None
    for record in records:
        if record.t <= start:
            state = record.now
            continue
        if state is None:
            state = record.was
        if record.t > last_live:
            break
        if record.was != record.now or not record.now:
            return False
    return bool(state)
