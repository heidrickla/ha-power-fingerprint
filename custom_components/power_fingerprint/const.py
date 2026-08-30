"""Constants for Power Fingerprint."""

DOMAIN = "power_fingerprint"
MANUFACTURER = "Power Fingerprint"
# Must match manifest.json. HACS surfaces the release TAG while Home Assistant
# reports the MANIFEST version, so a mismatch is a defect users see as a wrong
# version number. Bump both together.
VERSION = "0.17.0"

CONF_MAINS = "mains"
CONF_CIRCUITS = "circuits"
CONF_PRICE = "price_per_kwh"
CONF_TOLERANCE = "coverage_tolerance_pct"
CONF_PAIRS = "contradiction_pairs"

# ⭐ ONLY A LAST RESORT. `price.async_dashboard_price` is asked first, because a
# second place to type your tariff is a second place for it to be wrong, and the
# wrong one is always the one nobody looks at. Measured: a dashboard holding
# $0.145/kWh against this constant made every standby cost read 11% low.
DEFAULT_PRICE = 0.13
# A well-clamped panel sums to within a few percent of its mains. Measured on
# the development install: 27 circuits summed to 6527 W against a 6556 W mains
# reading, a remainder of 28 W or 0.4%. 5% is comfortably outside normal noise
# while still catching a single mid-size CT dropping out.
DEFAULT_TOLERANCE = 5.0

# How long a window to keep for standby/floor calculations. Standby is the 5th
# percentile over this window, so it needs to span at least one full duty cycle
# of the slowest cycling appliance on the circuit.
WINDOW_HOURS = 24
POLL_SECONDS = 30

# ⛔ HOW LITTLE OF THE WINDOW IS STILL TOO LITTLE TO ANSWER. Standby is a 5th
# percentile, so it needs to span at least a duty cycle or two of whatever
# cycles slowest on the circuit. Measured on the development install: the
# central AC runs 42% of a day, and a window covering only the minute after a
# restart reported 5,017 W of "standby" against a true 24-hour figure of 3 W.
# Below this span the sensors report `unknown`, which is the honest answer and
# is not the same as reporting a number nobody should trust.
MIN_STANDBY_WINDOW_HOURS = 1.0


CONF_CONFIDENCE = "confidence"
DEFAULT_CONFIDENCE = "balanced"

# ⭐ ONE DIAL THE USER CAN REASON ABOUT, NOT TWELVE THEY CANNOT.
#
# Every threshold below was arrived at by measuring against a house with 27
# real clamps to check answers against. Almost nobody installing this has that,
# so exposing `min_step_match` and `min_margin` as separate numbers would be
# handing over controls with no way to tell whether turning them helped. What a
# person CAN say is how they would rather be wrong.
#
# ⛔ THE TWO WAYS TO BE WRONG ARE NOT SYMMETRIC. A missing answer is visible -
# the device simply has no circuit. A wrong answer is invisible: it looks
# exactly like a right one, gets written onto a device as a label, and is
# believed. `cautious` is therefore the safe end and `eager` carries a warning.
CONFIDENCE_PROFILES: dict[str, dict[str, float]] = {
    # Answers only where the evidence clearly beats coincidence. Expect roughly
    # half as many placements as `balanced`, and to trust all of them.
    "cautious": {
        "min_step_match": 0.60,
        "min_margin": 0.30,
        "min_correlation": 0.65,
        "min_lift": 0.35,
        "probes": 3,
        "cluster_threshold": 0.70,
        "absence_patience": 3.0,
    },
    # What the measurements in the README were taken with.
    "balanced": {
        "min_step_match": 0.35,
        "min_margin": 0.15,
        "min_correlation": 0.50,
        "min_lift": 0.15,
        "probes": 2,
        "cluster_threshold": 0.90,
        "absence_patience": 2.0,
    },
    # ⚠ More placements, and some of them wrong in a way you cannot see. Sound
    # choice while exploring a panel, poor one for driving automations.
    "eager": {
        "min_step_match": 0.25,
        "min_margin": 0.05,
        "min_correlation": 0.35,
        "min_lift": 0.05,
        "probes": 1,
        "cluster_threshold": 1.20,
        "absence_patience": 1.5,
    },
}


def profile(name: str | None) -> dict[str, float]:
    """The thresholds for a confidence setting, falling back to balanced."""
    return CONFIDENCE_PROFILES.get(
        str(name or ""), CONFIDENCE_PROFILES[DEFAULT_CONFIDENCE]
    )
