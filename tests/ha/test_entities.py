"""The entities, and the derived numbers behind them."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.power_fingerprint.const import DOMAIN
from custom_components.power_fingerprint.fingerprint import Fingerprint

# Declared locally rather than imported from conftest: tests/ha is not a
# package, so a relative import has no parent to resolve against.
MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def test_unmonitored_load_is_mains_minus_circuits(
    hass: HomeAssistant, config_entry, powered
):
    """155 W mains against circuits summing to 150 W leaves 5 W unaccounted."""
    await _setup(hass, config_entry)
    state = hass.states.get("sensor.power_fingerprint_unmonitored_load")
    assert state is not None
    assert float(state.state) == 5.0
    assert state.attributes["mains_w"] == 155.0
    assert state.attributes["circuits_w"] == 150.0


async def test_coverage_fault_is_off_on_a_well_clamped_panel(
    hass: HomeAssistant, config_entry, powered
):
    await _setup(hass, config_entry)
    state = hass.states.get("binary_sensor.power_fingerprint_ct_coverage_fault")
    assert state is not None
    assert state.state == "off"


async def test_coverage_fault_is_suppressed_on_an_idle_panel(
    hass: HomeAssistant, config_entry, powered
):
    """Below ~200 W the percentage is dominated by rounding, so a fault there
    would be noise rather than signal."""
    powered(mains=100.0, a=1.0, b=1.0)  # 98 W unaccounted, but tiny in absolute terms
    coordinator = await _setup(hass, config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.power_fingerprint_ct_coverage_fault")
    assert state.state == "off"


async def test_a_reversed_or_missing_ct_raises_the_fault(
    hass: HomeAssistant, config_entry, powered
):
    """A clamp coming off shows as circuits no longer summing to the mains."""
    powered(mains=2000.0, a=100.0, b=50.0)  # 1850 W unaccounted
    coordinator = await _setup(hass, config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.power_fingerprint_ct_coverage_fault")
    assert state.state == "on"


async def test_negative_remainder_also_faults(
    hass: HomeAssistant, config_entry, powered
):
    """Circuits summing to MORE than the mains usually means a CT is reversed or
    double-counting a leg - just as wrong as a positive remainder."""
    powered(mains=500.0, a=900.0, b=900.0)
    coordinator = await _setup(hass, config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get("binary_sensor.power_fingerprint_ct_coverage_fault").state
        == "on"
    )


async def test_no_appliance_sensor_until_something_is_named(
    hass: HomeAssistant, config_entry, powered
):
    """An appliance sensor per configured circuit would be 27 entities reading
    'unknown' on a real panel - entity spam dressed up as a feature."""
    await _setup(hass, config_entry)
    appliance = [
        e for e in hass.states.async_entity_ids("sensor") if e.endswith("_appliance")
    ]
    assert appliance == []


async def test_an_appliance_sensor_appears_once_a_shape_is_named(
    hass: HomeAssistant, config_entry, powered
):
    """Added by the coordinator listener, without a reload."""
    coordinator = await _setup(hass, config_entry)
    await config_entry.runtime_data.store.async_replace_circuit(
        CIRCUIT_A, [Fingerprint(label="Dryer", circuit=CIRCUIT_A, count=10)]
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_appliance_{CIRCUIT_A}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != "unavailable"
    assert state.attributes["circuit"] == CIRCUIT_A


async def test_an_appliance_sensor_goes_unavailable_with_its_circuit(
    hass: HomeAssistant, config_entry, powered
):
    """One circuit dropping out does not blank the aggregates - the coverage
    sensor reports it - but the sensor ABOUT that circuit has nothing to say
    and must not read `unknown`, which means "an unrecognised shape"."""
    coordinator = await _setup(hass, config_entry)
    await config_entry.runtime_data.store.async_replace_circuit(
        CIRCUIT_A, [Fingerprint(label="Dryer", circuit=CIRCUIT_A, count=10)]
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_appliance_{CIRCUIT_A}"
    )
    assert hass.states.get(entity_id).state != "unavailable"

    hass.states.async_set(
        CIRCUIT_A, "unavailable", {"device_class": "power", "unit_of_measurement": "W"}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    # The aggregates stay up: only one of three sources is gone.
    assert (
        hass.states.get("sensor.power_fingerprint_unmonitored_load").state
        != "unavailable"
    )

    powered()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != "unavailable"


async def test_entities_group_under_one_service_device(
    hass: HomeAssistant, config_entry, powered
):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    entry = registry.async_get("sensor.power_fingerprint_unmonitored_load")
    assert entry is not None and entry.device_id is not None

    devices = dr.async_get(hass)
    device = devices.async_get(entry.device_id)
    assert device.name == "Power Fingerprint"
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_a_kilowatt_mains_against_watt_circuits_is_not_a_coverage_fault(
    hass: HomeAssistant, config_entry, powered
):
    """The exact configuration found on the development install.

    `emporiavue_total_power` reports kW while all 27 circuits report W. Read
    raw, a healthy 1,380 W panel arrives as 1.38 and the coverage check sees a
    catastrophic negative remainder that never clears - a permanent fault on a
    panel with nothing wrong with it. Converted at ingestion it is ordinary.
    """
    hass.states.async_set(
        MAINS,
        0.155,  # 155 W, expressed the way a kW sensor expresses it
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
        },
    )
    coordinator = await _setup(hass, config_entry)
    await coordinator.async_refresh()

    assert coordinator.data["coverage"]["mains_w"] == 155.0
    assert hass.states.get("sensor.power_fingerprint_unmonitored_load").state == "5.0"
    fault = hass.states.get("binary_sensor.power_fingerprint_ct_coverage_fault")
    assert fault is not None and fault.state == "off"
    assert coordinator.source_profile()["units"][MAINS] == "kW"
    assert coordinator.source_profile()["units"][CIRCUIT_A] == "W"
    assert coordinator.source_profile()["units"][CIRCUIT_B] == "W"


async def test_a_sensor_reporting_a_non_power_unit_is_dropped_and_named(
    hass: HomeAssistant, config_entry, powered, caplog
):
    """Dropped rather than trusted, and named once rather than every poll."""
    coordinator = await _setup(hass, config_entry)
    hass.states.async_set(
        CIRCUIT_B,
        50.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "A",
        },
    )
    await coordinator.async_refresh()

    assert CIRCUIT_B in coordinator.source_profile()["rejected_non_power_units"]
    assert caplog.text.count("which is not a power unit") == 1
    await coordinator.async_refresh()
    assert caplog.text.count("which is not a power unit") == 1


# --- absence detection -----------------------------------------------------


async def test_silent_appliance_is_a_primary_entity_not_a_diagnostic(
    hass: HomeAssistant, config_entry, powered
):
    """It is a fact about the house, not about the measurement.

    Coverage and contradiction say "my own view is suspect" and belong in the
    diagnostic section. A freezer that stopped is the reason someone installed
    this at all, and must not be filed behind a collapsed panel.
    """
    from homeassistant.helpers import entity_registry as er

    await _setup(hass, config_entry)
    registry = er.async_get(hass)
    silent = registry.async_get("binary_sensor.power_fingerprint_silent_appliance")
    coverage = registry.async_get("binary_sensor.power_fingerprint_ct_coverage_fault")
    assert silent is not None
    assert silent.entity_category is None
    assert coverage.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_silent_appliance_is_unknown_with_nothing_learned(
    hass: HomeAssistant, config_entry, powered
):
    """Not `off`. `off` would claim every appliance is fine; nothing is known."""
    await _setup(hass, config_entry)
    state = hass.states.get("binary_sensor.power_fingerprint_silent_appliance")
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["watching"] == 0


# --- found by installing, not by any test ----------------------------------


async def test_diagnostics_download_does_not_raise(
    hass: HomeAssistant, config_entry, powered
):
    """`window_sizes()` returns COUNTS; diagnostics called len() on them.

    TypeError, and the whole diagnostics download returned HTTP 500. Nothing
    caught it because this suite had never been run; installing did.
    """
    from custom_components.power_fingerprint.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup(hass, config_entry)
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert isinstance(report["window_filled"], dict)
    assert all(isinstance(v, int) for v in report["window_filled"].values())
    assert "window_hours" in report
    assert "source" in report


async def test_standby_refuses_to_answer_from_a_cold_window(
    hass: HomeAssistant, config_entry, powered
):
    """A 5th percentile over four minutes is a different quantity, not a rough one.

    On the live install a window holding only the minute after a restart
    reported 5,017 W of "standby" while the true 24-hour figure was near zero,
    because the central AC happened to be running. It looked exactly as
    authoritative as a real number.
    """
    coordinator = await _setup(hass, config_entry)
    assert coordinator.window_hours() < 1.0
    state = hass.states.get("sensor.power_fingerprint_standby_power")
    assert state.state == "unknown"
    assert state.attributes["window_ready"] is False
    assert hass.states.get("sensor.power_fingerprint_standby_annual_cost").state == (
        "unknown"
    )
