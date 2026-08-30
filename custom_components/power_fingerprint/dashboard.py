"""What the energy dashboard already knows, so nobody types it twice.

⭐ IF THE ENERGY DASHBOARD ALREADY KNOWS THE PRICE, ASK IT. A second place to
type your tariff is a second place for it to be wrong, and the one that is
wrong is always the one nobody looks at. On the development install the
dashboard held $0.145/kWh while this integration defaulted to a hardcoded
$0.13, so every standby cost it reported was 11% low - not obviously wrong,
just quietly wrong, which is worse.

The value is only ever used as the DEFAULT offered in the config flow. Once a
user has set a price it is theirs, and a later change on the dashboard does not
silently reach in and alter what they configured.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _price_from_source(hass: HomeAssistant, source: dict[str, Any]) -> float | None:
    """Pull a price out of one energy source, whatever shape it is in.

    ⚠ Home Assistant has carried two shapes for a grid source: the price keys
    sit directly on the source in some versions and inside a `flow_from` list
    in others. Handling only the shape in front of you works until the next
    upgrade, so both are read here.
    """
    candidates: list[dict[str, Any]] = [source]
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
                except (TypeError, ValueError):
                    continue
    return None


async def async_dashboard_price(hass: HomeAssistant) -> float | None:
    """The energy dashboard's configured price per kWh, or None.

    Returns None rather than raising for every way this can be absent - the
    energy integration not set up, no grid source, no price configured. A
    missing price is an ordinary state, not a fault, and the caller has a
    perfectly good fallback.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return None

    try:
        manager = await async_get_manager(hass)
    except Exception as err:
        _LOGGER.debug("Could not read the energy dashboard's price: %s", err)
        return None

    data = manager.data or {}
    for source in data.get("energy_sources", []):
        if not isinstance(source, dict) or source.get("type") != "grid":
            continue
        price = _price_from_source(hass, source)
        if price is not None:
            _LOGGER.debug("Using the energy dashboard's price of %s per kWh", price)
            return price
    return None


async def async_circuit_names(hass: HomeAssistant) -> dict[str, str]:
    """Circuit power sensor -> the name the user gave it on the energy dashboard.

    ⭐ THE BEST NAMES IN THE HOUSE ARE USUALLY ALREADY ON THAT SCREEN. Entity
    titles come from the meter's firmware and read "EmporiaVue Circuit 25
    Power"; the energy dashboard is where somebody sat down and typed "Circuit
    25 Garage", "Circuit 26 Microwave", "Circuit 21 Washer". Reading those back
    is free and certain, and it is the difference between a user facing a list
    of numbered shapes and facing named appliances.

    Keyed by `stat_rate`, which is the circuit's POWER sensor and therefore
    exactly what this integration is configured with. The consumption
    statistic is a different entity and would not match.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return {}
    try:
        manager = await async_get_manager(hass)
    except Exception as err:
        _LOGGER.debug("Could not read the energy dashboard's names: %s", err)
        return {}

    out: dict[str, str] = {}
    for device in (manager.data or {}).get("device_consumption", []):
        if not isinstance(device, dict):
            continue
        rate = device.get("stat_rate")
        name = device.get("name")
        if rate and name:
            out[str(rate)] = str(name)
    return out
