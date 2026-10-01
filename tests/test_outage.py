"""Outage evidence: Zigbee probes, Z-Wave node status and TV state across a trip.

Timings are a real trip, relative to the circuit reading dead: a TV off at
+14 s, four Z-Wave nodes dead from +53 to +118 s, the circuit reading live at
+291 s, the nodes alive again at +285 to +287 s and the TV on at +371 s.
"""

from __future__ import annotations

from custom_components.power_fingerprint.outage import (
    AFTER,
    DURING,
    MEDIA,
    METER_LAG_S,
    PROBE_DELAY_S,
    RETURN_S,
    ZIGBEE,
    ZWAVE,
    OutageEvidence,
    ProbeResult,
    merge_outage,
)

FALL = 1000.0
LIVE = FALL + 291.0


def _trip() -> OutageEvidence:
    return OutageEvidence("sensor.circuit_7", FALL)


def test_a_node_dead_in_the_outage_and_alive_with_the_power_is_a_casualty() -> None:
    ev = _trip()
    ev.track_node("couch", alive=True, stable=True)
    ev.node("couch", FALL + 53, alive=False)
    ev.restore(LIVE)
    # Alive 6 s before the circuit's first live reading: the meter lags.
    ev.node("couch", LIVE - 6, alive=True)
    verdict = ev.judge()
    assert verdict.casualties["couch"] == {
        "signal": ZWAVE,
        "down_at": FALL + 53,
        "back_at": LIVE - 6,
    }


def test_a_node_back_while_the_circuit_was_still_dead_is_not_evidence() -> None:
    ev = _trip()
    ev.track_node("flap", alive=True, stable=True)
    ev.node("flap", FALL + 20, alive=False)
    ev.node("flap", FALL + 40, alive=True)
    ev.restore(LIVE)
    verdict = ev.judge()
    assert "flap" not in verdict.casualties
    assert "flap" in verdict.indeterminate


def test_nodes_dead_or_unsteady_before_the_fall_say_nothing() -> None:
    ev = _trip()
    ev.track_node("already_dead", alive=False, stable=True)
    ev.track_node("unsteady", alive=True, stable=False)
    for device in ("already_dead", "unsteady"):
        ev.node(device, FALL + 50, alive=False)
        ev.node(device, LIVE + 5, alive=True)
    ev.restore(LIVE)
    verdict = ev.judge()
    assert verdict.casualties == {}
    assert verdict.indeterminate == []


def test_a_node_that_never_comes_back_or_goes_after_the_return_is_indeterminate() -> (
    None
):
    ev = _trip()
    ev.track_node("gone", alive=True, stable=True)
    ev.node("gone", FALL + 60, alive=False)
    ev.track_node("late", alive=True, stable=True)
    ev.node("late", LIVE + 10, alive=False)
    ev.node("late", LIVE + 20, alive=True)
    ev.restore(LIVE)
    ev.node("gone", LIVE + RETURN_S + 1, alive=True)
    verdict = ev.judge()
    assert verdict.casualties == {}
    assert verdict.indeterminate == ["gone", "late"]


def test_a_tv_cut_off_with_the_circuit_is_a_casualty_and_one_switched_off_is_not() -> (
    None
):
    ev = _trip()
    for device in ("tv", "tv_late", "tv_person", "tv_stays_off"):
        ev.track_media(device, on=True, stable=True)
    ev.media("tv", FALL + 14, on=False, by_person=False)
    ev.media("tv_late", FALL + 45, on=False, by_person=False)
    ev.media("tv_person", FALL + 5, on=False, by_person=True)
    ev.media("tv_stays_off", FALL + 10, on=False, by_person=False)
    ev.restore(LIVE)
    for device in ("tv", "tv_late", "tv_person"):
        ev.media(device, LIVE + 80, on=True, by_person=False)
    verdict = ev.judge()
    assert verdict.casualties == {
        "tv": {"signal": MEDIA, "down_at": FALL + 14, "back_at": LIVE + 80}
    }
    assert verdict.indeterminate == ["tv_late", "tv_person", "tv_stays_off"]


def _probe(device: str, phase: str, sent: float, answered: bool | None) -> ProbeResult:
    return ProbeResult(device, phase, sent, sent + 10.0, answered)


def test_a_zigbee_failure_counts_only_beside_an_answer_after_the_return() -> None:
    ev = _trip()
    ev.probe(_probe("outlet", DURING, FALL + 15, False))
    ev.probe(_probe("broken", DURING, FALL + 25, False))
    ev.probe(_probe("elsewhere", DURING, FALL + 35, True))
    ev.restore(LIVE)
    ev.probe(_probe("outlet", AFTER, LIVE + 30, True))
    ev.probe(_probe("broken", AFTER, LIVE + 30, False))
    verdict = ev.judge()
    assert verdict.casualties == {
        "outlet": {
            "signal": ZIGBEE,
            "failed_at": FALL + 25,
            "answered_at": LIVE + 40,
        }
    }
    assert verdict.answered == ["elsewhere"]
    assert verdict.indeterminate == ["broken"]


def test_an_answer_with_the_circuit_dead_outweighs_a_failure() -> None:
    ev = _trip()
    ev.probe(_probe("router_child", DURING, FALL + 15, False))
    ev.probe(_probe("router_child", DURING, FALL + 60, True))
    ev.restore(LIVE)
    ev.probe(_probe("router_child", AFTER, LIVE + 30, True))
    verdict = ev.judge()
    assert verdict.casualties == {}
    assert verdict.answered == ["router_child"]


