"""Outage evidence during a live trip: Zigbee probes, Z-Wave node status, a TV.

Timings follow a measured trip: TV off 14 s after the circuit read
dead, nodes dead from 53 s, the circuit live again at 200 s here, the node
alive 4 s before that reading, and the TV back on 60 s after it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.power_fingerprint import probes
from custom_components.power_fingerprint.const import (
    CONF_BREAKER_PROBE,
    EVENT_BREAKER_TRIP,
)
from custom_components.power_fingerprint.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .test_breaker_trips import (  # noqa: F401 - fixtures
    CIRCUIT_A,
    CIRCUIT_B,
    POWER,
    _arm,
    _setup,
    _tick,
    house,
    stats,
)

pytestmark = pytest.mark.usefixtures("stats")

OUTLET_IEEE = "aa:bb:cc:dd:ee:ff:00:01"
ELSEWHERE_IEEE = "aa:bb:cc:dd:ee:ff:00:02"
NODE_STATUS = "sensor.couch_node_status"
NODE_PING = "button.couch_ping"


class DeliveryError(Exception):
    """Named like zigpy's, which is what `probes.answered` reads."""


@pytest.fixture
def radios(hass, request, monkeypatch):
    """A Zigbee outlet fed by A, one fed elsewhere, and a Z-Wave node on A."""
    request.getfixturevalue("house")
    monkeypatch.setattr(probes, "PING_GAP_S", 0.0)
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    made = {}
    for name, platform, rows in (
        (
            "outlet",
            "zha",
            [
                ("switch", f"{OUTLET_IEEE}-1-6", None),
                ("button", f"{OUTLET_IEEE}-1-3", EntityCategory.DIAGNOSTIC),
            ],
        ),
        (
            "elsewhere",
            "zha",
            [
                ("switch", f"{ELSEWHERE_IEEE}-1-6", None),
                ("button", f"{ELSEWHERE_IEEE}-1-3", EntityCategory.DIAGNOSTIC),
            ],
        ),
        (
            "couch",
            "zwave_js",
            [
                ("light", "123.5-38-0-currentValue", None),
                ("button", "123.5.ping", EntityCategory.CONFIG),
                ("sensor", "123.5.node_status", EntityCategory.DIAGNOSTIC),
            ],
        ),
    ):
        device = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("demo", name)}, name=name
        )
        made[name] = device
        for domain, unique_id, category in rows:
            suffix = {"button": "ping" if platform == "zwave_js" else "identify"}.get(
                domain, ""
            )
            if unique_id.endswith("node_status"):
                suffix = "node_status"
            registry.async_get_or_create(
                domain,
                platform,
                unique_id,
                device_id=device.id,
                suggested_object_id=f"{name}_{suffix}".rstrip("_"),
                entity_category=category,
            )
    for entity, state in (
        ("switch.outlet", "on"),
        ("switch.elsewhere", "on"),
        ("light.couch", "on"),
        (NODE_STATUS, "alive"),
    ):
        hass.states.async_set(entity, state)

    dead: set[str] = set()
    zha_calls: list[dict] = []
    pings: list[str] = []

    async def _identify(call):
        zha_calls.append(dict(call.data))
        if str(call.data["ieee"]) in dead:
            raise DeliveryError("Failed to send request: device did not respond")

    async def _press(call):
        pings.append(call.data["entity_id"])

    hass.services.async_register("zha", "issue_zigbee_cluster_command", _identify)
    hass.services.async_register("button", "press", _press)
    return {"devices": made, "dead": dead, "zha": zha_calls, "pings": pings}


