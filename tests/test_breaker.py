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
    """The blind-source rule. If nothing went dead, nothing was seen."""
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
    """It might be on the circuit, or its Zigbee parent might have been."""
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


def test_a_device_with_no_reading_before_that_reports_after_is_unaffected():
    """A meter that came online during the walk proves nothing about the
    breaker, and must not be counted as a casualty."""
    confirmed, suspected, unaffected = b.classify_devices(
        before={"late_meter": None},
        after={"late_meter": 40.0},
    )
    assert unaffected == ["late_meter"]
    assert confirmed == [] and suspected == []


def test_a_walk_result_serialises_for_the_action_response():
    result = b.walk(
        circuits_before={"c15": 400.0},
        circuits_after={"c15": 0.3},
        devices_before={"dishwasher": 380.0, "office_light": 9.0},
        devices_after={"dishwasher": 0.0, "office_light": 9.0},
    )
    payload = result.to_dict()
    assert payload["circuit"] == "c15"
    assert payload["circuit_before_w"] == 400.0
    assert payload["circuit_after_w"] == 0.3
    assert payload["confirmed"] == ["dishwasher"]
    # A count, not a list: the unaffected are most of the house.
    assert payload["unaffected_count"] == 1
    assert payload["reason"]


def test_without_a_confirmed_circuit_nothing_is_attributed():
    """Casualties are reported; none of them are mapped to anything.

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
# It reports measurements and forms no verdict: the person at the panel knows
# their own house better than any inference from names would.


def test_it_returns_measurements_and_no_verdict():
    d = b.describe(
        "sensor.c16",
        "Circuit 23 Study",
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
    """The number that works from day one, before anything is named."""
    d = b.describe(
        "sensor.c30", "Circuit 30", 40.0, [], standby_w=652.0, observed_hours=24.0
    )
    assert d["standby_w"] == 652.0
    assert d["known_devices"] == []


def test_a_circuit_idling_near_the_floor_does_not_count_as_dying():
    """2.149 -> 1.911 W, recorded on a washer circuit with nothing running."""
    before = {"c15": 420.0, "c21": 2.149}
    after = {"c15": 0.3, "c21": 1.911}
    assert b.dead_circuit(before, after, alive_w=4.0)[0] == "c15"
    assert b.dead_circuit(before, after)[0] is None


# --- eligibility --------------------------------------------------------------

HOUR = 3600.0


def _hours(n, low=100.0, end=0.0):
    return {end - (i + 1) * HOUR: low for i in range(n)}


def test_a_circuit_that_never_idles_is_eligible():
    verdict = b.eligibility("c", _hours(720), [], floor_w=4.0, now=0.0)
    assert verdict.eligible
    assert verdict.floor_w == 100.0


def test_eligibility_needs_a_weeks_history():
    verdict = b.eligibility("c", _hours(100), [], floor_w=4.0, now=0.0)
    assert not verdict.eligible
    assert verdict.observed_hours == 100


def test_one_hour_at_the_floor_makes_a_circuit_ineligible():
    hourly = _hours(720)
    hourly[-10 * HOUR] = 0.4
    verdict = b.eligibility("c", hourly, [], floor_w=4.0, now=0.0)
    assert not verdict.eligible
    assert verdict.last_idle == -10 * HOUR


def test_a_recorded_trip_does_not_cost_its_circuit_eligibility():
    hourly = _hours(720)
    hourly[-10 * HOUR] = 0.0
    trip = (-10 * HOUR + 1200.0, -10 * HOUR + 1207.0)
    assert b.eligibility("c", hourly, [trip], floor_w=4.0, now=0.0).eligible


def test_a_trip_still_open_covers_up_to_now():
    """Dead hours after the one it started in are still the trip."""
    hourly = _hours(720)
    for hour in (-3, -2, -1):
        hourly[hour * HOUR] = 0.0
    trip = (-3 * HOUR + 600.0, None)
    assert b.eligibility("c", hourly, [trip], 4.0, now=0.0).eligible


def test_an_idle_hour_older_than_the_lookback_no_longer_counts():
    hourly = _hours(720)
    hourly[-800 * HOUR] = 0.0
    assert b.eligibility("c", hourly, [], floor_w=4.0, now=0.0).eligible


def test_a_floor_an_idle_circuit_can_read_is_not_eligible():
    verdict = b.eligibility("c", _hours(720, low=3.2), [], floor_w=4.0, now=0.0)
    assert not verdict.eligible
    assert verdict.last_idle is None


# --- the detector, one guard at a time ----------------------------------------
#
# A panel reporting every 6 s: c1 at 300 W, c2 at 500 W. c1 reads 0 at 600
# and 606 and is back at 612, so the incident closes at 642.


def _panel(dead=(600, 612), c1=300.0, c2=500.0, c1_rows=None, c2_rows=None):
    events = []
    for t in range(0, 900, 6):
        w1 = 0.0 if dead[0] <= t < dead[1] else c1
        events.append((float(t), "circuit", "c1", (c1_rows or {}).get(t, w1)))
        events.append((t + 1.0, "circuit", "c2", (c2_rows or {}).get(t + 1, c2)))
    return events


def _drop(entity, t, back=None):
    out = [(float(t), "device", entity, (False, True, None))]
    if back is not None:
        out.append((float(back), "device", entity, (True, False, None)))
    return out


def _run(events, eligible=("c1",), armed_from=0.0, alive_w=4.0, close_at=None):
    detector = b.TripDetector(["c1", "c2", "c3"], alive_w=alive_w)
    detector.eligible = frozenset(eligible)
    detector.armed_from = armed_from
    outcomes = []
    for t, kind, entity, value in sorted(events, key=lambda e: (e[0], e[2])):
        if close_at is None and (out := detector.close(t)):
            outcomes.append(out)
        if kind == "circuit":
            detector.circuit(entity, value, t)
        else:
            detector.device(entity, t, *value)
    if out := detector.close(close_at if close_at is not None else 1e9):
        outcomes.append(out)
    return outcomes


def test_one_eligible_circuit_falling_to_the_floor_is_a_trip():
    [trip] = _run(_panel() + _drop("tv", 603))
    assert trip.kind == b.TRIP
    assert (trip.circuit, trip.start, trip.end, trip.last_live) == (
        "c1",
        600.0,
        612.0,
        594.0,
    )
    assert trip.result.suspected == ["tv"]


def test_an_ineligible_circuit_falling_opens_nothing():
    assert _run(_panel() + _drop("tv", 603), eligible=()) == []


def test_nothing_opens_before_the_detector_is_armed():
    assert _run(_panel(), armed_from=700.0) == []


def test_a_reading_after_unavailable_opens_nothing():
    """A restart's first reading, even if it is zero, has nothing before it."""
    assert _run(_panel(c1_rows={594: None})) == []


