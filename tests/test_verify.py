"""Tests for active verification logic.

Nothing here actuates anything - `verify.py` holds only the decision logic
precisely so it can be tested without touching a house.
"""

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "verify.py"
)
_spec = importlib.util.spec_from_file_location("pf_verify", _PATH)
v = importlib.util.module_from_spec(_spec)
sys.modules["pf_verify"] = v
_spec.loader.exec_module(v)


# ------------------------------------------------------------------ safety


def test_only_switches_and_lights_may_be_probed():
    assert v.may_probe("switch.lamp")[0]
    assert v.may_probe("light.kitchen")[0]


def test_dangerous_domains_are_refused():
    """An allowlist, so a domain nobody thought about fails closed."""
    for entity in (
        "lock.front_door",
        "alarm_control_panel.house",
        "cover.garage",
        "climate.living_room",
        "water_heater.tank",
        "valve.main",
        "siren.alarm",
        "media_player.tv",
        "script.anything",
    ):
        allowed, why = v.may_probe(entity)
        assert not allowed, entity
        assert "may be probed" in why


# ------------------------------------------------------------------ ranking


def test_matching_magnitude_wins_over_bigger_movement():
    """A circuit that moved a lot is not a better match than one that moved by
    the right amount."""
    ranked = v.rank_deltas(
        baseline={"a": 100.0, "b": 500.0},
        active={"a": 110.5, "b": 700.0},
        expected_w=10.8,
    )
    assert ranked[0]["circuit"] == "a"


def test_movement_in_the_wrong_direction_is_ignored():
    ranked = v.rank_deltas({"a": 100.0}, {"a": 89.0}, expected_w=10.8)
    assert ranked == []


def test_noise_is_ignored():
    ranked = v.rank_deltas({"a": 100.0}, {"a": 101.0}, expected_w=10.8)
    assert ranked == []


def test_a_circuit_unreadable_in_the_second_window_is_dropped_not_scored():
    """It would otherwise read as a step to nothing and win every ranking."""
    ranked = v.rank_deltas(
        baseline={"a": 100.0, "b": 500.0},
        active={"a": 110.5},
        expected_w=10.8,
    )
    assert [row["circuit"] for row in ranked] == ["a"]


# ----------------------------------------------------------------- deciding


def test_a_sole_matching_circuit_is_accepted():
    circuit, why = v.decide([{"circuit": "a", "delta_w": 10.5, "match": 0.97}])
    assert circuit == "a" and "sole" in why


def test_two_similar_circuits_are_refused():
    """Something else switched during the probe - that is not an answer."""
    circuit, why = v.decide(
        [
            {"circuit": "a", "delta_w": 10.5, "match": 0.97},
            {"circuit": "b", "delta_w": 10.4, "match": 0.96},
        ]
    )
    assert circuit is None and "ambiguous" in why


def test_no_movement_is_refused():
    circuit, why = v.decide([])
    assert circuit is None and "no circuit moved" in why


def test_a_clear_winner_is_named_with_its_margin():
    """The margin is reported because it is the reason the answer stands."""
    circuit, why = v.decide(
        [
            {"circuit": "a", "delta_w": 10.5, "match": 0.97},
            {"circuit": "b", "delta_w": 300.0, "match": 0.20},
        ]
    )
    assert circuit == "a"
    assert "clear winner by 0.77" in why


# ------------------------------------------------------- repeated probes


def test_unanimous_probes_are_accepted():
    circuit, why = v.agree(["sensor.c1", "sensor.c1", "sensor.c1"])
    assert circuit == "sensor.c1" and "3/3" in why


def test_probes_that_all_stayed_silent_are_no_answer_at_all():
    """Distinct from disagreement: nobody saw anything, so nothing is claimed."""
    circuit, why = v.agree([None, None])
    assert circuit is None
    assert why == "no probe identified a circuit"


def test_disagreeing_probes_return_nothing():
    """Not the most popular answer - nothing."""
    circuit, why = v.agree(["sensor.c1", "sensor.c2", "sensor.c1"])
    assert circuit is None and "disagreed" in why


