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


# ------------------------------------------------------- repeated probes


def test_unanimous_probes_are_accepted():
    circuit, why = v.agree(["sensor.c1", "sensor.c1", "sensor.c1"])
    assert circuit == "sensor.c1" and "3/3" in why


def test_disagreeing_probes_return_nothing():
    """Not the most popular answer - nothing."""
    circuit, why = v.agree(["sensor.c1", "sensor.c2", "sensor.c1"])
    assert circuit is None and "disagreed" in why


def test_a_probe_that_found_nothing_blocks_agreement():
    circuit, why = v.agree(["sensor.c1", None])
    assert circuit is None and "1/2" in why


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
