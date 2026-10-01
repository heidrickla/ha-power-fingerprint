"""record_breaker_evidence: hand-gathered outage evidence added to a recorded trip."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.power_fingerprint.const import DOMAIN, EVENT_BREAKER_TRIP

CIRCUIT = "sensor.circuit_a_power"
START = "2026-09-30T23:30:46.655261+00:00"
END = "2026-09-30T23:35:37.669391+00:00"


async def _loaded(hass: HomeAssistant, config_entry):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = config_entry.runtime_data
    await runtime.store.async_record_trip(
        {
            "circuit": CIRCUIT,
            "start": START,
            "end": END,
            "confirmed": [],
            "suspected": [],
        },
        "",
    )
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    for name, domain in (("couch", "light"), ("outlet", "switch"), ("sub", "switch")):
        device = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("demo", name)}, name=name
        )
        registry.async_get_or_create(
            domain, "demo", name, device_id=device.id, suggested_object_id=name
        )
    return runtime


async def _call(hass, **data):
    return await hass.services.async_call(
        DOMAIN,
        "record_breaker_evidence",
        {"circuit": CIRCUIT, "start": START, **data},
        blocking=True,
        return_response=True,
    )


async def test_hand_evidence_is_added_to_the_trip(
    hass: HomeAssistant, config_entry, powered
):
    runtime = await _loaded(hass, config_entry)
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    response = await _call(
        hass,
        devices=[
            {
                "entity": "light.couch",
                "signal": "zwave_node_dead",
                "down_at": "2026-09-30T23:31:39.734+00:00",
                "back_at": "2026-09-30T23:35:33.146+00:00",
            },
            {
                "entity": "switch.outlet",
                "signal": "zigbee_command",
                "failed_at": "2026-09-30T23:31:22+00:00",
                "answered_at": "2026-10-01T01:10:00+00:00",
            },
        ],
        answered=["switch.loose"],
        recovered=[
            {
                "entity": "switch.sub",
                "failed_at": "2026-09-30T23:06:33+00:00",
                "answered_at": "2026-09-30T23:37:11+00:00",
            }
        ],
    )
    rows = runtime.store.assignments()
    assert rows["light.couch"]["source"] == "breaker"
    assert rows["light.couch"]["evidence"]["result"] == "suspected"
    assert rows["light.couch"]["evidence"]["imported"] is True
    assert rows["switch.outlet"]["source"] == "probe"
    assert rows["switch.outlet"]["confidence"] == "inferred"
    assert rows["switch.outlet"]["evidence"]["probe_kind"] == "breaker_reachability"
    # A device that was failing before the trip is noted, never placed.
    assert "switch.sub" not in rows
    trip = response["trip"]
    assert trip["suspected"] == ["light.couch", "switch.outlet"]
    assert list(trip["outage"]["recovered"]) == ["switch.sub"]
    # An entity with no device is keyed by itself.
    assert trip["outage"]["answered"] == ["switch.loose"]
    assert [e.data["amended"] for e in events] == [True]


@pytest.mark.parametrize(
    ("start", "down_at", "back_at"),
    [
        # No trip starts then.
        (
            "2026-09-30T22:00:00+00:00",
            "2026-09-30T23:31:00+00:00",
            "2026-09-30T23:35:40+00:00",
        ),
        # Down before the circuit went dead.
        (START, "2026-09-30T23:30:00+00:00", "2026-09-30T23:35:40+00:00"),
        # Back while the circuit was still dead.
        (START, "2026-09-30T23:31:00+00:00", "2026-09-30T23:33:00+00:00"),
    ],
)
async def test_evidence_that_does_not_fit_the_trip_is_refused(
    hass: HomeAssistant, config_entry, powered, start, down_at, back_at
):
    runtime = await _loaded(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "record_breaker_evidence",
            {
                "circuit": CIRCUIT,
                "start": start,
                "devices": [
                    {
                        "entity": "light.couch",
                        "signal": "zwave_node_dead",
                        "down_at": down_at,
                        "back_at": back_at,
                    }
                ],
            },
            blocking=True,
            return_response=True,
        )
    assert runtime.store.assignments() == {}
    assert "outage" not in runtime.store.breaker_trips()[-1]


async def test_answers_alone_are_noted_without_an_event(
    hass: HomeAssistant, config_entry, powered
):
    runtime = await _loaded(hass, config_entry)
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    response = await _call(hass, answered=["light.couch"])
    assert response["trip"]["outage"]["answered"] == ["light.couch"]
    assert response["trip"]["suspected"] == []
    assert runtime.store.assignments() == {}
    assert events == []


@pytest.mark.parametrize(
    ("failed_at", "answered_at"),
    [
        # Refused after the circuit went dead: that is a casualty's evidence.
        ("2026-09-30T23:31:30+00:00", "2026-09-30T23:37:11+00:00"),
        # Answered while the circuit was still dead.
        ("2026-09-30T23:06:33+00:00", "2026-09-30T23:33:00+00:00"),
    ],
)
async def test_a_recovered_device_that_does_not_fit_the_trip_is_refused(
    hass: HomeAssistant, config_entry, powered, failed_at, answered_at
):
    runtime = await _loaded(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            recovered=[
                {
                    "entity": "switch.sub",
                    "failed_at": failed_at,
                    "answered_at": answered_at,
                }
            ],
        )
    assert "outage" not in runtime.store.breaker_trips()[-1]


async def test_a_trip_with_no_return_takes_answers_but_no_casualties(
    hass: HomeAssistant, config_entry, powered
):
    runtime = await _loaded(hass, config_entry)
    # The same trip, its circuit still dead: it replaces the ended one.
    await runtime.store.async_record_trip(
        {
            "circuit": CIRCUIT,
            "start": START,
            "end": None,
            "confirmed": [],
            "suspected": [],
        },
        "9999",
    )
    assert len(runtime.store.breaker_trips()) == 1
    with pytest.raises(ServiceValidationError) as refused:
        await _call(
            hass,
            devices=[
                {
                    "entity": "light.couch",
                    "signal": "zwave_node_dead",
                    "down_at": "2026-09-30T23:31:39+00:00",
                    "back_at": "2026-09-30T23:35:33+00:00",
                }
            ],
        )
    assert refused.value.translation_key == "trip_not_ended"
    assert runtime.store.assignments() == {}
    response = await _call(hass, answered=["light.couch"])
    assert response["trip"]["outage"]["answered"] == ["light.couch"]


async def test_a_second_import_adds_to_the_first(
    hass: HomeAssistant, config_entry, powered
):
    await _loaded(hass, config_entry)
    await _call(
        hass,
        devices=[
            {
                "entity": "light.couch",
                "signal": "zwave_node_dead",
                "down_at": "2026-09-30T23:31:39+00:00",
                "back_at": "2026-09-30T23:35:33+00:00",
            }
        ],
        answered=["switch.outlet"],
    )
    response = await _call(
        hass,
        recovered=[
            {
                "entity": "switch.sub",
                "failed_at": "2026-09-30T23:06:33+00:00",
                "answered_at": "2026-09-30T23:37:11+00:00",
            }
        ],
    )
    outage = response["trip"]["outage"]
    assert list(outage["casualties"]) == ["light.couch"]
    assert outage["answered"] == ["switch.outlet"]
    assert list(outage["recovered"]) == ["switch.sub"]
