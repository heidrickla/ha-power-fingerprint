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

# ONLY A LAST RESORT. `price.async_dashboard_price` is asked first, because a
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

# Standby is a 5th percentile, so it needs to span a duty cycle or two of
# whatever cycles slowest. Below this the sensors report `unknown` rather than a
# number from a window too short to mean anything.
# How often the poll heartbeat is persisted while nothing else is dirty.
# After a hard crash the un-persisted stretch is credited as blind time, so
# minutes - small next to any appliance cadence - not hours.
HEARTBEAT_SECONDS = 300

MIN_STANDBY_WINDOW_HOURS = 1.0


CONF_CONFIDENCE = "confidence"
DEFAULT_CONFIDENCE = "balanced"

# One dial rather than a dozen thresholds: these were found by measuring
# against a house with 27 clamps to check answers against, which nobody else
# has, so separate numeric knobs would be controls with no feedback.
#
# The two ways to be wrong are not symmetric. A missing answer is visible; a
# wrong one looks exactly like a right one and gets believed.
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
    # More placements, and some of them wrong in a way you cannot see. Sound
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