def test_a_circuit_going_unavailable_opens_nothing():
    assert _run(_panel(dead=(0, 0), c1_rows={600: None, 606: None})) == []


def test_a_circuit_already_at_the_floor_opens_nothing():
    rows = {t: 1.5 for t in range(0, 600, 6)}
    assert _run(_panel(c1_rows=rows)) == []


def test_devices_dropping_with_no_circuit_dead_open_nothing():
    """Twenty-two entities of one device at once, and every clamp steady."""
    events = _panel(dead=(0, 0))
    for n in range(22):
        events += _drop(f"kvm_{n}", 603)
    assert _run(events) == []


def test_the_detector_refuses_two_circuits_dying_together():
    [outcome] = _run(_panel(c2_rows={601: 0.0, 607: 0.0}))
    assert outcome.kind == b.TOGETHER
    assert outcome.together == ["c2"]
    assert not outcome.accepted
    assert outcome.to_dict()["suspected"] == []


def test_a_circuit_dithering_at_the_floor_is_not_dying_together():
    c2 = dict.fromkeys(range(1, 900, 6), 2.149)
    c2[607] = 1.911
    lamp = [
        (float(t), "device", "lamp", (True, True, 0.0 if t >= 602 else 60.0))
        for t in range(2, 900, 5)
    ]
    [outcome] = _run(_panel(c2_rows=c2) + lamp)
    assert outcome.kind == b.TRIP
    # The attribution agrees with the kind: the collapse is confirmed.
    assert outcome.result.circuit == "c1"
    assert outcome.result.confirmed == ["lamp"]


def test_a_circuit_that_died_just_before_is_together():
    """The other unit reports first: its leg reads dead 11 s before this one."""
    c2 = {t: 0.0 for t in range(589, 900, 6)}
    [outcome] = _run(_panel(c2_rows=c2))
    assert outcome.kind == b.TOGETHER


def test_a_circuit_that_died_before_the_together_window_is_not_together():
    """c2 reports at 6k + 1 s; dead from 559, 41 s before c1's fall."""
    c2 = {t: 0.0 for t in range(559, 900, 6)}
    assert b.TOGETHER_S < 600 - 559
    [outcome] = _run(_panel(c2_rows=c2))
    assert outcome.kind == b.TRIP
    assert outcome.together == []


def test_a_device_gone_between_the_last_live_reading_and_the_dead_one_counts():
    """The meter averages: the cut lands before it reads zero."""
    [trip] = _run(_panel() + _drop("fast", 597))
    assert trip.result.suspected == ["fast"]


def test_a_device_unavailable_since_before_anything_was_heard_is_not_counted():
    """Its first record is its return, inside the outage."""
    events = [*_panel(), (605.0, "device", "plug", (True, False, None))]
    [trip] = _run(events + _drop("plug", 620) + _drop("tv", 603))
    assert trip.result.suspected == ["tv"]


def test_an_unreadable_report_after_the_circuit_came_back_is_still_a_trip():
    [trip] = _run(_panel(c1_rows={618: None}) + _drop("tv", 603))
    assert trip.kind == b.TRIP
    assert trip.end == 612.0


