"""Setup, unload, and the orphaned-pause backstop."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.power_fingerprint.const import CONF_CIRCUITS, DOMAIN

CIRCUIT_A = "sensor.circuit_a_power"


async def test_setup_and_unload(hass: HomeAssistant, config_entry, powered):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.coordinator is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_services_are_registered(hass: HomeAssistant, config_entry, powered):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for service in ("learn", "label", "verify_circuit"):
        assert hass.services.has_service(DOMAIN, service), service


async def test_a_dead_probe_does_not_leave_automations_switched_off(
    hass: HomeAssistant, config_entry, powered
):
    """THE SAFETY BACKSTOP, AND THE REASON IT EXISTS.

    If a probe pauses automations and the process then dies - killed, restarted,
    power cut - nothing else would ever switch them back on and the house would
    simply stop reacting, with no error anywhere. Setup must clean that up.

    Simulated by seeding the store as though a previous run had paused two
    automations and never finished.
    """
    config_entry.add_to_hass(hass)

    orphaned = ["automation.hall_motion", "automation.porch"]
    with (
        patch(
            "custom_components.power_fingerprint.store.FingerprintStore"
            ".orphaned_pauses",
            return_value=orphaned,
        ),
        patch(
            "custom_components.power_fingerprint.store.FingerprintStore"
            ".async_clear_paused",
        ) as clear,
    ):
        calls = async_mock_service(hass, "automation", "turn_on")
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    turned_on = [c.data["entity_id"] for c in calls]
    assert set(turned_on) == set(orphaned), turned_on
    # The record is cleared only after the restores dispatched.
    clear.assert_awaited_once()


async def test_nothing_is_touched_when_no_pauses_are_orphaned(
    hass: HomeAssistant, config_entry, powered
):
    """The backstop must not fire on a normal startup."""
    config_entry.add_to_hass(hass)
    calls = async_mock_service(hass, "automation", "turn_on")
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert not calls


# --- action-setup ----------------------------------------------------------


async def test_actions_exist_before_any_entry_is_set_up(hass: HomeAssistant):
    """Registered at component setup, so automations referencing them validate.

    Registered per entry they would vanish whenever the entry was unloaded, and
    every automation calling one would fail validation with "action not found"
    - which reads as a typo rather than as an integration that is down.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    for action in ("learn", "label", "verify_circuit"):
        assert hass.services.has_service(DOMAIN, action)


async def test_an_action_called_with_nothing_loaded_says_so(hass: HomeAssistant):
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "label",
            {"circuit": "sensor.x", "current_label": "a", "new_label": "b"},
            blocking=True,
        )
    assert err.value.translation_key == "not_loaded"


async def test_the_actions_survive_the_entry_being_unloaded(
    hass: HomeAssistant, config_entry, powered
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "learn")


# --- test-before-setup -----------------------------------------------------


async def test_setup_retries_while_the_power_sensors_are_missing(
    hass: HomeAssistant, config_entry
):
    """No `powered` fixture, so nothing the entry names exists yet.

    Setting up anyway would produce a device full of entities reading unknown
    with no explanation. ConfigEntryNotReady makes Home Assistant retry with
    backoff, which is what actually fixes it when the meter's integration
    loads a moment later.
    """
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_renamed_circuit_is_followed_not_orphaned(
    hass: HomeAssistant, config_entry, powered
):
    """Renaming a source sensor is an ordinary user action on the meter
    integration. The entry config must follow it instead of the circuit list
    silently pointing at a dead id."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "sensor",
        "test",
        "circuit-a-uid",
        suggested_object_id="circuit_a_power",
    )
    assert row.entity_id == CIRCUIT_A

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(
        "sensor.garage_power",
        50.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
    )
    registry.async_update_entity(CIRCUIT_A, new_entity_id="sensor.garage_power")
    await hass.async_block_till_done()

    assert "sensor.garage_power" in config_entry.data[CONF_CIRCUITS]
    assert CIRCUIT_A not in config_entry.data[CONF_CIRCUITS]
