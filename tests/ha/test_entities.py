"""The entities, and the derived numbers behind them."""

from homeassistant.core import HomeAssistant

from custom_components.power_fingerprint.const import DOMAIN


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


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