async def _outage(
    hass, freezer, radios, *, tv_by_person=False, also=(), node_back=True
):
    """A off for 200 s, devices following it the way they did in a measured trip."""
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    for circuit in also:
        hass.states.async_set(circuit, 0.0, POWER)
    radios["dead"].add(OUTLET_IEEE)
    await _tick(hass, freezer, 14)
    context = Context(user_id="person") if tv_by_person else None
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "off", context=context)
    await _tick(hass, freezer, 2)  # +16: the probes have run
    await _tick(hass, freezer, 37)
    hass.states.async_set(NODE_STATUS, "dead")  # +53
    await _tick(hass, freezer, 143)
    if node_back:
        hass.states.async_set(NODE_STATUS, "alive")  # +196
    radios["dead"].discard(OUTLET_IEEE)
    await _tick(hass, freezer, 4)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +200: live again
    for circuit in also:
        hass.states.async_set(circuit, 50.0, POWER)
    await _tick(hass, freezer, 31)  # the controls after SETTLE_S
    await _tick(hass, freezer, 29)
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "on")  # +260
    await _tick(hass, freezer, 1)


async def test_an_outage_records_what_went_down_with_the_breaker(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    await _outage(hass, freezer, radios)

    rows = watch._store.assignments()
    outlet = rows["switch.outlet"]
    assert outlet["circuit"] == CIRCUIT_A
    assert (outlet["source"], outlet["confidence"]) == ("probe", "inferred")
    assert outlet["evidence"]["result"] == "suspected"
    assert outlet["evidence"]["probe_kind"] == "breaker_reachability"
    assert outlet["evidence"]["signal"] == "zigbee_identify"
    couch = rows["light.couch"]
    assert (couch["source"], couch["evidence"]["signal"]) == (
        "breaker",
        "zwave_node_dead",
    )
    tv = rows["media_player.tv"]
    assert (tv["source"], tv["evidence"]["signal"]) == ("breaker", "media_off")
    assert "switch.elsewhere" not in rows

    trip = watch.last_trip()
    assert trip["suspected"] == ["light.couch", "media_player.tv", "switch.outlet"]
    assert trip["outage"]["answered"] == ["switch.elsewhere"]
    assert [e.data.get("amended", False) for e in events] == [False, True]
    # A trip that took devices with it keeps its circuit watched.
    assert CIRCUIT_A in watch.eligible

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["breaker"]["last_trip"]["outage"] == {
        "answered": 1,
        "casualties": 3,
        "indeterminate": 0,
        "recovered": 0,
    }
    dumped = json.dumps(report)
    for entity in ("switch.outlet", "light.couch", "media_player.tv"):
        assert entity not in dumped

    # Identify with a time of zero, to the endpoint that has the cluster.
    request = radios["zha"][0]
    assert (request["cluster_id"], request["command"]) == (3, 0)
    assert request["params"] == {"identify_time": 0}
    assert radios["pings"] == [NODE_PING]


async def test_a_tv_switched_off_by_a_person_is_not_a_casualty(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _outage(hass, freezer, radios, tv_by_person=True)
    assert "media_player.tv" not in watch._store.assignments()
    assert "media_player.tv" in watch.last_trip()["outage"]["indeterminate"]


async def test_a_refused_incident_records_nothing_from_its_outage(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    hass.states.async_set(CIRCUIT_B, 0.0, POWER)
    await _tick(hass, freezer, 16)
    run = watch._outage
    assert run is not None
    await _tick(hass, freezer, 110)  # +126: refused at CAP_S
    # Ended by the refusal, not left to expire.
    assert run.finished
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    hass.states.async_set(CIRCUIT_B, 50.0, POWER)
    await _tick(hass, freezer, 600)
    assert events == []
    assert watch._store.assignments() == {}


async def test_with_probing_off_nothing_is_sent_and_the_tv_still_counts(
    hass: HomeAssistant, powered, radios, freezer, config_entry
):
    entry = MockConfigEntry(
        domain=config_entry.domain,
        title=config_entry.title,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={CONF_BREAKER_PROBE: False},
    )
    watch = await _setup(hass, entry)
    await _arm(hass, freezer)
    await _outage(hass, freezer, radios)
    assert radios["zha"] == []
    assert radios["pings"] == []
    rows = watch._store.assignments()
    assert "media_player.tv" in rows
    # The node went dead on its own here; nothing asked it.
    assert "light.couch" in rows
    assert "switch.outlet" not in rows


async def test_an_unload_mid_outage_leaves_nothing_running(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 20)
    run = watch._outage
    assert run is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert run.finished
    calls = len(radios["zha"])
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    freezer.tick(timedelta(seconds=600))
    await _tick(hass, freezer, 1)
    assert len(radios["zha"]) == calls


def test_a_failed_request_is_read_by_what_caused_it():
    def chained(outer: Exception, inner: Exception) -> Exception:
        try:
            raise outer from inner
        except Exception as err:
            return err

    class ZHAException(Exception):
        pass

    class ServiceNotFound(Exception):
        pass

    assert probes.answered(DeliveryError("no ack")) is False
    assert probes.answered(chained(ZHAException("x"), DeliveryError("no ack"))) is False
    # It answered with a failure status: the device is there.
    assert probes.answered(ZHAException("status FAILURE")) is True
    assert probes.answered(ServiceNotFound("zha")) is None
    assert probes.answered(ValueError("no cluster")) is None


async def test_discovery_skips_battery_and_lock_devices(hass: HomeAssistant, radios):
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    for name, extra in (
        ("sensor", ("sensor", "battery", "battery")),
        ("lock", ("lock", "bolt", None)),
    ):
        device = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("demo", name)}, name=name
        )
        registry.async_get_or_create(
            "button",
            "zha",
            f"00:00:00:00:00:00:00:{len(name):02d}-1-3",
            device_id=device.id,
        )
        domain, object_id, device_class = extra
        registry.async_get_or_create(
            domain,
            "demo",
            f"{name}-{object_id}",
            device_id=device.id,
            original_device_class=device_class,
        )
    targets = probes.discover(hass, set())
    names = {
        devices.async_get(d).name: ieee for d, (ieee, _ep) in targets.zigbee.items()
    }
    assert names == {"outlet": OUTLET_IEEE, "elsewhere": ELSEWHERE_IEEE}
    assert [devices.async_get(d).name for d in targets.zwave] == ["couch"]
    assert [devices.async_get(d).name for d in targets.media] == ["Tv"]


async def test_a_node_that_never_comes_back_is_judged_when_the_window_ends(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _outage(hass, freezer, radios, node_back=False)
    # Asked again once power had been back SETTLE_S.
    assert radios["pings"] == [NODE_PING, NODE_PING]
    assert watch.last_trip().get("outage") is None
    await _tick(hass, freezer, 60)  # +321: RETURN_S after the return
    trip = watch.last_trip()
    assert trip["outage"]["indeterminate"] == ["light.couch"]
    assert "light.couch" not in watch._store.assignments()
    assert "switch.outlet" in watch._store.assignments()


async def test_a_circuit_dead_for_hours_is_not_followed(
    hass: HomeAssistant, config_entry, powered, radios, freezer, monkeypatch
):
    monkeypatch.setattr(probes, "MAX_OUTAGE_S", 300.0)
    # A ping that cannot be sent does not stop the run.
    hass.services.async_remove("button", "press")
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 20)
    run = watch._outage
    assert run is not None
    await _tick(hass, freezer, 290)
    assert run.finished
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, 200)
    assert watch.last_trip().get("outage") is None
    assert watch._store.assignments() == {}


