"""Breaker trips, detected as they happen.

Feeds `breaker.TripDetector` every circuit reading and every other entity's
availability changes, judges which circuits are eligible from the recorder's
hourly statistics, and records what a trip proved: one assignment per device,
an event, and the last trip for the entities and diagnostics.

While an incident is open, `probes.OutageRun` asks mains Zigbee and Z-Wave
devices for an answer and watches Z-Wave node status and TVs. What it finds is
added to the trip once power has been back long enough.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .analysis import to_watts
from .breaker import (
    DEAD_W,
    LOOKBACK_H,
    MIN_HISTORY_H,
    STABLE_S,
    Eligibility,
    TripDetector,
    TripOutcome,
    eligibility,
)
from .const import (
    CONF_BREAKER_PROBE,
    DEFAULT_BREAKER_PROBE,
    DOMAIN,
    EVENT_BREAKER_TRIP,
)
from .outage import ZIGBEE, ZIGBEE_COMMAND
from .probes import OutageRun, discover
from .store import BREAKER, CONFIRMED, PROBE
from .verify import INFERRED

if TYPE_CHECKING:
    from .coordinator import FingerprintCoordinator
    from .store import FingerprintStore

_LOGGER = logging.getLogger(__name__)

SUSPECTED = "suspected"
# Recorded on a probe row so it is never read as `verify_circuit`'s answer.
PROBE_KIND = "breaker_reachability"

_CIRCUIT = "circuit"
_METER = "meter"
_DEVICE = "device"

# Hourly minima change once an hour; reading them more often buys nothing.
REFRESH = timedelta(hours=1)

# Attribute lists are written to the state machine on every update.
MAX_SHOWN = 25


def _stamp(value: float | None) -> str | None:
    return None if value is None else dt_util.utc_from_timestamp(value).isoformat()


def _parse(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed.timestamp() if parsed else None


def _available(state: State | None) -> bool:
    """`unknown` is a normal state for many entities (a button never pressed)."""
    return state is not None and state.state != STATE_UNAVAILABLE


class BreakerWatch:
    """One config entry's trip detection."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: FingerprintCoordinator,
        store: FingerprintStore,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store = store
        self._circuits = frozenset(coordinator.circuits)
        self._mains = coordinator.mains
        self.floor_w = float(coordinator.profile["breaker_floor_w"])
        # Nothing opens until Home Assistant has started and every entity has
        # been watched for the stable period: startup is when entities flap.
        self.detector = TripDetector(
            coordinator.circuits, alive_w=self.floor_w, armed_from=math.inf
        )
        self._hourly: dict[str, dict[float, float]] = {c: {} for c in self._circuits}
        self._verdicts: dict[str, Eligibility] = {}
        self._kinds: dict[str, str | None] = {}
        self._excluded_devices: set[str] = set()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None
        self._timer_due: float | None = None
        self.listening_since: datetime | None = None
        self.armed_at: datetime | None = None
        self.last_refusal: dict[str, Any] | None = None
        self._stats_failed = False
        self._no_stats: set[str] = set()
        # Circuit -> start of its trip recorded while still dead.
        self._open_trips: dict[str, str] = {}
        options = {**entry.data, **entry.options}
        self._probe = bool(options.get(CONF_BREAKER_PROBE, DEFAULT_BREAKER_PROBE))
        # One incident's outage evidence at a time.
        self._outage: OutageRun | None = None

    # --- lifecycle -------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe; arm once Home Assistant has started."""
        now = dt_util.utcnow()
        self.listening_since = now
        registry = er.async_get(self.hass)
        # The meter's own other sensors (phases, totals) are not devices fed
        # by a circuit, and must not count as casualties.
        for entity in (*self._circuits, self._mains):
            row = registry.async_get(entity)
            if row is not None and row.device_id:
                self._excluded_devices.add(row.device_id)
        for circuit in sorted(self._circuits):
            state = self.hass.states.get(circuit)
            if state is not None:
                watts = self._watts(state)
                self.detector.circuit(circuit, watts, state.last_reported_timestamp)
                self._note_hour(circuit, watts, state.last_reported_timestamp)
        # A trip recorded while its circuit was still dead ends at the
        # circuit's next live reading, including one after a restart.
        self._open_trips = {
            str(trip["circuit"]): str(trip["start"])
            for trip in self._store.breaker_trips()
            if trip.get("end") is None and trip.get("circuit") and trip.get("start")
        }
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)
        )
        self._unsubs.append(async_at_started(self.hass, self._on_started))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_refresh, REFRESH)
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_timer()
        if self._outage is not None:
            self._outage.stop()
        self._outage = None

    @callback
    def _on_started(self, _hass: HomeAssistant) -> None:
        start = max(dt_util.utcnow(), self.listening_since or dt_util.utcnow())
        self.armed_at = start + timedelta(seconds=STABLE_S)
        self.detector.armed_from = self.armed_at.timestamp()

    @callback
    def _on_refresh(self, _now: datetime) -> None:
        self._entry.async_create_background_task(
            self.hass, self.async_refresh_eligibility(), f"{DOMAIN} breaker eligibility"
        )

    # --- eligibility -----------------------------------------------------

    async def async_refresh_eligibility(self) -> None:
        """Read hourly minima from the recorder, then judge every circuit.

        Statistics rather than states: 720 rows per circuit instead of about
        400,000, and the minimum still covers every reading.
        """
        now = dt_util.utcnow()
        start = now - timedelta(hours=LOOKBACK_H)
        try:
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.helpers.recorder import get_instance

            instance = get_instance(self.hass)
            rows = await instance.async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                now,
                set(self._circuits),
                "hour",
                {"power": UnitOfPower.WATT},
                {"min"},
            )
        except Exception as err:
            # Without history nothing can be judged, so nothing is eligible:
            # the live readings fill the hours from here on. Said once.
            if not self._stats_failed:
                _LOGGER.warning(
                    "Could not read circuit statistics (%s) - breaker trips are "
                    "detected only on circuits with %d hours of live readings",
                    err,
                    MIN_HISTORY_H,
                )
            self._stats_failed = True
            rows = {}
        else:
            if self._stats_failed:
                _LOGGER.info(
                    "Circuit statistics readable again - breaker trips are "
                    "judged on recorded history"
                )
            self._stats_failed = False
            self._no_stats = {c for c in self._circuits if not rows.get(c)}
        for circuit, stats in rows.items():
            bucket = self._hourly.setdefault(circuit, {})
            for row in stats:
                low = row.get("min")
                begin = row.get("start")
                if low is None or begin is None:
                    continue
                hour = float(begin)
                bucket[hour] = min(bucket.get(hour, math.inf), float(low))
        edge = now.timestamp() - LOOKBACK_H * 3600.0
        for bucket in self._hourly.values():
            for hour in [h for h in bucket if h < edge]:
                del bucket[hour]
        self._rejudge()
        self._coordinator.async_update_listeners()

    def _trips_on(self, circuit: str) -> list[tuple[float, float | None]]:
        """Trips that excuse this circuit's falls to the floor.

        Only a trip that took devices with it. A fall that dropped nothing is
        what an appliance switching off looks like, so it counts against the
        circuit like any other: otherwise a circuit that has started idling
        at the floor would record every idle as a trip and stay watched.
        """
        out: list[tuple[float, float | None]] = []
        for trip in self._store.breaker_trips():
            if trip.get("circuit") != circuit:
                continue
            if not (trip.get("confirmed") or trip.get("suspected")):
                continue
            start = _parse(trip.get("start"))
            if start is not None:
                out.append((start, _parse(trip.get("end"))))
        return out

    @callback
    def _rejudge(self) -> None:
        now = dt_util.utcnow().timestamp()
        verdicts: dict[str, Eligibility] = {}
        for circuit in sorted(self._circuits):
            verdict = eligibility(
                circuit,
                self._hourly.get(circuit, {}),
                self._trips_on(circuit),
                self.floor_w,
                now,
            )
            if circuit in self._no_stats and verdict.floor_w is None:
                verdict = replace(
                    verdict,
                    reason=f"no long-term statistics (a circuit sensor needs "
                    f"state_class measurement); {verdict.reason}",
                )
            verdicts[circuit] = verdict
        self._verdicts = verdicts
        self.detector.eligible = frozenset(
            c for c, verdict in self._verdicts.items() if verdict.eligible
        )

    @property
    def eligible(self) -> list[str]:
        return sorted(self.detector.eligible)

    @property
    def verdicts(self) -> dict[str, Eligibility]:
        return dict(self._verdicts)

    # --- the stream ------------------------------------------------------

    def _watts(self, state: State | None) -> float | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return None
        # "nan" parses, and compares false both ways: it would pass for dead.
        if not math.isfinite(value):
            return None
        return to_watts(value, state.attributes.get("unit_of_measurement"))

    def _kind(self, entity: str, state: State | None) -> str | None:
        if entity in self._circuits:
            return _CIRCUIT
        if entity in self._kinds:
            return self._kinds[entity]
        kind = self._classify(entity, state)
        self._kinds[entity] = kind
        return kind

    def _classify(self, entity: str, state: State | None) -> str | None:
        """What an entity is to the detector; None for what it ignores."""
        if entity == self._mains:
            return None
        row = er.async_get(self.hass).async_get(entity)
        device_class: object = None
        if row is not None:
            if row.config_entry_id == self._entry.entry_id or row.platform == DOMAIN:
                return None
            if row.device_id and row.device_id in self._excluded_devices:
                return None
            device_class = row.device_class or row.original_device_class
        if device_class is None and state is not None:
            device_class = state.attributes.get("device_class")
        if entity.startswith("sensor.") and device_class == "power":
            return _METER
        return _DEVICE

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity = event.data["entity_id"]
        new = event.data["new_state"]
        if new is None:
            return  # removed, not unavailable
        if (
            self._outage is not None
            and not self._outage.finished
            and entity in self._outage.watching
        ):
            self._outage.feed(entity, new)
        kind = self._kind(entity, new)
        if kind is None:
            return
        old = event.data["old_state"]
        stamp = new.last_updated_timestamp
        # An entity appearing is not one coming back.
        was = _available(old) if old is not None else _available(new)
        if kind == _CIRCUIT:
            # An unchanged value is reported without an event; its last report
            # is the circuit's last live reading, not its first.
            if old is not None and old.last_reported_timestamp > (
                old.last_updated_timestamp
            ):
                self.detector.circuit(
                    entity, self._watts(old), old.last_reported_timestamp
                )
            watts = self._watts(new)
            self.detector.circuit(entity, watts, stamp)
            self._note_hour(entity, watts, stamp)
            self._follow_outage(entity, watts, stamp)
            if watts is not None and watts > DEAD_W and entity in self._open_trips:
                start = self._open_trips.pop(entity)
                self._entry.async_create_background_task(
                    self.hass,
                    self._async_end_trip(entity, start, stamp),
                    f"{DOMAIN} breaker trip end",
                )
        elif kind == _METER:
            self.detector.device(
                entity,
                stamp,
                _available(new),
                was,
                self._watts(new),
                self._watts(old),
            )
        elif was != _available(new):
            self.detector.device(entity, stamp, _available(new), was)
        else:
            return
        self._schedule()

    @callback
    def _follow_outage(self, entity: str, watts: float | None, stamp: float) -> None:
        """Start an incident's outage run, and tell it when its circuit is back."""
        run = self._outage
        if run is not None and run.finished:
            run = self._outage = None
        if run is not None:
            if entity == run.circuit and watts is not None and watts > DEAD_W:
                run.restore(stamp)
            return
        start = self.detector.open_start
        if start is None or self.detector.open_circuit != entity:
            return
        run = OutageRun(
            self.hass,
            self._entry,
            entity,
            start,
            discover(self.hass, self._excluded_devices),
            self._probe,
            self._async_record_run,
        )
        self._outage = run
        run.start()

    def _note_hour(self, circuit: str, watts: float | None, stamp: float) -> None:
        """Keep the hour's lowest reading, for the hours statistics lack yet."""
        if watts is None:
            return
        hour = math.floor(stamp / 3600.0) * 3600.0
        bucket = self._hourly.setdefault(circuit, {})
        bucket[hour] = min(bucket.get(hour, math.inf), watts)

    async def _async_end_trip(self, circuit: str, start: str, stamp: float) -> None:
        await self._store.async_end_trip(circuit, start, _stamp(stamp) or "")
        self._rejudge()
        self._coordinator.async_update_listeners()

    @callback
    def _schedule(self) -> None:
        due = self.detector.due()
        if due is None:
            self._cancel_timer()
            return
        if self._timer is not None and self._timer_due is not None:
            if self._timer_due <= due:
                return
            self._cancel_timer()
        delay = max(0.0, due - dt_util.utcnow().timestamp())
        self._timer_due = due
        self._timer = async_call_later(self.hass, delay, self._on_due)

    @callback
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
        self._timer = None
        self._timer_due = None

    @callback
    def _on_due(self, _now: datetime) -> None:
        self._timer = None
        self._timer_due = None
        outcome = self.detector.close(dt_util.utcnow().timestamp())
        if outcome is None:
            self._schedule()
            return
        if outcome.accepted and outcome.end is None:
            self._open_trips[outcome.circuit] = _stamp(outcome.start) or ""
        self._entry.async_create_background_task(
            self.hass, self.async_handle(outcome), f"{DOMAIN} breaker trip"
        )

    # --- what a trip proved ------------------------------------------------

    async def async_handle(self, outcome: TripOutcome) -> None:
        """Record a trip, or note why an incident was not one."""
        run = self._outage
        if run is not None and (run.circuit, run.fall) != (
            outcome.circuit,
            outcome.start,
        ):
            run = None
        if not outcome.accepted:
            if run is not None:
                run.decide(None)
            self.last_refusal = {
                "circuit": outcome.circuit,
                "start": _stamp(outcome.start),
                "end": _stamp(outcome.end),
                "kind": outcome.kind,
                "together": list(outcome.together),
                "reason": outcome.reason,
            }
            _LOGGER.info(
                "%s went dead and is not recorded as a trip: %s",
                outcome.circuit,
                outcome.reason,
            )
            self._coordinator.async_update_listeners()
            return
        confirmed = sorted(outcome.result.confirmed)
        suspected = sorted(outcome.result.suspected)
        trip = {
            "circuit": outcome.circuit,
            "start": _stamp(outcome.start),
            "end": _stamp(outcome.end),
            "confirmed": confirmed,
            "suspected": suspected,
        }
        keep_after = _stamp(dt_util.utcnow().timestamp() - LOOKBACK_H * 3600.0)
        await self._store.async_record_trip(trip, keep_after or "")
        await self._record_devices(outcome.circuit, trip, confirmed, suspected)
        if run is not None:
            run.decide(dict(trip))
        self.hass.bus.async_fire(EVENT_BREAKER_TRIP, dict(trip))
        _LOGGER.info(
            "Breaker trip on %s: %d confirmed, %d suspected",
            outcome.circuit,
            len(confirmed),
            len(suspected),
        )
        self._rejudge()
        self._coordinator.async_update_listeners()

    async def _record_devices(
        self,
        circuit: str,
        trip: dict[str, Any],
        confirmed: list[str],
        suspected: list[str],
    ) -> None:
        """One assignment per device, never over a probe's answer.

        A bed publishes thirty entities; recorded per entity it would get
        thirty Circuit entities on its page. The row goes under the key the
        device already has an assignment on, else its own power sensor, else
        its first casualty, so the device keeps one.
        """
        registry = er.async_get(self.hass)
        groups: dict[str, list[str]] = {}
        devices: dict[str, str | None] = {}
        for entity in [*confirmed, *suspected]:
            row = registry.async_get(entity)
            device = row.device_id if row is not None else None
            group = device or entity
            groups.setdefault(group, []).append(entity)
            devices[group] = device
        existing = self._store.assignments()
        for group, entities in sorted(groups.items()):
            key = self._key_for(registry, devices[group], entities, existing)
            await self._store.async_record_assignment(
                key,
                circuit,
                INFERRED,
                BREAKER,
                {
                    "start": trip["start"],
                    "end": trip["end"],
                    "result": CONFIRMED
                    if any(e in confirmed for e in entities)
                    else SUSPECTED,
                    "entities": sorted(entities),
                },
            )

    async def _async_record_run(self, run: OutageRun) -> None:
        trip = run.trip or {}
        verdict = run.evidence.judge()
        await self.async_record_outage(
            run.circuit,
            str(trip.get("start")),
            verdict.casualties,
            verdict.answered,
            verdict.indeterminate,
        )

    async def async_record_outage(
        self,
        circuit: str,
        start: str,
        casualties: dict[str, dict[str, Any]],
        answered: list[str],
        indeterminate: list[str],
        recovered: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Add an outage's evidence to a recorded trip; the trip, or None if absent.

        Keys are device ids, or entity ids for entities without a device. Each
        casualty is `suspected`: a device that stops answering may only have
        lost its parent router. A Zigbee answer is stored as a probe and the
        rest as breaker evidence. `recovered` devices refused requests before
        the outage and answered after it: noted on the trip, never assigned.
        """
        trip = next(
            (
                t
                for t in self._store.breaker_trips()
                if t.get("circuit") == circuit and t.get("start") == start
            ),
            None,
        )
        if trip is None or not (casualties or answered or indeterminate or recovered):
            return trip
        registry = er.async_get(self.hass)
        existing = self._store.assignments()

        def key(device: str) -> tuple[str, list[str]]:
            rows = er.async_entries_for_device(registry, device)
            # A node's ping button and status sensor are not what it is.
            primary = [r.entity_id for r in rows if r.entity_category is None]
            entities = sorted(primary or [r.entity_id for r in rows])
            if not entities:
                return device, [device]
            return self._key_for(registry, device, entities, existing), entities

        def stamped(found: dict[str, Any]) -> dict[str, Any]:
            return {
                k: _stamp(v) if k.endswith("_at") and isinstance(v, float) else v
                for k, v in found.items()
            }

        shown: dict[str, dict[str, Any]] = {}
        for device, raw in sorted(casualties.items()):
            found = stamped(raw)
            name, entities = key(device)
            active = found.get("signal") in (ZIGBEE, ZIGBEE_COMMAND)
            evidence: dict[str, Any] = {
                "start": start,
                "result": SUSPECTED,
                "entities": entities[:MAX_SHOWN],
                **found,
            }
            if active:
                evidence["probe_kind"] = PROBE_KIND
            await self._store.async_record_assignment(
                name, circuit, INFERRED, PROBE if active else BREAKER, evidence
            )
            shown[name] = dict(found)
        amended = await self._store.async_amend_trip(
            circuit,
            start,
            sorted(shown),
            {
                "casualties": shown,
                "answered": sorted(key(d)[0] for d in answered),
                "indeterminate": sorted(key(d)[0] for d in indeterminate),
                "recovered": {
                    key(d)[0]: stamped(v) for d, v in sorted((recovered or {}).items())
                },
            },
        )
        if amended is not None and shown:
            self.hass.bus.async_fire(EVENT_BREAKER_TRIP, {**amended, "amended": True})
            _LOGGER.info(
                "Breaker trip on %s: %d more suspected from its outage",
                circuit,
                len(shown),
            )
        self._rejudge()
        self._coordinator.async_update_listeners()
        return amended

    @staticmethod
    def _key_for(
        registry: er.EntityRegistry,
        device: str | None,
        entities: list[str],
        existing: dict[str, dict[str, Any]],
    ) -> str:
        if device is None:
            return sorted(entities)[0]
        siblings = sorted(
            row.entity_id
            for row in er.async_entries_for_device(
                registry, device, include_disabled_entities=True
            )
        )
        held = [e for e in siblings if e in existing]
        if held:
            return held[0]
        meters = [
            e
            for e in siblings
            if e.startswith("sensor.")
            and (
                (row := registry.async_get(e)) is not None
                and (row.device_class or row.original_device_class) == "power"
            )
        ]
        if meters:
            return meters[0]
        return sorted(entities)[0]

    # --- reporting -------------------------------------------------------

    def last_trip(self) -> dict[str, Any] | None:
        trips = self._store.breaker_trips()
        return trips[-1] if trips else None

    def attributes(self) -> dict[str, Any]:
        """For the unmonitored load sensor: eligible circuits and the last trip."""
        out: dict[str, Any] = {"breaker_trip_circuits": self.eligible}
        trip = self.last_trip()
        if trip is None:
            out["last_breaker_trip"] = None
            return out
        shown = dict(trip)
        for key in (CONFIRMED, SUSPECTED):
            rows = list(trip.get(key) or [])
            shown[key] = rows[:MAX_SHOWN]
            if len(rows) > MAX_SHOWN:
                shown[f"{key}_not_shown"] = len(rows) - MAX_SHOWN
        out["last_breaker_trip"] = shown
        return out
