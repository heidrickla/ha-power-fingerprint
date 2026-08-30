"""Setup, unload, and the orphaned-pause backstop."""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.power_fingerprint.const import DOMAIN


async def test_setup_and_unload(hass: HomeAssistant, config_entry, powered):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert DOMAIN in hass.data

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_services_are_registered(hass: HomeAssistant, config_entry, powered):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for service in ("learn", "label", "verify_circuit"):
        assert hass.services.has_service(DOMAIN, service), service


async def test_a_dead_probe_does_not_leave_automations_switched_off(
    hass: HomeAssistant, config_entry, powered
):
    """⛔ THE SAFETY BACKSTOP, AND THE REASON IT EXISTS.

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
            ".async_take_orphaned_pauses",
            return_value=orphaned,
        ),
        patch.object(
            hass.services, "async_call", wraps=hass.services.async_call
        ) as call,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    turned_on = [
        c.args[2]["entity_id"]
        for c in call.call_args_list
        if c.args[:2] == ("automation", "turn_on")
    ]
    assert set(turned_on) == set(orphaned), turned_on


async def test_nothing_is_touched_when_no_pauses_are_orphaned(
    hass: HomeAssistant, config_entry, powered
):
    """The backstop must not fire on a normal startup."""
    config_entry.add_to_hass(hass)
    with patch.object(
        hass.services, "async_call", wraps=hass.services.async_call
    ) as call:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert not [
        c for c in call.call_args_list if c.args[:2] == ("automation", "turn_on")
    ]