async def test_a_zigbee_request_that_hangs_says_nothing(
    hass: HomeAssistant, radios, monkeypatch
):
    monkeypatch.setattr(probes, "REQUEST_TIMEOUT_S", 0.05)

    async def _hang(call):
        await asyncio.sleep(5)

    hass.services.async_register("zha", "issue_zigbee_cluster_command", _hang)
    assert await probes.identify(hass, OUTLET_IEEE, 1) is None


async def test_one_outage_is_followed_at_a_time(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    """A trips again while its first outage still waits for the TV: the second
    trip is recorded without outage evidence, and the first gets its own."""
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 14)
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "off")
    # A casualty keeps A watched for the second trip.
    hass.states.async_set("binary_sensor.bed_occupied", "unavailable")
    await _tick(hass, freezer, 30)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +44
    hass.states.async_set("binary_sensor.bed_occupied", "off")
    await _tick(hass, freezer, 31)  # the first trip closes
    run = watch._outage
    assert run is not None
    first = watch.last_trip()["start"]
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)  # +75
    await _tick(hass, freezer, 10)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, 31)  # the second trip closes
    assert watch._outage is run
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "on")  # +117
    await _tick(hass, freezer, 1)
    trips = {t["start"]: t for t in watch._store.breaker_trips()}
    assert len(trips) == 2
    second = next(s for s in trips if s != first)
    assert "outage" not in trips[second]
    assert trips[first]["outage"]["casualties"]["media_player.tv"]["signal"] == (
        "media_off"
    )


