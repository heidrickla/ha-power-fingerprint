"""verify_circuit: the only action in the integration that touches the house.

Every probe here is against mock switch services, but the decision path is the
real one - the meter discovery, the safety refusals, the restore in `finally`,
the grading and what gets written to the store.
"""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.power_fingerprint.const import DOMAIN

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
LAMP = "switch.porch_lamp"
LAMP_METER = "sensor.porch_lamp_power"

POWER = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}


@pytest.fixture
def instant_settle():
    """Run the settle windows without waiting them out.

    The probe sleeps for the settle window several times per probe; the code
    under test is what happens either side of those sleeps.
    """
    real_sleep = asyncio.sleep

    async def _no_wait(_delay, *args, **kwargs):
        return await real_sleep(0)

    with patch("asyncio.sleep", _no_wait):
        yield


@pytest.fixture
def lamp(hass):
    """A switch on a device that meters itself, owned by another integration."""
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("demo", "porch-lamp")},
        name="Porch lamp",
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        "demo",
        "porch-lamp",
        device_id=device.id,
        suggested_object_id="porch_lamp",
    )
    registry.async_get_or_create(
        "sensor",
        "demo",
        "porch-lamp-power",
        device_id=device.id,
        suggested_object_id="porch_lamp_power",
    )
    hass.states.async_set(LAMP, "off")
    hass.states.async_set(LAMP_METER, 0.0, POWER)
    return device


@pytest.fixture
def switching(hass, powered):
    """Make the mock switch services actually move a circuit.

    Without this the probe measures nothing and every test would pass through
    the same "no circuit moved" path.
    """

    def _turn(watts):
        async def _handle(call):
            hass.states.async_set(LAMP, "on" if watts else "off")
            hass.states.async_set(LAMP_METER, float(watts), POWER)
            powered(mains=155.0 + watts, a=100.0 + watts)

        return _handle

    hass.services.async_register("switch", "turn_on", _turn(100.0))
    hass.services.async_register("switch", "turn_off", _turn(0.0))


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def _verify(hass, **data):
    return await hass.services.async_call(
        DOMAIN,
        "verify_circuit",
        {"device": LAMP, **data},
        blocking=True,
        return_response=True,
    )


