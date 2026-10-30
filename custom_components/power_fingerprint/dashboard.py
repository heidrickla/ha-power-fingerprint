"""Reads what the energy dashboard already knows: price, names and power sensors.

All are things the user has set once already. A second place to enter them
is a second place for them to be wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def _async_prefs(hass: HomeAssistant) -> Mapping[str, Any]:
    """The energy dashboard's preferences, empty for every way they can be absent.

    Read as a loose mapping on purpose: the energy schema has changed shape
    across versions and this module handles every shape it has had.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return {}
    try:
        manager = await async_get_manager(hass)
    except Exception as err:
        _LOGGER.debug("Could not read the energy dashboard: %s", err)
        return {}
    return cast("Mapping[str, Any] | None", manager.data) or {}


def _price_from_source(hass: HomeAssistant, source: Mapping[str, Any]) -> float | None:
    """Pull a price out of one energy source, whatever shape it is in.

    Home Assistant has carried two shapes for a grid source: the price keys
    sit directly on the source in some versions and inside a `flow_from` list
    in others. Handling only the shape in front of you works until the next
    upgrade, so both are read here.
    """
    candidates: list[Mapping[str, Any]] = [source]
    flow_from = source.get("flow_from")
    if isinstance(flow_from, list):
        candidates.extend(f for f in flow_from if isinstance(f, dict))

    for candidate in candidates:
        number = candidate.get("number_energy_price")
        if isinstance(number, int | float):
            return float(number)
        entity_id = candidate.get("entity_energy_price")
        if entity_id:
            # A price entity is a live tariff. Take its value now; the flow
            # only needs a sensible starting number, not a subscription.
            state = hass.states.get(str(entity_id))
            if state is not None:
                try:
                    return float(state.state)
                except TypeError, ValueError:
                    continue
    return None


async def async_dashboard_price(hass: HomeAssistant) -> float | None:
    """The energy dashboard's configured price per kWh, or None.

    Returns None rather than raising for every way this can be absent - the
    energy integration not set up, no grid source, no price configured. A
    missing price is an ordinary state, not a fault, and the caller has a
    perfectly good fallback.
    """
    for source in (await _async_prefs(hass)).get("energy_sources", []):
        if not isinstance(source, dict) or source.get("type") != "grid":
            continue
        price = _price_from_source(hass, source)
        if price is not None:
            _LOGGER.debug("Using the energy dashboard's price of %s per kWh", price)
            return price
    return None


async def async_circuit_names(hass: HomeAssistant) -> dict[str, str]:
    """Circuit power sensor -> the name the user gave it on the energy dashboard.

    THE BEST NAMES IN THE HOUSE ARE USUALLY ALREADY ON THAT SCREEN. Entity
    titles come from the meter's firmware and read "EmporiaVue Circuit 25
    Power"; the energy dashboard is where somebody sat down and typed "Circuit
    25 Garage", "Circuit 20 Microwave", "Circuit 14 Washer". Reading those back
    is free and certain, and it is the difference between a user facing a list
    of numbered shapes and facing named appliances.

    Keyed by `stat_rate`, which is the circuit's POWER sensor and therefore
    exactly what this integration is configured with. The consumption
    statistic is a different entity and would not match.
    """
    out: dict[str, str] = {}
    for device in (await _async_prefs(hass)).get("device_consumption", []):
        if not isinstance(device, dict):
            continue
        rate = device.get("stat_rate")
        name = device.get("name")
        if rate and name:
            out[str(rate)] = str(name)
    return out


async def async_dashboard_power_sensors(
    hass: HomeAssistant,
) -> tuple[list[str], list[str]]:
    """The power sensors the energy dashboard names for the mains and the circuits.

    The grid's power sensor reads the whole panel only when nothing else feeds
    the house, so a solar or battery source leaves the mains unnamed. Circuits
    are the individual devices no other device includes, so a plug metered
    inside a circuit is not counted twice.
    """
    prefs = await _async_prefs(hass)
    sources = [s for s in prefs.get("energy_sources", []) if isinstance(s, dict)]
    mains: list[str] = []
    if all(s.get("type") == "grid" for s in sources):
        for source in sources:
            # The unified grid source carries `stat_rate`; the older one a `power` list.
            legacy = source.get("power")
            for power in [source, *(legacy if isinstance(legacy, list) else [])]:
                if isinstance(power, dict) and power.get("stat_rate"):
                    mains.append(str(power["stat_rate"]))
    circuits = [
        str(device["stat_rate"])
        for device in prefs.get("device_consumption", [])
        if isinstance(device, dict)
        and device.get("stat_rate")
        and not device.get("included_in_stat")
    ]
    return mains, circuits