def test_a_silent_probe_does_not_void_an_answer():
    """Regression: demanding unanimity threw away a real confirmation.

    Probing a Foyer light on a live panel, probe 1 returned nothing because the
    device's Z-Wave meter had not reported yet and probe 2 cleanly identified
    circuit 30, matching passive exactly. Silence is missing data, not a
    contradiction."""
    circuit, why = v.agree(["sensor.c1", None])
    assert circuit == "sensor.c1"
    assert "1/2" in why


def test_a_partial_answer_is_graded_down():
    assert v.grade("sensor.c1", [], True, answered=1, total=2)[1] == v.INFERRED
    assert v.grade("sensor.c1", [], True, answered=2, total=2)[1] == v.MEASURED


def test_an_automation_firing_makes_the_probe_suspect():
    circuit, level = v.grade("sensor.c1", ["automation.motion"], True, 2, 2)
    assert circuit == "sensor.c1" and level == v.SUSPECT


def test_interference_detects_a_moved_last_triggered():
    before = {"automation.a": "01:00:00", "automation.b": None}
    after = {"automation.a": "01:00:00", "automation.b": "01:01:10"}
    assert v.interference(before, after) == ["automation.b"]


def test_unmetered_ranking_falls_back_to_largest_mover():
    """A Z-Wave dimmer whose meter never moved must not make the probe useless."""
    ranked = v.rank_deltas(
        {"a": 100.0, "b": 100.0}, {"a": 139.3, "b": 104.0}, expected_w=None
    )
    assert ranked[0]["circuit"] == "a"
    assert ranked[0]["unmetered"] is True


# ------------------------------------------- combining passive with active


def test_both_methods_agreeing_is_the_strongest_verdict():
    circuit, level = v.combine("sensor.c1", "sensor.c1")
    assert (circuit, level) == ("sensor.c1", v.CONFIRMED)


def test_disagreement_trusts_neither():
    """Preferring the measurement would hide a real problem behind a confident
    answer."""
    circuit, level = v.combine("sensor.c1", "sensor.c2")
    assert circuit is None and level == v.CONFLICT


def test_each_method_alone_is_reported_as_such():
    assert v.combine(None, "sensor.c1") == ("sensor.c1", v.MEASURED)
    assert v.combine("sensor.c1", None) == ("sensor.c1", v.INFERRED)
    assert v.combine(None, None) == (None, v.UNKNOWN)


# ---------------------------------------- pausing automations for a probe


def test_safety_domains_are_never_paused():
    """Refusing to pause costs a noisier measurement. Pausing the wrong thing
    costs a leak alert that never fires."""
    for entity in (
        "lock.front_door",
        "alarm_control_panel.house",
        "cover.garage",
        "valve.main_shutoff",
        "water_heater.tank",
        "siren.alarm",
        "notify.mobile_app_phone",
        "device_tracker.phone",
        "person.lewis",
    ):
        ok, why = v.safe_to_pause({"light.lamp", entity})
        assert not ok, entity
        assert entity in why


def test_safety_device_classes_are_never_paused():
    for dc in ("moisture", "smoke", "gas", "carbon_monoxide", "safety"):
        ok, why = v.safe_to_pause(
            {"binary_sensor.leak", "light.lamp"}, {"binary_sensor.leak": dc}
        )
        assert not ok, dc
        assert dc in why


def test_an_ordinary_motion_light_automation_may_be_paused():
    ok, why = v.safe_to_pause(
        {"binary_sensor.hall_motion", "light.hall"},
        {"binary_sensor.hall_motion": "motion"},
    )
    assert ok and why == ""


def test_the_check_fails_closed_on_an_unknown_domain():
    """A domain nobody anticipated should not silently become pausable."""
    ok, _ = v.safe_to_pause({"lock.side_door"})
    assert not ok


# ------------------------------------------------------ battery devices


def test_a_battery_sensor_is_not_probeable():
    """No mains current means no CT will ever see it switch."""
    assert v.is_battery_powered(
        {"sensor.motion_battery": "battery", "binary_sensor.motion": "motion"}
    )