async def test_a_return_near_the_dead_limit_is_still_followed(
    hass: HomeAssistant, config_entry, powered, radios, freezer, monkeypatch
):
    # Back at +200, ten seconds inside the limit: the limit no longer applies.
    monkeypatch.setattr(probes, "MAX_OUTAGE_S", 210.0)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _outage(hass, freezer, radios)
    assert "switch.outlet" in watch.last_trip()["outage"]["casualties"]


async def test_a_return_that_does_not_hold_waits_for_the_power(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    radios["dead"].add(OUTLET_IEEE)
    await _tick(hass, freezer, 16)  # the outlet fails, elsewhere answers
    run = watch._outage
    assert run is not None
    await _tick(hass, freezer, 44)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +60: live for ten seconds
    await _tick(hass, freezer, 10)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)  # +70: dead again, same incident
    await _tick(hass, freezer, 1)
    assert run.evidence.restored is None
    asked = len(radios["zha"])
    await _tick(hass, freezer, 128)  # +199
    # Nothing was asked to answer while the circuit was dead again.
    assert len(radios["zha"]) == asked
    radios["dead"].discard(OUTLET_IEEE)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +200
    await _tick(hass, freezer, 31)  # the controls after SETTLE_S
    await _tick(hass, freezer, 1)
    casualty = watch.last_trip()["outage"]["casualties"]["switch.outlet"]
    assert casualty["answered_at"] >= watch.last_trip()["end"]
    assert "switch.outlet" in watch._store.assignments()


