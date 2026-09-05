"""Reading the energy dashboard: the price, and the names of the circuits.

Both are things the user typed once already, and a second place to type them
is a second place for them to be wrong.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.power_fingerprint.dashboard import (
    async_circuit_names,
    async_dashboard_price,
)

MANAGER = "homeassistant.components.energy.data.async_get_manager"


def manager(prefs):
    """The energy manager, which carries the dashboard preferences in `data`."""

    async def _get(hass):
        return SimpleNamespace(data=prefs)

    return _get


async def test_the_price_typed_straight_onto_a_grid_source_is_used(
    hass: HomeAssistant,
):
    prefs = {
        "energy_sources": [{"type": "grid", "number_energy_price": 0.145}],
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) == 0.145


async def test_a_price_nested_in_flow_from_is_found_too(hass: HomeAssistant):
    """Home Assistant has carried both shapes for a grid source; handling only
    the one in front of you works until the next upgrade."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"number_energy_price": 0.21}],
            }
        ]
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) == 0.21


async def test_a_live_tariff_entity_is_read_at_its_current_value(
    hass: HomeAssistant,
):
    hass.states.async_set("sensor.tariff", "0.32")
    prefs = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"entity_energy_price": "sensor.tariff"}]}
        ]
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) == 0.32


async def test_a_tariff_entity_that_is_not_a_number_yields_no_price(
    hass: HomeAssistant,
):
    """`unknown` at the moment the flow opens is ordinary, not a fault."""
    hass.states.async_set("sensor.tariff", "unknown")
    prefs = {
        "energy_sources": [
            {"type": "grid", "flow_from": [{"entity_energy_price": "sensor.tariff"}]}
        ]
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) is None


async def test_a_tariff_entity_that_does_not_exist_yields_no_price(
    hass: HomeAssistant,
):
    prefs = {"energy_sources": [{"type": "grid", "entity_energy_price": "sensor.gone"}]}
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) is None


async def test_solar_and_battery_sources_are_not_asked_for_a_grid_price(
    hass: HomeAssistant,
):
    prefs = {
        "energy_sources": [
            {"type": "solar", "number_energy_price": 9.99},
            "not a dict at all",
        ]
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_dashboard_price(hass) is None


async def test_no_preferences_at_all_is_no_price(hass: HomeAssistant):
    with patch(MANAGER, manager(None)):
        assert await async_dashboard_price(hass) is None


async def test_an_energy_dashboard_that_will_not_answer_is_not_a_fault(
    hass: HomeAssistant,
):
    """A missing price is an ordinary state and the caller has a fallback."""

    async def _boom(hass):
        raise RuntimeError("energy not set up")

    with patch(MANAGER, _boom):
        assert await async_dashboard_price(hass) is None
    with patch(MANAGER, _boom):
        assert await async_circuit_names(hass) == {}


async def test_the_circuit_names_come_back_keyed_by_the_power_sensor(
    hass: HomeAssistant,
):
    """Keyed by `stat_rate`, which is the circuit's POWER sensor and therefore
    exactly what this integration is configured with."""
    prefs = {
        "device_consumption": [
            {"stat_rate": "sensor.circuit_25_power", "name": "Circuit 25 Garage"},
            {"stat_rate": "sensor.circuit_26_power", "name": "Circuit 26 Microwave"},
            {"stat_rate": "sensor.no_name_power"},
            {"name": "no rate"},
            "not a dict",
        ]
    }
    with patch(MANAGER, manager(prefs)):
        assert await async_circuit_names(hass) == {
            "sensor.circuit_25_power": "Circuit 25 Garage",
            "sensor.circuit_26_power": "Circuit 26 Microwave",
        }


async def test_no_devices_on_the_dashboard_is_no_names(hass: HomeAssistant):
    with patch(MANAGER, manager({})):
        assert await async_circuit_names(hass) == {}


@pytest.mark.parametrize("read", [async_dashboard_price, async_circuit_names])
async def test_an_install_without_the_energy_component_reads_nothing(
    hass: HomeAssistant, read
):
    """The energy integration is not a dependency; an install without it is
    supported and falls back to the configured price."""
    with patch.dict(sys.modules, {"homeassistant.components.energy.data": None}):
        assert not await read(hass)
