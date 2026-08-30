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
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .analysis import (
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


def _raw(state) -> float | None:
    """The numeric part of a state, with no unit interpretation."""
    if state is None or state.state in ("unknown", "unavailable", "", None):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _unit(state) -> str | None:
    return state.attributes.get("unit_of_measurement") if state else None


class FingerprintCoordinator(DataUpdateCoordinator):
    """Maintains a rolling sample window and derives the label-free checks."""

    def __init__(
        self,
        hass: HomeAssistant,
        options: dict,
        entry_id: str,
        store: object | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
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
        self._window: dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._seeded = False
        # Samples of the run currently in progress on each circuit, if any.
        self._live_run: dict[str, list[float]] = defaultdict(list)
        # The unit each source actually reports in, and the sensors whose unit
        # is not power at all. Both are surfaced in diagnostics: the meter is
        # the single biggest unknown in any bug report about this integration.
        self._units: dict[str, str | None] = {}
        self._rejected: set[str] = set()

    def _watts(self, entity: str, state) -> float | None:
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
            return None
        return watts

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
            self._seeded = True
            return

        start = dt_util.utcnow() - timedelta(hours=WINDOW_HOURS)

        def _fetch():
            return history.state_changes_during_period(
                self.hass,
                start,
                dt_util.utcnow(),
                entity_id=None,
                include_start_time_state=False,
                no_attributes=True,
            )

        try:
            data = await get_instance(self.hass).async_add_executor_job(_fetch)
        except Exception as err:
            _LOGGER.debug("Could not seed window from recorder: %s", err)
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
        _LOGGER.debug(
            "Seeded %d circuits from recorder",
            sum(1 for c in self.circuits if self._window[c]),
        )
        self._seeded = True

    async def _async_update_data(self) -> dict:
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

        return {
            "coverage": cov,
            "standby": ranking,
            "standby_total_w": round(sum(floors.values()), 1),
            "standby_annual_cost": round(sum(floors.values()) * 8.766 * self.price, 2),
            "contradictions": clashes,
            "silent_circuits": silent,
            "tolerance_pct": self.tolerance,
            "labelled_fingerprints": (len(self.store.labelled()) if self.store else 0),
            "running": running,
        }

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
