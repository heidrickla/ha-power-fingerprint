"""Breaker walk: the only causal test in the integration, and the riskiest."""

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "breaker.py"
)
_spec = importlib.util.spec_from_file_location("pf_breaker", _PATH)
b = importlib.util.module_from_spec(_spec)
sys.modules["pf_breaker"] = b
_spec.loader.exec_module(b)


# --- which breaker was flipped ---------------------------------------------


def test_the_circuits_own_clamp_identifies_the_breaker():
    """Nobody has to say which one they flipped."""
    before = {"c15": 420.0, "c16": 700.0, "c22": 60.0}
    after = {"c15": 0.4, "c16": 700.0, "c22": 60.0}
    circuit, why = b.dead_circuit(before, after)
    assert circuit == "c15"
    assert "confirms" in why


def test_a_breaker_with_no_clamp_is_a_refusal_not_a_guess():
    """⛔ The blind-source rule. If nothing went dead, nothing was seen."""
    before = {"c15": 420.0, "c16": 700.0}
    after = {"c15": 421.0, "c16": 699.0}
    circuit, why = b.dead_circuit(before, after)
    assert circuit is None
    assert "no clamp" in why or "no monitored circuit" in why


def test_an_already_idle_circuit_cannot_be_the_answer():
    """Staying at zero proves nothing, and would let someone "identify" a
    breaker they never touched."""
    before = {"c14": 0.0, "c15": 420.0}
    after = {"c14": 0.0, "c15": 420.0}
    assert b.dead_circuit(before, after)[0] is None


def test_two_circuits_dying_together_is_refused():
    """A double-pole breaker or a main. Picking the larger would invent a fact."""
    before = {"c1": 900.0, "c3": 850.0, "c9": 100.0}
    after = {"c1": 0.2, "c3": 0.1, "c9": 100.0}
    circuit, why = b.dead_circuit(before, after)
    assert circuit is None
    assert "double-pole" in why


def test_the_noise_floor_is_not_mistaken_for_alive():
    """A CT reads a few hundred milliwatts with nothing on it."""
    assert b.dead_circuit({"c": 500.0}, {"c": 1.4})[0] == "c"


# --- what died with it ------------------------------------------------------


def test_a_measured_collapse_is_confirmed():
    confirmed, suspected, _ = b.classify_devices({"lamp": 60.0}, {"lamp": 0.0})
    assert confirmed == ["lamp"]
    assert suspected == []


def test_a_device_that_merely_vanished_is_only_suspected():
    """⛔ It might be on the circuit, or its Zigbee parent might have been."""
    confirmed, suspected, _ = b.classify_devices({"sensor": 5.0}, {"sensor": None})
    assert confirmed == []
    assert suspected == ["sensor"]


def test_a_known_mesh_router_stays_suspected_however_it_looks():
    """Kill one mains-powered router and a dozen battery sensors go quiet."""
    confirmed, suspected, _ = b.classify_devices(
        {"router_plug": 8.0},
        {"router_plug": 0.0},
        mesh_routers=frozenset({"router_plug"}),
    )
    assert confirmed == []
    assert suspected == ["router_plug"]


def test_a_load_that_barely_moved_is_unaffected():
    confirmed, _s, unaffected = b.classify_devices({"fridge": 120.0}, {"fridge": 118.0})
    assert confirmed == []
    assert unaffected == ["fridge"]


def test_a_device_already_at_zero_proves_nothing_by_staying_there():
    confirmed, _s, unaffected = b.classify_devices({"off_lamp": 0.0}, {"off_lamp": 0.0})
    assert confirmed == []
    assert unaffected == ["off_lamp"]


# --- the whole walk ---------------------------------------------------------


def test_a_clean_walk_attributes_only_what_it_measured():
    result = b.walk(
        circuits_before={"c15": 400.0, "c16": 700.0},
        circuits_after={"c15": 0.3, "c16": 700.0},
        devices_before={"dishwasher": 380.0, "office_light": 9.0, "doorbell": None},
        devices_after={"dishwasher": 0.0, "office_light": 9.0, "doorbell": None},
    )
    assert result.circuit == "c15"
    assert result.confirmed == ["dishwasher"]
    assert "office_light" in result.unaffected
    assert "doorbell" in result.suspected


def test_without_a_confirmed_circuit_nothing_is_attributed():
    """⛔ Casualties are reported; none of them are mapped to anything.

    Something died, but with no circuit confirmed there is nothing to attribute
    it TO, and guessing would be the whole point of this module thrown away.
    """
    result = b.walk(
        circuits_before={"c15": 400.0},
        circuits_after={"c15": 399.0},
        devices_before={"dishwasher": 380.0},
        devices_after={"dishwasher": 0.0},
    )
    assert result.circuit is None
    assert result.confirmed == []
    assert result.suspected == ["dishwasher"]


# --- what to show someone about to flip a breaker ---------------------------
#
# ⛔ Two earlier versions rated circuits as safe or unsafe. Both were wrong, in
# opposite directions, and the person at the panel knows their own house better
# than either. These check that it reports and does not judge.


def test_it_returns_measurements_and_no_verdict():
    d = b.describe(
        "sensor.c16",
        "Circuit 16 Study",
        700.0,
        ["sensor.pdu"],
        standby_w=652.0,
        observed_hours=48.0,
        ever_seen_idle=False,
    )
    assert d["standby_w"] == 652.0
    assert d["known_devices"] == ["sensor.pdu"]
    for absent in ("verdict", "safe_looking", "warnings", "risky"):
        assert absent not in d, f"{absent} is a judgement, not a measurement"


def test_a_new_install_reports_what_little_it_knows_without_pretending():
    """No attributions and almost no history is an honest state, not a verdict."""
    d = b.describe("sensor.c7", "Circuit 7", 12.0, [], observed_hours=0.5)
    assert d["known_devices"] == []
    assert d["observed_hours"] == 0.5
    assert d["ever_seen_idle"] is None


def test_the_permanent_floor_survives_having_nothing_identified():
    """⭐ The number that works from day one, before anything is named."""
    d = b.describe(
        "sensor.c30", "Circuit 30", 40.0, [], standby_w=652.0, observed_hours=24.0
    )
    assert d["standby_w"] == 652.0
    assert d["known_devices"] == []