async def test_the_controls_end_with_the_return_window(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    radios["dead"].add(OUTLET_IEEE)
    await _tick(hass, freezer, 16)
    run = watch._outage
    assert run is not None

    async def _hang(call):
        radios["zha"].append(dict(call.data))
        await asyncio.Event().wait()

    hass.services.async_register("zha", "issue_zigbee_cluster_command", _hang)
    await _tick(hass, freezer, 184)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +200
    asked = len(radios["zha"])

    async def _step(seconds):
        # Frozen time only: the hanging request would end at its real 30 s timeout.
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    await _step(31)
    # Asked again after SETTLE_S, and no answer yet.
    assert [c["ieee"] for c in radios["zha"][asked:]] == [OUTLET_IEEE]
    assert not run.finished
    await _step(89)  # +320: RETURN_S after the return
    # Handed over at the window's end, with the request still unanswered.
    assert run.finished
    await _tick(hass, freezer, 1)
    trip = watch.last_trip()
    assert trip["outage"]["indeterminate"] == ["switch.outlet"]
    assert trip["outage"]["answered"] == ["switch.elsewhere"]


async def test_one_media_entity_going_off_is_not_the_tv_going_off(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 14)
    hass.states.async_set("remote.tv", "off")  # the media player stays on
    await _tick(hass, freezer, 186)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +200
    await _tick(hass, freezer, 60)
    hass.states.async_set("remote.tv", "on")
    await _tick(hass, freezer, 61)
    trip = watch.last_trip()
    assert "media_player.tv" not in trip["outage"]["casualties"]
    assert "media_player.tv" not in watch._store.assignments()


async def test_a_device_with_its_main_entity_disabled_keeps_its_own_key(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    registry = er.async_get(hass)
    registry.async_update_entity(
        "light.couch", disabled_by=er.RegistryEntryDisabler.USER
    )
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _outage(hass, freezer, radios)
    rows = watch._store.assignments()
    assert rows["light.couch"]["evidence"]["signal"] == "zwave_node_dead"
    assert NODE_PING not in rows


async def test_discovery_reads_disabled_entities_for_what_a_device_is(
    hass: HomeAssistant, radios
):
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    for name, extra in (
        ("hidden_battery", ("sensor", "battery", "battery")),
        ("hidden_lock", ("lock", "bolt", None)),
    ):
        device = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("demo", name)}, name=name
        )
        registry.async_get_or_create(
            "button",
            "zha",
            f"00:00:00:00:00:00:01:{len(name):02d}-1-3",
            device_id=device.id,
        )
        domain, object_id, device_class = extra
        registry.async_get_or_create(
            domain,
            "demo",
            f"{name}-{object_id}",
            device_id=device.id,
            original_device_class=device_class,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    targets = probes.discover(hass, set())
    names = {devices.async_get(d).name for d in targets.zigbee}
    assert names == {"outlet", "elsewhere"}


async def test_an_incident_opened_during_a_run_never_gets_one_later(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    """A trips again while its first run waits for the TV, and stays dead after."""
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 14)
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "off")
    hass.states.async_set("binary_sensor.bed_occupied", "unavailable")
    await _tick(hass, freezer, 30)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +44
    hass.states.async_set("binary_sensor.bed_occupied", "off")
    await _tick(hass, freezer, 31)  # the first trip closes
    first = watch._outage
    assert first is not None
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)  # +75: a second incident, skipped
    await _tick(hass, freezer, 25)
    for entity in ("media_player.tv", "remote.tv"):
        hass.states.async_set(entity, "on")  # +100: the first run ends
    await _tick(hass, freezer, 1)
    assert first.finished
    hass.states.async_set(CIRCUIT_A, 0.5, POWER)  # +101: still dead, a new reading
    await _tick(hass, freezer, 1)
    assert watch._outage is None


async def test_a_return_that_does_not_hold_starts_a_fresh_probe_pass(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    release = asyncio.Event()

    async def _press(call):
        radios["pings"].append(call.data["entity_id"])
        await release.wait()

    hass.services.async_register("button", "press", _press)

    async def _step(seconds):
        # Frozen time only: a press waits until released, so background tasks
        # are not awaited.
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _step(16)  # the first pass: its ping is still waiting
    assert radios["pings"] == [NODE_PING]
    await _step(44)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +60
    await _step(10)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)  # +70: dead again, same incident
    await _step(16)  # +86: PROBE_DELAY_S after it
    assert radios["pings"] == [NODE_PING, NODE_PING]
    assert watch._outage is not None
    release.set()
    await _tick(hass, freezer, 1)


async def test_a_tv_with_an_unreadable_sibling_is_not_judged_down(
    hass: HomeAssistant, config_entry, powered, radios, freezer
):
    hass.states.async_set("remote.tv", "unavailable")
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 14)
    hass.states.async_set("media_player.tv", "off")  # the remote's state is not known
    await _tick(hass, freezer, 186)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # +200
    await _tick(hass, freezer, 60)
    hass.states.async_set("media_player.tv", "on")
    await _tick(hass, freezer, 61)
    trip = watch.last_trip()
    assert "media_player.tv" not in trip["outage"]["casualties"]
    assert "media_player.tv" not in watch._store.assignments()
