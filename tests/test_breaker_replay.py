"""Automatic trip detection replayed against recorded states from a real panel.

`tests/fixtures/breaker_trips.json` holds recorder windows from one install
with every entity id replaced by a generic one: a 7 s breaker flip, a Home
Assistant restart, an integration dropping 22 entities at once with no circuit
moving, and eight appliance circuits reaching the noise floor. Eligibility is
judged on the 720 hourly minima each circuit recorded before the flip.
"""

import importlib.util
import itertools
import json
import math
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    path = _ROOT / "custom_components" / "power_fingerprint" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"pf_replay_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


b = _load("breaker")
an = _load("analysis")

FIXTURE = json.loads(
    (_ROOT / "tests" / "fixtures" / "breaker_trips.json").read_text(encoding="utf-8")
)
# The balanced confidence setting's eligibility floor.
FLOOR_W = 4.0
FLIP = "sensor.circuit_20_power"


def eligible_set(floor_w=FLOOR_W, trips=None):
    first = FIXTURE["first_hour"]
    out = {}
    for circuit, mins in FIXTURE["hourly_min"].items():
        hourly = {
            first + i * 3600.0: float(v) for i, v in enumerate(mins) if v is not None
        }
        out[circuit] = b.eligibility(circuit, hourly, trips or [], floor_w, now=0.0)
    return out


ELIGIBILITY = eligible_set()
ELIGIBLE = frozenset(c for c, e in ELIGIBILITY.items() if e.eligible)


def _available(value):
    return value != "unavailable"


def _watts(value, unit):
    if isinstance(value, str):
        return None
    return an.to_watts(float(value), unit or "W")


def events(window):
    """Every row of a window as (t, entity, kind, value, previous, unit)."""
    out = []
    for entity, info in FIXTURE["windows"][window]["entities"].items():
        prev = None
        for t, value in info["rows"]:
            out.append((t, entity, info["kind"], value, prev, info["unit"]))
            prev = value
    out.sort(key=lambda row: (row[0], row[1]))
    return out


def replay(window, eligible=ELIGIBLE, armed_from=-math.inf, drop=()):
    """Feed a window to a detector the way the integration does; return outcomes."""
    detector = b.TripDetector(list(FIXTURE["hourly_min"]), alive_w=FLOOR_W)
    detector.eligible = frozenset(eligible)
    detector.armed_from = armed_from
    outcomes = []
    last = 0.0
    for t, entity, kind, value, prev, unit in events(window):
        if entity in drop:
            continue
        outcome = detector.close(t)
        if outcome:
            outcomes.append(outcome)
        if kind == "circuit":
            detector.circuit(entity, _watts(value, unit), t)
        elif kind == "meter":
            detector.device(
                entity,
                t,
                _available(value),
                _available(prev) if prev is not None else _available(value),
                _watts(value, unit),
                _watts(prev, unit) if prev is not None else None,
            )
        elif prev is None or _available(prev) != _available(value):
            detector.device(
                entity,
                t,
                _available(value),
                _available(prev) if prev is not None else _available(value),
            )
        last = t
    outcome = detector.close(last + 1e6)
    if outcome:
        outcomes.append(outcome)
    return outcomes


def devices_of(entities, window="breaker_flip"):
    table = FIXTURE["windows"][window]["entities"]
    return {table[e]["device"] for e in entities}


# --- eligibility on 720 real hours ----------------------------------------


def test_circuits_that_never_idle_are_eligible_and_appliance_circuits_are_not():
    numbers = [
        "9",
        "10",
        "11",
        "12",
        "16",
        "17",
        "19",
        "20",
        "22",
        "24",
        "25",
        "30",
        "32",
    ]
    assert sorted(ELIGIBLE) == sorted(f"sensor.circuit_{n}_power" for n in numbers)
    for appliance in ("1_3", "2_4", "5_7", "6_8", "13", "15", "26"):
        verdict = ELIGIBILITY[f"sensor.circuit_{appliance}_power"]
        assert not verdict.eligible
        assert verdict.last_idle is not None


def test_a_circuit_idling_at_the_noise_floor_is_not_eligible():
    """Circuit 28 sits at 1.8-3.1 W with nothing running."""
    assert not ELIGIBILITY["sensor.circuit_28_power"].eligible
    assert ELIGIBILITY["sensor.circuit_28_power"].last_idle is not None


def test_a_floor_within_what_an_idle_circuit_reads_is_not_eligible():
    """Circuit 31 never reaches 2 W but never clears 4 W either."""
    verdict = ELIGIBILITY["sensor.circuit_31_power"]
    assert not verdict.eligible
    assert verdict.last_idle is None
    assert 2.0 < verdict.floor_w <= FLOOR_W


