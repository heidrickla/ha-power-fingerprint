"""Setup, unload, and the orphaned-pause backstop."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.power_fingerprint.const import (
    CONF_CIRCUITS,
    CONF_MAINS,
    DOMAIN,
)

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
POWER = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}


async def test_setup_and_unload(hass: HomeAssistant, config_entry, powered):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.coordinator is not None

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_removing_the_entry_deletes_the_learned_library(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """The README promises the .storage file goes with the entry. Prove it.

    Unload writes the store, so the key is present before removal; the entry
    is keyed on its id, and a leftover file could never be found again.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await config_entry.runtime_data.store.async_save()
    key = f"{DOMAIN}.{config_entry.entry_id}"
    assert key in hass_storage

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


# --- action-exceptions -----------------------------------------------------


async def test_learn_without_the_recorder_is_a_translated_error(
    hass: HomeAssistant, config_entry, powered
):
    """The recorder is an after-dependency; a user may have disabled it.

    The test hass has no recorder loaded, which is exactly that state. A bare
    KeyError from get_instance reached the frontend as a stack trace.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "learn", {}, blocking=True)
    assert err.value.translation_key == "recorder_not_running"
    assert not isinstance(err.value, ServiceValidationError)


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

    The automation component is set up FIRST: the manifest declares it as a
    dependency, so entry setup would otherwise load the real component after
    the mock and its turn_on registration would replace the mock's.
    """
    assert await async_setup_component(hass, "automation", {})
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
    hass: HomeAssistant, config_entry
):
    """Renaming a source sensor is an ordinary user action on the meter
    integration. The entry config must follow it instead of the circuit list
    silently pointing at a dead id."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    # Registered BEFORE any state exists: with a state already in the machine
    # the registry treats the object id as taken and appends _2.
    row = registry.async_get_or_create(
        "sensor",
        "test",
        "circuit-a-uid",
        suggested_object_id="circuit_a_power",
    )
    assert row.entity_id == CIRCUIT_A
    for entity, value in (
        ("sensor.mains_power", 155.0),
        (CIRCUIT_A, 100.0),
        ("sensor.circuit_b_power", 50.0),
    ):
        hass.states.async_set(
            entity,
            value,
            {
                "device_class": "power",
                "state_class": "measurement",
                "unit_of_measurement": "W",
            },
        )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Rename first: the registry refuses a new id that already has a state.
    registry.async_update_entity(CIRCUIT_A, new_entity_id="sensor.garage_power")
    hass.states.async_set(
        "sensor.garage_power",
        50.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
    )
    await hass.async_block_till_done()

    assert "sensor.garage_power" in config_entry.data[CONF_CIRCUITS]
    assert CIRCUIT_A not in config_entry.data[CONF_CIRCUITS]


async def test_a_rename_takes_this_integrations_own_entities_with_it(
    hass: HomeAssistant, config_entry, hass_storage
):
    """The unique ids embed the source entity id, so a rename that did not
    migrate them minted duplicate entities and orphaned the history."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.power_fingerprint.fingerprint import Fingerprint

    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "sensor", "test", "circuit-a-uid", suggested_object_id="circuit_a_power"
    )
    assert row.entity_id == CIRCUIT_A
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [
                Fingerprint(label="Dryer", circuit=CIRCUIT_A, count=9).to_dict()
            ]
        },
    }
    for entity, value in (
        (MAINS, 155.0),
        (CIRCUIT_A, 100.0),
        ("sensor.circuit_b_power", 50.0),
    ):
        hass.states.async_set(entity, value, POWER)

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_appliance_{CIRCUIT_A}"
    )

    registry.async_update_entity(CIRCUIT_A, new_entity_id="sensor.garage_power")
    hass.states.async_set("sensor.garage_power", 100.0, POWER)
    await hass.async_block_till_done()

    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_appliance_sensor.garage_power"
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{config_entry.entry_id}_appliance_{CIRCUIT_A}"
        )
        is None
    )