def test_a_sparse_circuit_does_not_outrun_the_stable_period():
    """Reporting every 150 s: the records the stable check needs are kept."""
    events = [
        (float(t), "circuit", "c1", 0.0 if t == 600 else 300.0)
        for t in range(0, 1200, 150)
    ]
    readings = [
        (float(t), "device", "meter", (True, True, 40.0)) for t in range(2, 600, 5)
    ]
    readings += _drop("meter", 160, back=170)
    readings = [r for r in readings if r[0] not in (162.0, 167.0)]
    readings.append((605.0, "device", "meter", (False, True, None)))
    # Back and reporting again, so its own log reaches the close.
    readings.append((650.0, "device", "meter", (True, False, 40.0)))
    readings += [
        (float(t), "device", "meter", (True, True, 40.0)) for t in range(655, 900, 5)
    ]
    [trip] = _run(events + readings + _drop("tv", 603))
    assert trip.last_live == 450.0
    assert trip.result.suspected == ["tv"]


def test_a_circuit_that_died_long_before_is_not_together():
    c2 = {t: 0.0 for t in range(505, 900, 6)}
    [outcome] = _run(_panel(c2_rows=c2))
    assert outcome.kind == b.TRIP


def test_a_device_that_flapped_just_before_is_not_counted():
    events = _panel() + _drop("radar", 400, back=420) + _drop("radar", 603)
    [trip] = _run(events + _drop("tv", 603))
    assert trip.result.suspected == ["tv"]


def test_a_device_already_unavailable_is_not_counted():
    """Unavailable before the circuit died; back and gone again inside it."""
    events = _panel() + _drop("plug", 100, back=605) + _drop("plug", 620)
    [trip] = _run(events + _drop("tv", 603))
    assert trip.result.suspected == ["tv"]


def test_a_device_that_dropped_and_recovered_long_before_is_not_counted():
    events = _panel() + _drop("old", -2000, back=-1900)
    [trip] = _run(events + _drop("tv", 603))
    assert trip.result.suspected == ["tv"]


def test_a_device_dropping_within_the_grace_is_counted():
    """The chime on the recorded flip went 7 s after the circuit came back."""
    [trip] = _run(_panel() + _drop("chime", 619))
    assert trip.result.suspected == ["chime"]


def test_a_device_dropping_after_the_close_is_not_counted():
    """Even when the close runs late and later drops have been fed."""
    events = _panel(dead=(600, 612)) + _drop("late", 650) + _drop("tv", 603)
    [trip] = _run([e for e in events if e[0] <= 660], close_at=700.0)
    assert trip.result.suspected == ["tv"]


def test_a_meter_collapsing_is_confirmed():
    readings = [
        (float(t), "device", "lamp", (True, True, 0.0 if t >= 602 else 60.0))
        for t in range(2, 900, 5)
    ]
    [trip] = _run(_panel() + readings)
    assert trip.result.confirmed == ["lamp"]


def test_a_meter_going_unavailable_is_suspected():
    readings = [
        (float(t), "device", "plug", (True, True, 40.0)) for t in range(2, 600, 5)
    ]
    readings.append((603.0, "device", "plug", (False, True, None)))
    [trip] = _run(_panel() + readings)
    assert trip.result.suspected == ["plug"]


def test_an_outage_still_dead_at_the_cap_is_reported_without_an_end():
    [trip] = _run(_panel(dead=(600, 900)) + _drop("tv", 603))
    assert trip.kind == b.TRIP
    assert trip.end is None
    assert trip.result.suspected == ["tv"]


def test_a_circuit_unreadable_mid_outage_is_not_a_trip():
    [outcome] = _run(_panel(c1_rows={606: None}) + _drop("tv", 603))
    assert outcome.kind == b.LOST
    assert outcome.end is None
    assert "unreadable" in outcome.reason
    assert outcome.to_dict()["suspected"] == []


def test_a_circuit_not_seen_drawing_before_it_died_is_not_a_trip():
    rows = {t: None for t in range(0, 594, 6)}
    rows[588] = 300.0
    [outcome] = _run(_panel(c1_rows=rows))
    assert outcome.kind == b.UNSEEN
    assert "not seen drawing" in outcome.reason


def test_a_meter_first_heard_inside_the_outage_uses_the_reading_it_replaced():
    """Home Assistant reports a change, not a stream: 50 W was in force."""
    report = [(603.0, "device", "fan", (True, True, 0.0, 50.0))]
    [trip] = _run(_panel() + report)
    assert trip.result.confirmed == ["fan"]


def test_a_meter_with_no_known_reading_before_is_not_confirmed():
    report = [(603.0, "device", "fan", (True, True, 0.0))]
    [trip] = _run(_panel() + report)
    assert trip.result.confirmed == []
    assert trip.result.unaffected == ["fan"]


def test_the_close_waits_for_the_grace():
    detector = b.TripDetector(["c1"], alive_w=4.0)
    detector.eligible = frozenset({"c1"})
    for t, w in ((-60.0, 300.0), (0.0, 300.0), (6.0, 0.0), (12.0, 300.0)):
        detector.circuit("c1", w, t)
    assert detector.due() == 12.0 + b.GRACE_S
    assert detector.close(12.0 + b.GRACE_S - 1) is None
    assert detector.close(12.0 + b.GRACE_S).kind == b.TRIP
