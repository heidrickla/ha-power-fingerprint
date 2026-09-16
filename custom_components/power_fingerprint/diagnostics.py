"""Downloadable diagnostics.

Deliberately includes the derived numbers and the configuration shape, but
redacts the entity ids themselves. Entity ids in this integration are named
after rooms and appliances - `sensor.master_bathroom_motion_light_power` says
where someone lives and what is in their house - and a diagnostics file is
routinely pasted into a public issue tracker. The counts and the analysis are
what a maintainer needs; the room names are not.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_CIRCUITS, CONF_MAINS, CONF_PAIRS
from .coordinator import PowerFingerprintConfigEntry


class _Redactor:
    """Pseudonyms that hold across one report and carry nothing about the id.

    A DIGEST OF THE ENTITY ID ALONE IS REVERSIBLE HERE. These ids are built
    from a small vocabulary of room and appliance words: a 14,300-candidate
    dictionary recovered all three of the ids named as examples in this
    integration from their unsalted 8-hex SHA-256 prefix in 0.01 s on one
    core. A counter that restarts on every download cannot be inverted, and
    correlating one circuit across a single report is the whole requirement.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def __call__(self, entity_id: str) -> str:
        pseudonym = self._seen.get(entity_id)
        if pseudonym is None:
            domain = entity_id.split(".", 1)[0] if "." in entity_id else "entity"
            pseudonym = f"{domain}.redacted_{len(self._seen) + 1}"
            self._seen[entity_id] = pseudonym
        return pseudonym


def _source(profile: dict[str, Any], anon: _Redactor) -> dict[str, Any]:
    """Anonymise the entity ids in the source profile, keep the measurements.

    The units and cadences are the diagnostic value here and identify nobody;
    the entity ids naming rooms and appliances still have to go.
    """
    return {
        **profile,
        "units": _unit_histogram(profile.get("units", {})),
        "rejected_non_power_units": [
            anon(e) for e in sorted(profile.get("rejected_non_power_units", []))
        ],
        "per_circuit_interval_s": {
            anon(entity): (round(seconds, 1) if seconds else None)
            for entity, seconds in sorted(
                profile.get("per_circuit_interval_s", {}).items()
            )
        },
    }


def _unit_histogram(units: dict[str, str | None]) -> dict[str, int]:
    """How many sources report in each unit. A mixed panel - some circuits in
    W and some in kW - is a real configuration and shows up here as two keys."""
    counts: dict[str, int] = {}
    for unit in units.values():
        key = unit if unit else "(none declared)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    anon = _Redactor()

    standby = [
        {**row, "circuit": anon(str(row.get("circuit", "")))}
        for row in data.get("standby", [])
    ]
    clashes = [
        {
            **row,
            "switch": anon(str(row.get("switch", ""))),
            "circuit": anon(str(row.get("circuit", ""))),
        }
        for row in data.get("contradictions", [])
    ]

    return {
        "config": {
            "mains": anon(coordinator.mains),
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
        # `window_sizes()` already returns COUNTS. Calling len() on one raises
        # TypeError, which makes the whole diagnostics download return 500.
        # Sorted on the real id before redaction. Sorting on the pseudonym
        # orders redacted_10 before redacted_2.
        "window_filled": {
            anon(entity): count
            for entity, count in sorted(coordinator.window_sizes().items())
        },
        "window_hours": round(coordinator.window_hours(), 2),
        # The meter itself. This integration was developed against one brand of
        # per-circuit monitor, and unit and cadence are where another one will
        # differ - so report both rather than making a maintainer ask.
        "source": _source(coordinator.source_profile(), anon),
    }