# --- the flip -------------------------------------------------------------


def test_the_flip_is_one_trip_on_its_circuit():
    outcomes = replay("breaker_flip")
    assert [o.kind for o in outcomes] == [b.TRIP]
    trip = outcomes[0]
    assert trip.circuit == FLIP
    assert trip.end is not None
    assert 5.0 < trip.end - trip.start < 15.0


def test_the_flip_suspects_the_tv_bed_and_chime():
    trip = replay("breaker_flip")[0]
    assert trip.result.confirmed == []
    assert {"bedroom_tv", "bedroom_tv_remote", "bed", "chime"} <= devices_of(
        trip.result.suspected
    )
    assert devices_of(trip.result.suspected) == {
        "bedroom_tv",
        "bedroom_tv_remote",
        "bed",
        "chime",
    }


def test_the_flip_is_not_detected_on_an_ineligible_circuit():
    assert replay("breaker_flip", eligible=ELIGIBLE - {FLIP}) == []


def test_the_flip_is_not_detected_before_the_detector_is_armed():
    assert replay("breaker_flip", armed_from=math.inf) == []


# --- negative cases -------------------------------------------------------


def test_a_home_assistant_restart_is_not_a_trip():
    """Every circuit reads unavailable, then its value: no fall to the floor."""
    assert replay("ha_restart") == []


def test_the_restart_writes_unavailable_not_zero():
    circuits = {
        e: info
        for e, info in FIXTURE["windows"]["ha_restart"]["entities"].items()
        if info["kind"] == "circuit"
    }
    assert len(circuits) == 27
    for entity, info in circuits.items():
        values = [v for _t, v in info["rows"]]
        assert "unavailable" in values
        if entity in ELIGIBLE:
            assert not any(isinstance(v, float) and v <= b.DEAD_W for v in values)
        gap = values.index("unavailable")
        before = next(v for v in reversed(values[:gap]) if isinstance(v, float))
        after = next(v for v in values[gap:] if isinstance(v, float))
        assert (after <= b.DEAD_W) == (before <= b.DEAD_W), entity


def test_an_integration_dropping_many_entities_is_not_a_trip():
    """22 entities of one device go unavailable together; no circuit moves."""
    rows = FIXTURE["windows"]["integration_reconnect"]["entities"]
    dropped = [
        e
        for e, info in rows.items()
        if info["device"] == "kvm" and "unavailable" in [v for _t, v in info["rows"]]
    ]
    assert len(dropped) == 22
    assert replay("integration_reconnect") == []


APPLIANCE_WINDOWS = {
    "ac_bedrooms_off": "sensor.circuit_6_8_power",
    "microwave_off": "sensor.circuit_26_power",
    "disposal_off": "sensor.circuit_13_power",
    "kitchen_lights_off": "sensor.circuit_18_power",
    "dishwasher_standby": "sensor.circuit_15_power",
    "washer_standby": "sensor.circuit_21_power",
    "ac_central_standby": "sensor.circuit_2_4_power",
    "oven_standby": "sensor.circuit_1_3_power",
}


def test_appliance_circuits_reaching_the_floor_are_not_trips():
    for window, circuit in APPLIANCE_WINDOWS.items():
        rows = FIXTURE["windows"][window]["entities"][circuit]["rows"]
        falls = [
            (prev, now)
            for (_t0, prev), (_t1, now) in itertools.pairwise(rows)
            if isinstance(prev, float)
            and isinstance(now, float)
            and prev > b.DEAD_W >= now
        ]
        assert falls, window
        assert replay(window) == [], window


def test_an_appliance_circuit_would_trip_if_it_were_eligible():
    """Positive control: the windows do contain a fall the detector would open on."""
    for window, circuit in APPLIANCE_WINDOWS.items():
        outcomes = replay(window, eligible={circuit})
        assert outcomes, window
        assert outcomes[0].circuit == circuit


def test_two_circuits_dead_together_is_refused():
    """The flip with a second circuit's clamp falling alongside it."""
    detector_window = FIXTURE["windows"]["breaker_flip"]["entities"]
    other = "sensor.circuit_22_power"
    saved = [list(row) for row in detector_window[other]["rows"]]
    try:
        for row in detector_window[other]["rows"]:
            if 376.0 <= row[0] <= 389.0:
                row[1] = 0.0
        outcomes = replay("breaker_flip")
    finally:
        detector_window[other]["rows"] = saved
    assert [o.kind for o in outcomes] == [b.TOGETHER]
    assert outcomes[0].together == [other]
    assert not outcomes[0].accepted