async def test_renaming_the_mains_moves_the_mains_setting(
    hass: HomeAssistant, config_entry, powered
):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    hass.states.async_remove(MAINS)
    row = registry.async_get_or_create(
        "sensor", "test", "mains-uid", suggested_object_id="mains_power"
    )
    assert row.entity_id == MAINS
    hass.states.async_set(MAINS, 155.0, POWER)

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry.async_update_entity(MAINS, new_entity_id="sensor.panel_total_power")
    hass.states.async_set("sensor.panel_total_power", 155.0, POWER)
    await hass.async_block_till_done()

    assert config_entry.data[CONF_MAINS] == "sensor.panel_total_power"


async def test_a_registry_update_that_is_not_a_rename_is_ignored(
    hass: HomeAssistant, config_entry
):
    """Every registry change on a tracked entity fires this listener; only an
    entity id change means anything to the stored library."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "sensor", "test", "circuit-a-uid", suggested_object_id="circuit_a_power"
    )
    assert row.entity_id == CIRCUIT_A
    for entity, value in (
        (MAINS, 155.0),
        (CIRCUIT_A, 100.0),
        ("sensor.circuit_b_power", 50.0),
    ):
        hass.states.async_set(entity, value, POWER)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry.async_update_entity(CIRCUIT_A, name="Dishwasher circuit")
    await hass.async_block_till_done()

    assert config_entry.data[CONF_CIRCUITS] == [CIRCUIT_A, "sensor.circuit_b_power"]


async def test_a_stray_nameless_device_from_an_earlier_version_is_removed(
    hass: HomeAssistant, config_entry, powered
):
    """Returning another device's identifiers in DeviceInfo produced a second,
    nameless device rather than merging. Nine of them, one per mapping."""
    from homeassistant.helpers import device_registry as dr

    config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    stray = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("zha", "64:02:8f:ff:fe:a1:ca:4a")},
    )
    assert stray.name is None
    # A nameless row that IS this integration's own service device must be
    # left alone: it is named when the first entity attaches to it.
    unnamed_service_device = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, config_entry.entry_id)},
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Nothing else owned it, so removing this entry's claim removed the row.
    assert devices.async_get(stray.id) is None
    assert devices.async_get(unnamed_service_device.id) is not None
    # The integration's own service device is untouched.
    ours = devices.async_get_device({(DOMAIN, config_entry.entry_id)})
    assert ours is not None and ours.name == "Power Fingerprint"


async def test_setup_waits_rather_than_stranding_paused_automations(
    hass: HomeAssistant, config_entry, powered
):
    """A missing automation.turn_on must not lose the record of what was
    switched off: ConfigEntryNotReady keeps it and retries.

    The automation component is a manifest dependency, so the only way it is
    absent at this moment is a failure to set it up - simulated by answering
    "no such service" for that one call.
    """
    from homeassistant.core import ServiceRegistry

    real_has_service = ServiceRegistry.has_service

    def _no_automation(self, domain, service):
        if (domain, service) == ("automation", "turn_on"):
            return False
        return real_has_service(self, domain, service)

    with (
        patch.object(ServiceRegistry, "has_service", _no_automation),
        patch(
            "custom_components.power_fingerprint.store.FingerprintStore"
            ".orphaned_pauses",
            return_value=["automation.hall_motion"],
        ),
    ):
        config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    # The card says what is waiting and how many, not a bare key.
    assert "1 automation(s) paused for a probe" in config_entry.reason


async def test_a_renamed_switch_in_a_contradiction_pair_is_followed(
    hass: HomeAssistant, config_entry
):
    """The pair field is `switch.x: sensor.y` per line. Split any other way,
    the switch was never tracked and a rename silently broke the check."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "switch", "test", "heater-uid", suggested_object_id="heater"
    )
    assert row.entity_id == "switch.heater"
    for entity, value in (
        (MAINS, 155.0),
        (CIRCUIT_A, 100.0),
        ("sensor.circuit_b_power", 50.0),
    ):
        hass.states.async_set(entity, value, POWER)
    hass.states.async_set("switch.heater", "on")

    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "contradiction_pairs": "switch.heater: sensor.circuit_b_power",
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry.async_update_entity("switch.heater", new_entity_id="switch.space_heater")
    hass.states.async_set("switch.space_heater", "on")
    await hass.async_block_till_done()

    assert config_entry.data["contradiction_pairs"] == (
        "switch.space_heater: sensor.circuit_b_power"
    )
