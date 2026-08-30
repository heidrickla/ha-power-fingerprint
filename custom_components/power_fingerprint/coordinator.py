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
    parse_pairs,
    standby_ranking,
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

_LOGGER = logging.getLogger(__name__)


def _as_float(state) -> float | None:
    if state is None or state.state in ("unknown", "unavailable", "", None):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


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
            for st in data.get(entity, []):
                val = _as_float(st)
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
            val = _as_float(self.hass.states.get(entity))
            if val is None:
                continue
            live[entity] = val
            self._window[entity].append((now, val))

        mains_val = _as_float(self.hass.states.get(self.mains))

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

        return {
            "coverage": cov,
            "standby": ranking,
            "standby_total_w": round(sum(floors.values()), 1),
            "standby_annual_cost": round(sum(floors.values()) * 8.766 * self.price, 2),
            "contradictions": clashes,
            "silent_circuits": silent,
            "tolerance_pct": self.tolerance,
            "labelled_fingerprints": (len(self.store.labelled()) if self.store else 0),
        }

    def window_sizes(self) -> dict[str, int]:
        """How many samples each circuit has accumulated. Used by diagnostics
        to show whether the rolling window has actually filled - a standby
        figure from a nearly empty window is not yet meaningful."""
        return {e: len(self._window[e]) for e in self.circuits}