async def test_a_probe_that_agrees_with_itself_places_the_device(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """Two probes, the device's own meter to check magnitude against, and a
    circuit that moved by what the device said it drew."""
    coordinator = await _setup(hass, config_entry)

    response = await _verify(hass, probes=2, settle=10)

    assert response["circuit"] == CIRCUIT_A
    assert response["confidence"] == "measured"
    assert response["device_metered"] is True
    assert response["power_sensor"] == LAMP_METER
    assert response["power_sensor_source"] == "discovered"
    assert response["automations_fired_during_probe"] == []
    assert len(response["probes"]) == 2
    assert response["probes"][0]["expected_w"] == 100.0

    # Written down, so the answer survives a restart and shows on the device.
    row = config_entry.runtime_data.store.assignments()[LAMP]
    assert row["circuit"] == CIRCUIT_A
    assert row["source"] == "probe"
    assert row["evidence"]["agreed"] == 2

    # And the device's own page gets an entity saying which breaker it is on.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_circuit_{LAMP}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state.attributes["established_by"] == "probe"
    assert state.attributes["measured_entity"] == LAMP
    assert er.async_get(hass).async_get(entity_id).device_id == lamp.id


async def test_the_device_is_left_exactly_as_it_was_found(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """A probe that leaves the house in a different state is a bug whatever
    it learned."""
    await _setup(hass, config_entry)
    await _verify(hass, probes=1, settle=10)
    assert hass.states.get(LAMP).state == "off"


async def test_a_device_that_is_already_on_is_probed_by_switching_it_off(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """Insisting on "on" would refuse to verify anything already running."""
    hass.states.async_set(LAMP, "on")
    hass.states.async_set(LAMP_METER, 100.0, POWER)
    powered(mains=255.0, a=200.0)
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=1, settle=10, force=True)

    assert response["circuit"] == CIRCUIT_A
    # One probe is never `measured`, however clean it looked.
    assert response["confidence"] == "inferred"
    assert hass.states.get(LAMP).state == "on"


async def test_a_single_probe_is_never_measured(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """With one probe "all probes agreed" is vacuously true."""
    await _setup(hass, config_entry)
    response = await _verify(hass, probes=1, settle=10)
    assert response["circuit"] == CIRCUIT_A
    assert response["confidence"] == "inferred"


async def test_without_the_devices_own_meter_the_result_is_only_inferred(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """Ranking falls back to "which circuit moved most", which cannot reject
    an unrelated load of similar size."""
    er.async_get(hass).async_remove(LAMP_METER)
    hass.states.async_remove(LAMP_METER)
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=2, settle=10)

    assert response["power_sensor"] is None
    assert response["power_sensor_source"].startswith("none")
    assert response["device_metered"] is False
    assert response["confidence"] == "inferred"


async def test_a_power_sensor_given_by_the_caller_is_used_as_given(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    await _setup(hass, config_entry)
    response = await _verify(hass, probes=1, settle=10, power_sensor=LAMP_METER)
    assert response["power_sensor_source"] == "given"


async def test_a_probe_that_moves_nothing_answers_nothing(
    hass: HomeAssistant, config_entry, powered, lamp, instant_settle
):
    """No handler moves a circuit here, so the honest answer is no answer -
    and nothing is written over whatever was known before."""
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=2, settle=10)

    assert response["circuit"] is None
    assert response["confidence"] == "unknown"
    assert config_entry.runtime_data.store.assignments() == {}


# --- refusals --------------------------------------------------------------


async def test_a_lock_is_not_a_thing_to_switch_for_a_measurement(
    hass: HomeAssistant, config_entry, powered
):
    hass.states.async_set("lock.front_door", "locked")
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "verify_circuit",
            {"device": "lock.front_door"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "may_not_probe"


async def test_a_config_switch_is_refused_by_its_registry_category(
    hass: HomeAssistant, config_entry, powered, lamp
):
    """A Z-Wave dimmer publishes its settings in the same domain as its load;
    flipping `invert_switch` reconfigures the device and moves no current."""
    er.async_get(hass).async_update_entity(
        LAMP, entity_category=er.EntityCategory.CONFIG
    )
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await _verify(hass, settle=10)
    assert err.value.translation_key == "may_not_probe"


async def test_a_device_that_is_not_available_is_refused(
    hass: HomeAssistant, config_entry, powered, lamp
):
    hass.states.async_set(LAMP, "unavailable")
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await _verify(hass, settle=10)
    assert err.value.translation_key == "device_unavailable"


async def test_a_battery_device_is_refused_before_the_settle_time_is_spent(
    hass: HomeAssistant, config_entry, powered, lamp
):
    """It draws no mains current, so no CT can ever see it switch."""
    er.async_get(hass).async_get_or_create(
        "sensor",
        "demo",
        "porch-lamp-battery",
        device_id=lamp.id,
        suggested_object_id="porch_lamp_battery",
    )
    hass.states.async_set("sensor.porch_lamp_battery", 90, {"device_class": "battery"})
    hass.states.async_remove(LAMP_METER)
    er.async_get(hass).async_remove(LAMP_METER)
    await _setup(hass, config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await _verify(hass, settle=10)
    assert err.value.translation_key == "battery_powered"


async def test_a_device_carrying_a_load_is_not_switched_off_to_measure_it(
    hass: HomeAssistant, config_entry, powered, lamp
):
    """The domain allowlist cannot tell a lamp from a computer's power feed."""
    hass.states.async_set(LAMP, "on")
    hass.states.async_set(LAMP_METER, 800.0, POWER)
    await _setup(hass, config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await _verify(hass, settle=10)
    assert err.value.translation_key == "carrying_load"


async def test_force_probes_it_anyway(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    hass.states.async_set(LAMP, "on")
    hass.states.async_set(LAMP_METER, 800.0, POWER)
    await _setup(hass, config_entry)
    response = await _verify(hass, settle=10, probes=1, force=True)
    assert response["device"] == LAMP


# --- automations -----------------------------------------------------------


async def test_an_automation_that_fired_during_the_probe_downgrades_it(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """The power trace alone cannot show that something else moved the device."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "porch",
                "trigger": {"platform": "state", "entity_id": LAMP},
                "action": {
                    "service": "persistent_notification.create",
                    "data": {"message": "porch moved"},
                },
            }
        },
    )
    await hass.async_block_till_done()
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=2, settle=10)

    assert response["automations_fired_during_probe"]
    assert response["confidence"] == "suspect"


async def test_pausing_switches_the_safe_automations_off_and_back_on(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """Refusals are returned rather than swallowed: "we left three automations
    running" changes how much to trust the measurement."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "porch light",
                    "trigger": {"platform": "state", "entity_id": LAMP},
                    "action": {"service": "switch.turn_on", "entity_id": LAMP},
                },
                {
                    "alias": "front door",
                    "trigger": {"platform": "state", "entity_id": LAMP},
                    "action": {"service": "lock.lock", "entity_id": "lock.front"},
                },
            ]
        },
    )
    await hass.async_block_till_done()
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=1, settle=10, pause_automations=True)

    assert response["automations_paused"] == ["automation.porch_light"]
    refused = response["automations_refused_as_unsafe"]
    assert [row["automation"] for row in refused] == ["automation.front_door"]
    # Switched back on, and the record of the pause cleared.
    assert hass.states.get("automation.porch_light").state == "on"
    assert config_entry.runtime_data.store.orphaned_pauses() == []


async def test_a_settle_window_shorter_than_the_meter_reports_is_called_out(
    hass: HomeAssistant,
    config_entry,
    powered,
    lamp,
    switching,
    instant_settle,
    history,
    cycling,
):
    """Both reads come from the same stale value, so the probe returns a
    confident "no change" - a wrong answer that looks like a clean one."""
    history[CIRCUIT_A] = [(stamp, watts) for stamp, watts in cycling(1200.0, step_s=30)]
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=1, settle=10)

    assert response["notes"]
    assert "short for a meter reporting every" in response["notes"][0]


async def test_a_meter_that_cannot_be_read_is_treated_as_no_meter(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """A sensor that has gone, or that reports text, gives no magnitude to
    check the circuits against - and must not raise on the way to saying so."""
    hass.states.async_set("sensor.broken_meter", "unavailable")
    await _setup(hass, config_entry)

    gone = await _verify(hass, probes=1, settle=10, power_sensor="sensor.no_such")
    broken = await _verify(
        hass, probes=1, settle=10, power_sensor="sensor.broken_meter"
    )

    assert gone["device_metered"] is False
    assert broken["device_metered"] is False


async def test_an_automation_that_is_already_off_is_left_alone(
    hass: HomeAssistant, config_entry, powered, lamp, switching, instant_settle
):
    """It was off before the probe, so switching it on afterwards would be
    this integration changing something nobody asked it to change."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "porch light",
                "trigger": {"platform": "state", "entity_id": LAMP},
                "action": {"service": "switch.turn_on", "entity_id": LAMP},
            }
        },
    )
    await hass.async_block_till_done()
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": "automation.porch_light"}, blocking=True
    )
    await _setup(hass, config_entry)

    response = await _verify(hass, probes=1, settle=10, pause_automations=True)

    assert response["automations_paused"] == []
    assert hass.states.get("automation.porch_light").state == "off"
