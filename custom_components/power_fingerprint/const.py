"""Constants for Power Fingerprint."""

DOMAIN = "power_fingerprint"
MANUFACTURER = "Power Fingerprint"
# Must match manifest.json. HACS surfaces the release TAG while Home Assistant
# reports the MANIFEST version, so a mismatch is a defect users see as a wrong
# version number. Bump both together.
VERSION = "0.9.0"

CONF_MAINS = "mains"
CONF_CIRCUITS = "circuits"
CONF_PRICE = "price_per_kwh"
CONF_TOLERANCE = "coverage_tolerance_pct"
CONF_PAIRS = "contradiction_pairs"

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
