"""The persistent library: what it keeps, what it refuses to overwrite.

The store is Home Assistant's own `Store` helper, so it needs the harness even
though nothing here is an entity.
"""

from homeassistant.core import HomeAssistant

from custom_components.power_fingerprint.const import DOMAIN
from custom_components.power_fingerprint.fingerprint import Fingerprint
from custom_components.power_fingerprint.store import FingerprintStore

CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
LAMP = "sensor.porch_lamp_power"


def shape(label, circuit=CIRCUIT_A, count=5):
    return Fingerprint(
        label=label, circuit=circuit, count=count, centroid={"peak_w": 1200.0}
    )


async def _loaded(hass, entry_id="entry"):
    store = FingerprintStore(hass, entry_id)
    await store.async_load()
    return store


async def test_a_new_install_loads_an_empty_library(hass: HomeAssistant):
    store = await _loaded(hass)
    assert store.fingerprints == []
    assert store.labelled() == []
    assert store.circuits() == []
    assert store.last_poll() is None
    assert store.orphaned_pauses() == []


async def test_what_was_saved_is_what_comes_back(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer"), shape("unnamed_1")])
    await store.async_record_paused(["automation.hall"])
    await store.async_record_seen({"a|Dryer": "2026-09-04T12:00:00+00:00"}, "stamp")
    await store.async_save()

    reloaded = await _loaded(hass)
    assert [fp.label for fp in reloaded.fingerprints] == ["Dryer", "unnamed_1"]
    assert reloaded.labelled()[0].label == "Dryer"
    assert [fp.label for fp in reloaded.unnamed()] == ["unnamed_1"]
    assert reloaded.circuits() == [CIRCUIT_A]
    assert reloaded.for_circuit(CIRCUIT_A)[0].label == "Dryer"
    assert reloaded.orphaned_pauses() == ["automation.hall"]
    assert reloaded.last_seen() == {"a|Dryer": "2026-09-04T12:00:00+00:00"}
    assert reloaded.last_poll() == "stamp"


async def test_a_candidate_never_drives_an_entity(hass: HomeAssistant):
    """`unnamed_2 is running` is worse than saying nothing."""
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("unnamed_0")])
    assert store.labelled() == []
    assert store.circuits() == []


async def test_relearning_replaces_only_the_circuit_it_learned(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer")])
    await store.async_replace_circuit(CIRCUIT_B, [shape("Fridge", circuit=CIRCUIT_B)])
    await store.async_replace_circuit(CIRCUIT_A, [shape("unnamed_0")])

    labels = {fp.circuit: fp.label for fp in store.fingerprints}
    # The name a human gave is carried onto the new shape by position.
    assert labels[CIRCUIT_A] == "Dryer"
    assert labels[CIRCUIT_B] == "Fridge"


async def test_naming_a_candidate_that_does_not_exist_is_refused(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("unnamed_0")])
    assert await store.async_relabel(CIRCUIT_A, "unnamed_0", "Dryer") is True
    assert await store.async_relabel(CIRCUIT_A, "unnamed_7", "Oven") is False
    assert store.labelled()[0].label == "Dryer"


# --- assignments -----------------------------------------------------------


async def test_a_passive_sweep_never_overwrites_a_probe(hass: HomeAssistant):
    """A probe switched a real light and watched a real circuit move. A later
    correlation finding something else must not quietly replace that."""
    store = await _loaded(hass)
    assert await store.async_record_assignment(
        LAMP, "sensor.circuit_30", "measured", "probe", {"probes": 3}
    )
    assert not await store.async_record_assignment(
        LAMP, "sensor.circuit_18", "inferred", "correlation"
    )
    assert store.assignments()[LAMP]["circuit"] == "sensor.circuit_30"


async def test_i_could_not_tell_this_time_is_not_recorded(hass: HomeAssistant):
    store = await _loaded(hass)
    assert not await store.async_record_assignment(LAMP, None, "unknown", "probe")
    assert store.assignments() == {}


async def test_recording_the_same_answer_twice_writes_nothing(hass: HomeAssistant):
    """The coordinator refreshes every 30 s; a no-op must not hit the disk."""
    store = await _loaded(hass)
    assert await store.async_record_assignment(
        LAMP, "sensor.circuit_30", "inferred", "correlation"
    )
    assert not await store.async_record_assignment(
        LAMP, "sensor.circuit_30", "inferred", "correlation"
    )