def test_a_request_finished_near_the_return_says_nothing() -> None:
    ev = _trip()
    ev.restore(LIVE)
    ev.probe(ProbeResult("edge", DURING, LIVE - 40, LIVE - METER_LAG_S + 1, False))
    ev.probe(_probe("edge", AFTER, LIVE + 30, True))
    assert ev.failed_zigbee() == []
    verdict = ev.judge()
    assert verdict.casualties == {}
    assert verdict.indeterminate == ["edge"]


def test_failed_zigbee_lists_what_to_ask_again() -> None:
    ev = _trip()
    ev.probe(_probe("b", DURING, FALL + 15, False))
    ev.probe(_probe("a", DURING, FALL + 30, False))
    ev.probe(_probe("c", DURING, FALL + 45, True))
    ev.probe(_probe("d", DURING, FALL + 50, None))
    assert ev.failed_zigbee() == ["a", "b"]


def test_waiting_holds_while_a_device_is_down_inside_the_return_window() -> None:
    ev = _trip()
    ev.track_node("n", alive=True, stable=True)
    ev.node("n", FALL + 50, alive=False)
    assert ev.waiting(FALL + 100)  # still dead
    ev.restore(LIVE)
    assert ev.waiting(LIVE + 10)
    ev.node("n", LIVE + 8, alive=True)
    assert not ev.waiting(LIVE + 10)
    other = _trip()
    other.track_node("m", alive=True, stable=True)
    other.node("m", FALL + 50, alive=False)
    other.restore(LIVE)
    assert not other.waiting(LIVE + RETURN_S)


def test_tracks_take_the_first_state_seen() -> None:
    ev = _trip()
    ev.track_node("n", alive=True, stable=True)
    ev.track_node("n", alive=False, stable=False)
    ev.node("n", FALL + 50, alive=False)
    ev.restore(LIVE)
    ev.node("n", LIVE + 5, alive=True)
    assert "n" in ev.judge().casualties
    # Changes for a device never tracked are ignored.
    ev.node("stranger", FALL + 50, alive=False)
    ev.media("stranger", FALL + 50, on=False, by_person=False)
    assert "stranger" not in ev.judge().indeterminate


def test_before_the_return_nothing_is_judged_and_unsteady_devices_never_wait() -> None:
    ev = _trip()
    ev.track_node("n", alive=True, stable=True)
    ev.track_node("unsteady", alive=True, stable=False)
    ev.node("n", FALL + 50, alive=False)
    ev.node("unsteady", FALL + 50, alive=False)
    verdict = ev.judge()
    assert (verdict.casualties, verdict.indeterminate) == ({}, [])
    ev.restore(LIVE)
    ev.node("n", LIVE + 5, alive=True)
    # The unsteady node never came back, and holds nothing open.
    assert not ev.waiting(LIVE + 10)


def test_an_answer_after_the_return_window_says_nothing() -> None:
    ev = _trip()
    ev.probe(_probe("slow", DURING, FALL + 15, False))
    ev.restore(LIVE)
    ev.probe(ProbeResult("slow", AFTER, LIVE + 100, LIVE + RETURN_S + 1, True))
    verdict = ev.judge()
    assert verdict.casualties == {}
    assert verdict.indeterminate == ["slow"]


def test_a_return_that_did_not_hold_is_not_the_return() -> None:
    ev = _trip()
    flicker = FALL + 100
    ev.probe(_probe("outlet", DURING, FALL + 15, False))
    # In flight when the power came back for a moment: says nothing.
    ev.probe(ProbeResult("edge", DURING, flicker - 5, flicker + 2, False))
    ev.restore(flicker)
    ev.probe(_probe("outlet", AFTER, flicker + 1, False))
    ev.dead_again(flicker + 10)
    assert ev.restored is None
    assert ev.asked_while_dead() == {"outlet"}
    # Asked again once the supplies drained.
    ev.probe(_probe("edge", DURING, flicker + 10 + PROBE_DELAY_S, False))
    ev.restore(LIVE)
    ev.probe(_probe("outlet", AFTER, LIVE + 30, True))
    ev.probe(_probe("edge", AFTER, LIVE + 30, True))
    verdict = ev.judge()
    assert set(verdict.casualties) == {"outlet", "edge"}
    assert verdict.casualties["edge"]["failed_at"] == flicker + 20 + PROBE_DELAY_S
    assert verdict.casualties["outlet"]["answered_at"] == LIVE + 40


def test_a_second_write_adds_to_the_first() -> None:
    first = {
        "casualties": {"a": {"signal": ZIGBEE}},
        "answered": ["b"],
        "indeterminate": ["c", "d"],
        "recovered": {},
    }
    second = {
        "casualties": {"c": {"signal": ZWAVE}},
        "answered": ["a"],
        "indeterminate": [],
        "recovered": {"d": {"failed_at": "x"}},
    }
    merged = merge_outage(first, second)
    assert merged["casualties"] == {"a": {"signal": ZIGBEE}, "c": {"signal": ZWAVE}}
    # One place per device, a casualty first.
    assert merged["answered"] == ["b"]
    assert merged["recovered"] == {"d": {"failed_at": "x"}}
    assert merged["indeterminate"] == []
    assert merge_outage(None, first) == first