def test_a_ups_reports_a_battery_and_is_still_mains_powered():
    """Regression guard: excluding anything with a battery sensor would drop
    real loads - a UPS, some thermostats, a mains smoke alarm with a backup
    cell."""
    assert not v.is_battery_powered(
        {"sensor.ups_battery": "battery", "sensor.ups_load": "power"}
    )


def test_a_plain_mains_device_is_not_battery_powered():
    assert not v.is_battery_powered({"sensor.lamp_power": "power"})


def test_energy_or_current_also_prove_mains():
    assert not v.is_battery_powered(
        {"sensor.x_battery": "battery", "sensor.x_energy": "energy"}
    )
    assert not v.is_battery_powered(
        {"sensor.x_battery": "battery", "sensor.x_current": "current"}
    )


def test_a_config_switch_may_not_be_probed():
    """Found while picking probe targets on a real install.

    One Z-Wave dimmer published eleven `switch` entities - smart bulb mode,
    invert switch, local protection, double tap enabled - and one actual light.
    All eleven pass a domain allowlist. Flipping `invert_switch` reverses the
    paddle; flipping `local_protection` stops the wall switch working. None of
    them move any current, so the probe would learn nothing either way.
    """
    ok, why = v.may_probe("switch.front_porch_light_invert_switch", "config")
    assert not ok
    assert "not a load" in why


def test_a_diagnostic_switch_may_not_be_probed():
    ok, why = v.may_probe(
        "switch.front_porch_light_firmware_progress_led", "diagnostic"
    )
    assert not ok
    assert "diagnostic" in why


def test_a_real_light_is_still_probeable():
    """The guard must not refuse the thing it exists to allow."""
    assert v.may_probe("light.front_porch_light", None)[0]
    assert v.may_probe("switch.playroom_outlet", None)[0]


def test_a_live_load_is_not_switched_off_for_a_measurement():
    """Found by enumerating a real living room before probing it.

    `switch.living_room_logans_computer`, `switch.living_room_subwolfer_outlet`
    and `switch.living_room_usp_strip_outlet_1` all pass a domain allowlist and
    all mean yanking power from something mid-operation.
    """
    ok, why = v.safe_to_switch_off("on", 118.0)
    assert not ok
    assert "118.0 W" in why


def test_an_idle_switch_may_be_probed():
    assert v.safe_to_switch_off("on", 0.4)[0]
    assert v.safe_to_switch_off("off", None)[0]


def test_an_unmetered_device_that_is_on_is_refused_not_guessed():
    """It could be a lamp or a workstation. Say so instead of picking one."""
    ok, why = v.safe_to_switch_off("on", None)
    assert not ok
    assert "no way to tell" in why


def test_turning_an_idle_device_ON_is_never_gated():
    """The asymmetry is deliberate - powering something up is recoverable."""
    assert v.safe_to_switch_off("off", 0.0)[0]
    assert v.safe_to_switch_off("off", 500.0)[0]


def test_one_probe_can_never_be_measured():
    """The regression from a real 18-device sweep at probes: 1.

    Every placement came back `measured`, including a chandelier on an
    air-conditioner circuit and six devices from five unrelated areas on one
    busy circuit. With a single probe "all probes agreed" is vacuously true, so
    the verdict rests on one coincident step.
    """
    circuit, conf = v.grade("sensor.circuit_16", [], True, answered=1, total=1)
    assert circuit == "sensor.circuit_16"
    assert conf == v.INFERRED


def test_two_agreeing_probes_on_a_metered_device_are_measured():
    _c, conf = v.grade("sensor.circuit_16", [], True, answered=2, total=2)
    assert conf == v.MEASURED


def test_no_circuit_is_no_verdict():
    circuit, conf = v.grade(None, [], True, answered=0, total=3)
    assert circuit is None
    assert conf == v.UNKNOWN


def test_an_unmetered_sweep_is_only_ever_inferred():
    """Without the device's own meter, ranking is "which circuit moved most",
    which cannot reject a similar unrelated load however many probes agree."""
    _c, conf = v.grade("sensor.circuit_16", [], False, answered=3, total=3)
    assert conf == v.INFERRED


def test_interference_still_outranks_probe_count():
    _c, conf = v.grade("sensor.circuit_16", ["automation.x"], True, answered=3, total=3)
    assert conf == v.SUSPECT