async def test_a_probe_may_replace_its_own_earlier_answer(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_record_assignment(LAMP, "sensor.circuit_18", "measured", "probe")
    assert await store.async_record_assignment(
        LAMP, "sensor.circuit_30", "measured", "probe"
    )
    assert store.assignments()[LAMP]["circuit"] == "sensor.circuit_30"


async def test_forgetting_an_assignment_that_was_never_made_changes_nothing(
    hass: HomeAssistant,
):
    store = await _loaded(hass)
    assert not await store.async_forget_assignment(LAMP)
    await store.async_record_assignment(LAMP, "sensor.circuit_30", "measured", "probe")
    assert await store.async_forget_assignment(LAMP)
    assert store.assignments() == {}


async def test_an_assignment_of_our_own_sensor_is_dropped_on_load(
    hass: HomeAssistant, hass_storage
):
    """`unmonitored_load` is mains minus the circuits, so correlating it with a
    circuit is circular. An early version did exactly that and persisted it."""
    hass_storage[f"{DOMAIN}.entry"] = {
        "version": 1,
        "data": {
            "fingerprints": [],
            "assignments": {
                f"sensor.{DOMAIN}_unmonitored_load": {"circuit": "sensor.circuit_1"},
                LAMP: {"circuit": "sensor.circuit_30"},
            },
        },
    }
    store = await _loaded(hass)
    assert list(store.assignments()) == [LAMP]


# --- renames ---------------------------------------------------------------


async def test_a_rename_follows_every_reference_to_the_old_id(hass: HomeAssistant):
    """Fingerprints are keyed by circuit, last-seen by "circuit|label" and
    assignments by both device and circuit. All four have to move together."""
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer")])
    await store.async_record_seen({store.seen_key(CIRCUIT_A, "Dryer"): "then"}, "poll")
    await store.async_record_assignment(LAMP, CIRCUIT_A, "measured", "probe")
    await store.async_record_assignment(CIRCUIT_A, "sensor.circuit_9", "x", "probe")

    await store.async_migrate_entity_id(CIRCUIT_A, "sensor.garage_power")

    assert store.circuits() == ["sensor.garage_power"]
    assert "sensor.garage_power|Dryer" in store.last_seen()
    assert store.assignments()[LAMP]["circuit"] == "sensor.garage_power"
    assert "sensor.garage_power" in store.assignments()


async def test_a_rename_of_something_unrelated_writes_nothing(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer")])
    await store.async_migrate_entity_id("sensor.nothing_here", "sensor.still_nothing")
    assert store.circuits() == [CIRCUIT_A]


# --- removal ---------------------------------------------------------------


async def test_removing_the_store_empties_it(hass: HomeAssistant, hass_storage):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer")])
    await store.async_record_paused(["automation.hall"])
    assert f"{DOMAIN}.entry" in hass_storage

    await store.async_remove()
    assert f"{DOMAIN}.entry" not in hass_storage
    assert store.fingerprints == []
    assert store.orphaned_pauses() == []
    assert store.assignments() == {}
    assert store.last_seen() == {}
    assert store.last_poll() is None


async def test_clearing_the_pause_record_leaves_the_rest_alone(hass: HomeAssistant):
    store = await _loaded(hass)
    await store.async_replace_circuit(CIRCUIT_A, [shape("Dryer")])
    await store.async_record_paused(["automation.hall"])
    await store.async_clear_paused()
    assert store.orphaned_pauses() == []
    assert store.circuits() == [CIRCUIT_A]


async def test_the_poll_heartbeat_is_kept_without_touching_last_seen(
    hass: HomeAssistant,
):
    """The heartbeat used to persist only on a sighting, so after a restart the
    whole gap since the last SIGHTING was credited as blind time - muting the
    overdue alert exactly when an appliance had died."""
    store = await _loaded(hass)
    store.record_poll("2026-09-04T12:00:00+00:00")
    assert store.last_poll() == "2026-09-04T12:00:00+00:00"
    assert store.last_seen() == {}
