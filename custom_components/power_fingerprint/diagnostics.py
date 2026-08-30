"""Downloadable diagnostics.

Deliberately includes the derived numbers and the configuration shape, but
redacts the entity ids themselves. Entity ids in this integration are named
after rooms and appliances - `sensor.master_bathroom_motion_light_power` says
where someone lives and what is in their house - and a diagnostics file is
routinely pasted into a public issue tracker. The counts and the analysis are
what a maintainer needs; the room names are not.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CIRCUITS, CONF_MAINS, CONF_PAIRS, DOMAIN
from .coordinator import FingerprintCoordinator


def _anon(entity_id: str) -> str:
    """Stable pseudonym so the same circuit is recognisable across a report."""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
    digest = hashlib.sha256(entity_id.encode()).hexdigest()[:8]
    return f"{domain}.redacted_{digest}"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FingerprintCoordinator = entry_data["coordinator"]
    data = coordinator.data or {}

    standby = [
        {**row, "circuit": _anon(str(row.get("circuit", "")))}
        for row in data.get("standby", [])
    ]
    clashes = [
        {
            **row,
            "switch": _anon(str(row.get("switch", ""))),
            "circuit": _anon(str(row.get("circuit", ""))),
        }
        for row in data.get("contradictions", [])
    ]

    return {
        "config": {
            "mains": _anon(coordinator.mains),
            "circuit_count": len(coordinator.circuits),
            "pair_count": len(coordinator.pairs),
            "tolerance_pct": coordinator.tolerance,
            "price_per_kwh": coordinator.price,
            # The raw option values are redacted wholesale rather than
            # per-field: CONF_PAIRS is free text and could contain anything.
            "raw_options_present": sorted(
                k
                for k in (CONF_MAINS, CONF_CIRCUITS, CONF_PAIRS)
                if k in {**entry.data, **entry.options}
            ),
        },
        "coverage": data.get("coverage"),
        "standby_total_w": data.get("standby_total_w"),
        "standby_annual_cost": data.get("standby_annual_cost"),
        "standby_ranking": standby[:15],
        "contradictions": clashes,
        "silent_circuit_count": len(data.get("silent_circuits", [])),
        "window_filled": {
            _anon(entity): len(samples)
            for entity, samples in coordinator.window_sizes().items()
        },
    }
