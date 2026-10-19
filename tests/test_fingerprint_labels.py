"""Tests for label carrying across a re-learn."""

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "fingerprint.py"
)
_spec = importlib.util.spec_from_file_location("pf_fingerprint", _PATH)
fp = importlib.util.module_from_spec(_spec)
sys.modules["pf_fingerprint"] = fp
_spec.loader.exec_module(fp)


def make(label, circuit="sensor.c1", count=1, watts=None):
    centroid = {} if watts is None else {"peak_w": watts, "floor_w": watts * 0.8}
    return fp.Fingerprint(label=label, circuit=circuit, count=count, centroid=centroid)


def test_unnamed_is_not_an_identification():
    assert not fp.is_named(make("unnamed_0"))
    assert fp.is_named(make("Dryer"))


def test_relearning_keeps_human_labels():
    old = [make("Dryer", watts=2400), make("Washer", watts=500)]
    new = [make("unnamed_0", watts=2350), make("unnamed_1", watts=520)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Dryer", "Washer"]


def test_a_name_follows_its_shape_when_the_size_order_changes():
    """Clusters come largest first, so a week of more washing than drying
    reorders them. The name goes with the shape, not the position."""
    old = [make("Dryer", watts=2400), make("Washer", watts=500)]
    new = [make("unnamed_0", watts=520), make("unnamed_1", watts=2350)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Washer", "Dryer"]


def test_relearning_does_not_invent_labels_for_extra_clusters():
    """A re-run finding MORE shapes must leave the new ones unnamed."""
    old = [make("Dryer", watts=2400)]
    new = [
        make("unnamed_0", watts=2350),
        make("unnamed_1", watts=500),
        make("unnamed_2", watts=60),
    ]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Dryer", "unnamed_1", "unnamed_2"]


def test_unnamed_old_labels_are_not_carried():
    """Carrying 'unnamed_0' forward would be noise, not information."""
    old = [make("unnamed_0", watts=2400), make("Washer", watts=500)]
    new = [make("unnamed_0", watts=2350), make("unnamed_1", watts=520)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["unnamed_0", "Washer"]


def test_a_name_is_not_carried_onto_a_shape_the_match_would_refuse():
    """Nearest is not enough: past the match tolerance the new shape is
    another appliance, and the named shape is kept instead."""
    old = [make("Dryer", watts=2400)]
    new = [make("unnamed_0", watts=300)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["unnamed_0", "Dryer"]
    assert out[1] is old[0]


def test_a_named_shape_that_did_not_run_is_kept():
    """A second stage or a heater can sit out a whole window; its name and
    shape stay, after the new shapes."""
    stage2 = make("Stage 2", watts=2700, count=40)
    old = [make("Stage 1", watts=1500), stage2]
    new = [make("unnamed_0", watts=1520)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Stage 1", "Stage 2"]
    assert out[1] is stage2


def test_one_name_per_new_shape_and_the_nearest_wins():
    """Two named shapes near one new shape: the nearer gives its name, and
    the other is kept rather than naming the same shape twice."""
    old = [make("Keurig", watts=1500), make("Kettle", watts=1400)]
    new = [make("unnamed_0", watts=1490)]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Keurig", "Kettle"]


def test_match_refuses_rather_than_guessing():
    """An unmatched event means something new was plugged in - it must not be
    forced into the nearest bucket."""
    library = [
        fp.Fingerprint(
            label="Dryer",
            circuit="sensor.c1",
            count=5,
            centroid={
                "peak_w": 800.0,
                "floor_w": 380.0,
                "mean_w": 400.0,
                "plateaus": 3,
                "duty_above_half_peak": 0.9,
            },
        )
    ]
    nothing_like_it = {
        "peak_w": 12.0,
        "floor_w": 1.0,
        "mean_w": 8.0,
        "plateaus": 1,
        "duty_above_half_peak": 0.1,
    }
    matched, dist = fp.match(nothing_like_it, library)
    assert matched is None
    assert dist > 1.0


# --- reading the name off the circuit ---------------------------------------


def test_a_named_circuit_yields_its_appliance():
    assert fp.suggest_label("EmporiaVue Circuit 5 Dish Washer Power") == "Dish Washer"
    assert fp.suggest_label("EmporiaVue Circuit 13 Furnace Central Power") == (
        "Furnace Central"
    )


def test_a_multi_breaker_designation_is_stripped():
    """ "Circuit 7 & 9" is an address, not part of the appliance's name."""
    assert (
        fp.suggest_label("EmporiaVue Circuit 7 & 9 Air Conditioner Bedrooms Power")
        == "Air Conditioner Bedrooms"
    )


def test_an_unnamed_circuit_returns_none_rather_than_a_number():
    """ "Circuit 25" names a breaker, not an appliance."""
    assert fp.suggest_label("EmporiaVueSecondary Circuit 25 Power") is None
    assert fp.suggest_label("EmporiaVue Circuit 16 Power") is None


def test_the_secondary_unit_prefix_is_stripped_too():
    assert fp.suggest_label("EmporiaVueSecondary Circuit 14 Washer Power") == "Washer"


def test_a_dashboard_style_name_yields_the_room_or_appliance():
    """The energy dashboard is where the good names live.

    Entity titles come from the meter's firmware; these come from a person.
    """
    assert fp.suggest_label("Circuit 19 Garage") == "Garage"
    assert fp.suggest_label("Circuit 20 Microwave") == "Microwave"
    assert fp.suggest_label("Circuit 1 & 3 Oven") == "Oven"
    assert fp.suggest_label("Circuit 18 Breakfast, Kitchen Lights") == (
        "Breakfast, Kitchen Lights"
    )


def test_a_dashboard_name_that_is_only_a_number_still_returns_none():
    assert fp.suggest_label("Circuit 30") is None
    assert fp.suggest_label("Circuit 32") is None


def test_a_leading_bare_number_is_part_of_the_address_not_the_name():
    """Some panels are labelled "16 Study", with no "Circuit" in front. The
    number is still the breaker, not the appliance."""
    assert fp.suggest_label("16 Study") == "Study"
    assert fp.suggest_label("16") is None


# --- label names (a label wants the breaker number; an appliance name does not)


def _label_name(title, prefix="Circuit"):
    """Mirror of services._label_name, which cannot be imported without HA."""
    cleaned = " ".join(
        w for w in title.split() if not w.lower().startswith("emporiavue")
    )
    for noise in (" Power", " Energy"):
        if cleaned.endswith(noise):
            cleaned = cleaned[: -len(noise)]
    cleaned = cleaned.strip()
    if not cleaned:
        return prefix
    if cleaned.lower().startswith(prefix.lower()):
        return cleaned
    return f"{prefix} {cleaned}".strip()


def test_a_dashboard_name_is_already_a_good_label():
    """A dashboard name is already a label; it must not be re-prefixed."""
    assert _label_name("Circuit 30") == "Circuit 30"
    assert _label_name("Circuit 23 Study") == "Circuit 23 Study"


def test_a_firmware_title_is_trimmed_but_keeps_its_number():
    """Unlike an appliance name, a label wants the breaker it refers to."""
    assert _label_name("EmporiaVue Circuit 5 Dish Washer Power") == (
        "Circuit 5 Dish Washer"
    )
    assert _label_name("EmporiaVueSecondary Circuit 25 Power") == "Circuit 25"
