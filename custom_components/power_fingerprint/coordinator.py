"""Polls the configured power sensors and runs the label-free checks.

Keeps its own rolling window in memory rather than querying the recorder on
every refresh. Two reasons: the recorder purges (30 days by default, and the
window needed here is 24 hours), and repeated history queries against a
multi-gigabyte recorder database are slow enough to matter on every poll.

The window IS seeded from the recorder once at startup, so standby figures are
meaningful immediately after a restart instead of taking a day to converge.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .store import FingerprintStore

from .analysis import (
    Cadence,
    Sample,
    absence,
    circuit_floor,
    contradictions,
    coverage,
    on_threshold,
    parse_pairs,
    sample_interval,
    standby_ranking,
    to_watts,
)
from .const import (
    CONF_CIRCUITS,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DEFAULT_PRICE,
    DEFAULT_TOLERANCE,
    DOMAIN,
    POLL_SECONDS,
    WINDOW_HOURS,
)
from .fingerprint import match

_LOGGER = logging.getLogger(__name__)


@dataclass
class PowerFingerprintData:
    """Everything one config entry owns at runtime.

    Held on `entry.runtime_data` rather than in `hass.data`, so the type is
    carried by the entry itself and every consumer gets it checked rather than
    fishing an untyped dict out of a shared dictionary.
    """

    coordinator: FingerprintCoordinator
    store: FingerprintStore


type PowerFingerprintConfigEntry = ConfigEntry[PowerFingerprintData]


def _raw(state: State | None) -> float | None:
    """The numeric part of a state, with no unit interpretation."""
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _unit(state: State | None) -> str | None:
    unit = state.attributes.get("unit_of_measurement") if state else None
    return str(unit) if unit is not None else None


class FingerprintCoordinator(DataUpdateCoordinator):
    """Maintains a rolling sample window and derives the label-free checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        options: dict[str, Any],
        entry_id: str,
        store: FingerprintStore | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Polls the state machine rather than a device or a network
            # service, so the interval is not a politeness question. 30 s is
            # the standby window's resolution: standby is a 5th percentile over
            # 24 hours and does not move faster than that. The live appliance
            # match reads whatever the meter has most recently published, so a
            # faster poll would resample the same value.
            update_interval=timedelta(seconds=POLL_SECONDS),
        )
        self.entry_id = entry_id
        self.store = store
        self.mains: str = options[CONF_MAINS]
        self.circuits: list[str] = list(options[CONF_CIRCUITS])
        self.price: float = float(options.get(CONF_PRICE, DEFAULT_PRICE))
        self.tolerance: float = float(options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))
        self.pairs: list[tuple[str, str]] = parse_pairs(options.get(CONF_PAIRS, ""))
        maxlen = int(WINDOW_HOURS * 3600 / POLL_SECONDS)
        self._window: dict[str, deque[Sample]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )
        self._seeded = False
        # Samples of the run currently in progress on each circuit, if any.
        self._live_run: dict[str, list[float]] = defaultdict(list)
        # The unit each source actually reports in, and the sensors whose unit
        # is not power at all. Both are surfaced in diagnostics: the meter is
        # the single biggest unknown in any bug report about this integration.
        self._units: dict[str, str | None] = {}
        self._rejected: set[str] = set()
        # Which sources have gone away, so their disappearance is logged once
        # rather than every 30 seconds, and logged again when they return.
        self._missing: set[str] = set()
        # Seconds each circuit spent unreadable since its appliance was last
        # seen. ⛔ THIS IS THE WHOLE POINT OF ABSENCE DETECTION: time nobody was
        # watching is not evidence that nothing happened.
        self._blind_s: dict[str, float] = defaultdict(float)
        self._last_seen: dict[str, str] = {}
        self._seen_dirty = False
        # Resolved once, on the first refresh after startup.
        self._last_poll_gap: float | None = None

    def _watts(self, entity: str, state: State | None) -> float | None:
        """One reading, converted to watts, or None if it is unusable.

        A sensor whose unit is not a power unit is dropped and named once in
        the log rather than every 30 seconds. `device_class: power` does not
        constrain the unit, so this really does happen.
        """
        value = _raw(state)
        if value is None:
            return None
        unit = _unit(state)
        self._units[entity] = unit
        watts = to_watts(value, unit)
        if watts is None:
            if entity not in self._rejected:
                self._rejected.add(entity)
                _LOGGER.warning(
                    "%s reports in %r, which is not a power unit - excluding it. "
                    "Every figure in this integration is in watts.",
                    entity,
                    unit,
                )
                # A repair issue rather than only a log line: the consequence
                # is a circuit silently missing from every total, which is
                # invisible in the UI and would otherwise be found by nobody.
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"bad_unit_{entity}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="bad_unit",
                    translation_placeholders={"entity": entity, "unit": str(unit)},
                )
            return None
        if entity in self._rejected:
            self._rejected.discard(entity)
            ir.async_delete_issue(self.hass, DOMAIN, f"bad_unit_{entity}")
        return watts

    def _note_availability(self, seen: set[str]) -> None:
        """Log a source going away once, and its return once.

        Every 30 seconds is not a log, it is a denial of service on the log
        file - and the transition is the only part anyone needs.
        """
        configured = {self.mains, *self.circuits}
        missing = configured - seen
        for entity in sorted(missing - self._missing):
            _LOGGER.warning("%s is unavailable - it is no longer being read", entity)
        for entity in sorted(self._missing - missing):
            _LOGGER.info("%s is available again", entity)
        self._missing = missing

    @property
    def sources_available(self) -> bool:
        """False when nothing configured is readable.

        Deliberately not "any source missing": one circuit dropping out is a
        real condition the coverage sensor is there to report, and marking
        every entity unavailable would hide it. Only a total loss means the
        derived numbers are meaningless.
        """
        return len(self._missing) < len(self.circuits) + 1

    def _match_live(self, entity: str, watts: float) -> dict[str, object]:
        """Identify what is running on one circuit, right now.

        Matching happens on a PARTIAL run - the samples seen so far - rather
        than waiting for it to finish. That works here only because of an
        earlier decision: duration and energy are weighted to zero for
        identity, and every feature that does count (peak, floor, plateau
        count, duty) is computable mid-run. Had duration mattered, nothing
        could be identified until it was already over, which is useless for
        driving an automation.

        Three outcomes, and the third is not a failure:
          running + matched   -> the appliance name
          not running         -> idle
          running + no match  -> unknown, meaning something new was plugged in
                                 or an appliance changed behaviour
        """
        window = list(self._window[entity])
        if len(window) < 10:
            return {"state": None, "reason": "window still filling"}

        threshold = on_threshold(window)
        if watts <= threshold:
            self._live_run[entity].clear()
            return {"state": "idle", "threshold_w": round(threshold, 1)}

        self._live_run[entity].append(watts)
        samples = self._live_run[entity]
        if len(samples) < 3:
            return {"state": "starting", "threshold_w": round(threshold, 1)}

        library = self.store.for_circuit(entity) if self.store else []
        if not library:
            return {"state": "unknown", "reason": "no named fingerprint yet"}

        peak = max(samples)
        features = {
            "peak_w": peak,
            "floor_w": min(samples),
            "mean_w": sum(samples) / len(samples),
            "plateaus": len({round(w / 50.0) for w in samples}),
            "duty_above_half_peak": sum(1 for w in samples if w > peak / 2)
            / len(samples),
        }
        best, distance = match(features, library)
        return {
            "state": best.label if best else "unknown",
            "distance": round(distance, 3),
            "samples": len(samples),
            "threshold_w": round(threshold, 1),
        }

    async def _async_seed_from_recorder(self) -> None:
        """Fill the window from history once, so standby is not blank on boot."""
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:  # recorder disabled
            _LOGGER.warning(
                "The recorder is not available, so standby figures start from "
                "an empty window and take up to %d hours to mean anything",
                WINDOW_HOURS,
            )
            self._seeded = True
            return

        start = dt_util.utcnow() - timedelta(hours=WINDOW_HOURS)

        def _fetch() -> dict[str, list[State]]:
            # ⛔ ASK FOR THE CONFIGURED ENTITIES, NOT FOR EVERYTHING. The first
            # version passed entity_id=None, which asks the recorder for 24
            # hours of EVERY entity in the house. On a real install that is
            # millions of rows; it failed, the failure was swallowed into a
            # debug line, and the window stayed empty. Standby then reported
            # the 5th percentile of about sixty seconds of samples - which on a
            # night with the air conditioning running came out as 5,017 W of
            # "standby" against a true 24-hour figure near zero. A confident
            # wrong number, with nothing anywhere saying it was wrong.
            rows: dict[str, list[State]] = history.get_significant_states(
                self.hass,
                start,
                dt_util.utcnow(),
                entity_ids=list(self.circuits),
                include_start_time_state=False,
                no_attributes=True,
            )
            return rows

        try:
            data = await get_instance(self.hass).async_add_executor_job(_fetch)
        except Exception as err:
            # WARNING, not debug. A silent seed failure produces standby
            # figures that look real and are not.
            _LOGGER.warning(
                "Could not seed the window from the recorder (%s) - standby "
                "figures will be meaningless until the window fills",
                err,
            )
            self._seeded = True
            return

        for entity in self.circuits:
            # `no_attributes=True` above strips unit_of_measurement from every
            # history row, so the unit comes from the entity's live state and
            # is applied to the whole seeded window. Asking the recorder for
            # attributes instead would mean loading and de-duplicating an
            # attributes blob per row across 24 hours of samples, which is the
            # expensive half of a history query and buys nothing: a sensor
            # does not change its unit mid-stream.
            unit = _unit(self.hass.states.get(entity))
            for st in data.get(entity, []):
                val = to_watts(_raw(st), unit)
                if val is not None:
                    self._window[entity].append((st.last_changed, val))
        seeded = sum(1 for c in self.circuits if self._window[c])
        if seeded:
            _LOGGER.debug(
                "Seeded %d of %d circuits from the recorder (%d samples)",
                seeded,
                len(self.circuits),
                sum(len(self._window[c]) for c in self.circuits),
            )
        else:
            _LOGGER.warning(
                "The recorder returned no history for any of the %d configured "
                "circuits - standby figures will be meaningless until the "
                "window fills",
                len(self.circuits),
            )
        self._seeded = True

    async def _async_update_data(self) -> dict[str, Any]:
        if not self._seeded:
            await self._async_seed_from_recorder()

        now: datetime = dt_util.utcnow()
        live: dict[str, float] = {}
        for entity in self.circuits:
            val = self._watts(entity, self.hass.states.get(entity))
            if val is None:
                continue
            live[entity] = val
            self._window[entity].append((now, val))

        mains_val = self._watts(self.mains, self.hass.states.get(self.mains))
        seen = set(live)
        if mains_val is not None:
            seen.add(self.mains)
        self._note_availability(seen)
        self._accrue_blind_time(now, seen)

        cov = coverage(mains_val, live) if mains_val is not None else None
        floors = {
            e: circuit_floor(list(self._window[e]))
            for e in self.circuits
            if self._window[e]
        }
        ranking = standby_ranking(floors, self.price)

        pair_input = []
        for switch, circuit in self.pairs:
            sw = self.hass.states.get(switch)
            watts = live.get(circuit)
            if sw is None or watts is None:
                continue
            pair_input.append((switch, circuit, sw.state == "on", watts))
        clashes = contradictions(pair_input)

        # A CT that has never moved is reported separately from a coverage
        # fault: an unused circuit legitimately reads zero forever, so this is
        # information rather than an alarm.
        silent = [
            e
            for e in self.circuits
            if self._window[e] and max(w for _, w in self._window[e]) < 5.0
        ]

        running = {e: self._match_live(e, live[e]) for e in self.circuits if e in live}
        self._note_seen(now, running)
        quiet = self._absences(now)
        await self.async_persist_seen(now)

        return {
            "coverage": cov,
            "standby": ranking,
            "standby_total_w": round(sum(floors.values()), 1),
            "standby_annual_cost": round(sum(floors.values()) * 8.766 * self.price, 2),
            # ⛔ Standby is a 5th percentile, so it only means anything once the
            # window spans a duty cycle or two. Published so the entities can
            # refuse to answer rather than publish a number from four minutes
            # of data that looks exactly as authoritative as a real one.
            "window_hours": round(self.window_hours(), 2),
            "contradictions": clashes,
            "silent_circuits": silent,
            "tolerance_pct": self.tolerance,
            "labelled_fingerprints": (len(self.store.labelled()) if self.store else 0),
            "running": running,
            "absent": [row for row in quiet if row["state"] == "overdue"],
            "absence_detail": quiet,
        }

    def _accrue_blind_time(self, now: datetime, seen: set[str]) -> None:
        """Count the seconds each circuit was unreadable.

        Two sources, and the second is the one that is easy to forget. A
        circuit that is `unavailable` at a poll contributes one poll interval.
        A Home Assistant restart contributes the whole gap since the last
        recorded poll, during which this coordinator was not running at all -
        that is the longest blind stretch most installs will ever have, and
        counting it as silence would fire an absence alert on every reboot.
        """
        if self.store is not None and self._last_poll_gap is None:
            recorded = self.store.last_poll()
            gap = 0.0
            if recorded:
                try:
                    gap = max(
                        0.0, (now - datetime.fromisoformat(recorded)).total_seconds()
                    )
                except ValueError:
                    gap = 0.0
            self._last_poll_gap = gap
            if gap > POLL_SECONDS * 2:
                _LOGGER.debug(
                    "Home Assistant was down or this entry unloaded for %.0f s - "
                    "counting it as unobserved, not as silence",
                    gap,
                )
                for entity in self.circuits:
                    self._blind_s[entity] += gap

        for entity in self.circuits:
            if entity not in seen:
                self._blind_s[entity] += float(POLL_SECONDS)

    def _note_seen(self, now: datetime, running: dict[str, dict[str, object]]) -> None:
        """Record any named appliance observed running, and clear its blindness."""
        if self.store is None:
            return
        stamp = now.isoformat()
        for circuit, info in running.items():
            state = info.get("state")
            if not isinstance(state, str) or state in ("idle", "starting", "unknown"):
                continue
            self._last_seen[self.store.seen_key(circuit, state)] = stamp
            # Blind time is only ever measured BACK TO the last sighting, so a
            # fresh sighting resets it. Otherwise an install that was offline
            # for a week could never raise an absence alert again.
            self._blind_s[circuit] = 0.0
            self._seen_dirty = True

    def _absences(self, now: datetime) -> list[dict[str, Any]]:
        """Which named appliances have gone quiet, and which cannot be judged."""
        if self.store is None:
            return []
        known = {**self.store.last_seen(), **self._last_seen}
        out: list[dict[str, Any]] = []
        for fp in self.store.labelled():
            if not fp.cadence:
                continue
            key = self.store.seen_key(fp.circuit, fp.label)
            stamp = known.get(key)
            if stamp is None:
                # Never seen since learning. Honest answer is "not yet", not
                # "overdue" - the library was built from history this
                # coordinator did not watch.
                continue
            try:
                silent_s = (now - datetime.fromisoformat(stamp)).total_seconds()
            except ValueError:
                continue
            rhythm = Cadence(
                runs=int(fp.cadence.get("runs", 0)),
                median_gap_s=float(fp.cadence.get("median_gap_s", 0.0)),
                p90_gap_s=float(fp.cadence.get("p90_gap_s", 0.0)),
            )
            state, why = absence(rhythm, silent_s, self._blind_s[fp.circuit])
            out.append(
                {
                    "appliance": fp.label,
                    "circuit": fp.circuit,
                    "state": state,
                    "reason": why,
                    "silent_h": round(silent_s / 3600.0, 1),
                    "unobserved_h": round(self._blind_s[fp.circuit] / 3600.0, 1),
                    "expected_every_h": round(rhythm.median_gap_s / 3600.0, 1),
                }
            )
        return out

    async def async_persist_seen(self, now: datetime) -> None:
        """Write last-seen times out, but only when something changed."""
        if self.store is None or not self._seen_dirty:
            return
        self._seen_dirty = False
        await self.store.async_record_seen(self._last_seen, now.isoformat())

    def window_hours(self) -> float:
        """How much wall-clock time the rolling window actually spans.

        ⭐ THE SAMPLE COUNT IS NOT THE ANSWER. A window can hold hundreds of
        samples and still cover four minutes, and a standby figure from four
        minutes is a confident wrong number rather than a rough one. What
        matters is the span.
        """
        spans = [
            (w[-1][0] - w[0][0]).total_seconds()
            for w in self._window.values()
            if len(w) >= 2
        ]
        return (max(spans) / 3600.0) if spans else 0.0

    def window_sizes(self) -> dict[str, int]:
        """How many samples each circuit has accumulated. Used by diagnostics
        to show whether the rolling window has actually filled - a standby
        figure from a nearly empty window is not yet meaningful."""
        return {e: len(self._window[e]) for e in self.circuits}

    def source_profile(self) -> dict[str, object]:
        """What the power source actually is, measured rather than assumed.

        This integration was developed against one meter, and the two things
        that differ between meters - the unit and the reporting cadence - are
        exactly the two that fail silently when they are wrong. Reporting both
        means a bug report from an unfamiliar meter arrives already diagnosed.

        The cadence here is the SEEDED cadence where the window came from the
        recorder, which is the meter's own rate. Once the window has turned
        over it converges on the coordinator's poll interval instead, since
        that is then what is doing the sampling.
        """
        cadences = {
            e: sample_interval(list(self._window[e]))
            for e in self.circuits
            if self._window[e]
        }
        measured = sorted(v for v in cadences.values() if v)
        return {
            "units": dict(self._units),
            "rejected_non_power_units": sorted(self._rejected),
            "poll_seconds": POLL_SECONDS,
            "median_report_interval_s": (
                measured[len(measured) // 2] if measured else None
            ),
            "per_circuit_interval_s": cadences,
        }
