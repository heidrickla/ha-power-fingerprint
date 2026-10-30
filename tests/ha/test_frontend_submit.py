"""Each setup form submitted the way the dialog sends it.

The data comes from the schema the frontend receives, through a port of its
initial-data code (frontend_form.py); the Advanced section is never opened.
"""

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.power_fingerprint.const import (
    CONF_BREAKER_PROBE,
    CONF_CIRCUITS,
    CONF_CONFIDENCE,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DOMAIN,
    SECTION_ADVANCED,
)

from .frontend_form import (
    all_required_filled,
    frontend_submission,
    initial_data,
    serialize,
)

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
TYPED = {CONF_MAINS: MAINS, CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B]}
TUNING = {
    CONF_TOLERANCE: 5.0,
    CONF_CONFIDENCE: "balanced",
    CONF_BREAKER_PROBE: True,
    CONF_PAIRS: "",
}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit(hass: HomeAssistant, result, typed=None):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, typed)
    )


async def test_the_sensors_picked_and_advanced_left_collapsed_set_up(
    hass: HomeAssistant, powered
):
    result = await _submit(hass, await _start(hass), TYPED)
    assert result["step_id"] == "price"
    result = await _submit(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**TYPED, CONF_PRICE: 0.13, **TUNING}


async def test_sensors_named_by_the_energy_dashboard_need_one_submit(
    hass: HomeAssistant, powered
):
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_rate": MAINS, "number_energy_price": 0.145}
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.a_energy", "stat_rate": CIRCUIT_A},
            {"stat_consumption": "sensor.b_energy", "stat_rate": CIRCUIT_B},
        ],
    }

    async def _manager(hass):
        return SimpleNamespace(data=prefs)

    with patch("homeassistant.components.energy.data.async_get_manager", _manager):
        result = await _submit(hass, await _start(hass))
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**TYPED, CONF_PRICE: 0.145, **TUNING}


async def test_a_mains_sensor_nobody_guessed_opens_empty_and_required(
    hass: HomeAssistant, powered
):
    # With no guess the dialog must hold Submit back itself, not send null.
    result = await _start(hass)
    schema = serialize(result["data_schema"])
    data = initial_data(schema)
    assert data[CONF_MAINS] == ""
    assert not all_required_filled(schema, data)
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_the_first_screen_shown_again_after_an_error_submits(
    hass: HomeAssistant, powered
):
    result = await _submit(
        hass, await _start(hass), {**TYPED, CONF_MAINS: "sensor.gone"}
    )
    assert result["errors"] == {CONF_MAINS: "mains_missing"}
    result = await _submit(hass, result, {CONF_MAINS: MAINS})
    assert result["step_id"] == "price"
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_the_no_sensors_step_checks_again_on_submit(hass: HomeAssistant, powered):
    for entity in (MAINS, CIRCUIT_A, CIRCUIT_B):
        hass.states.async_remove(entity)
    result = await _start(hass)
    assert result["step_id"] == "no_sensors"
    result = await _submit(hass, result)
    assert result["errors"] == {"base": "still_no_sensors"}
    powered()
    result = await _submit(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_what_was_typed_in_advanced_comes_back_after_an_error(
    hass: HomeAssistant, powered
):
    result = await _start(hass)
    sent = frontend_submission(result, {**TYPED, CONF_MAINS: "sensor.gone"})
    sent[SECTION_ADVANCED][CONF_TOLERANCE] = 9.0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sent)
    assert result["errors"] == {CONF_MAINS: "mains_missing"}

    # The section reopens holding the typed tolerance, and Submit sends it back.
    again = frontend_submission(result, {CONF_MAINS: MAINS})
    assert again[SECTION_ADVANCED][CONF_TOLERANCE] == 9.0
    result = await hass.config_entries.flow.async_configure(result["flow_id"], again)
    assert result["step_id"] == "price"
    hass.config_entries.flow.async_abort(result["flow_id"])
