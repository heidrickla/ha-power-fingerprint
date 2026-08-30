# Power Fingerprint

Identify what is running on a circuit from the shape of its load.

Home Assistant integration for panels with per-circuit power monitoring
(developed against an Emporia Vue, but nothing here is vendor-specific — it
works with any `device_class: power` sensors).

## Why this is not NILM

Classic non-intrusive load monitoring tries to pull thirty overlapping
appliances out of a single meter reading at the mains. That is genuinely hard
and is why the field leans on neural networks.

This solves the much easier problem: you already have a CT on each circuit, so
the separation NILM works to recover is already done in hardware. What is left
is telling apart the two or three appliances that share one circuit — which
turns out to need a handful of well-chosen features and no machine learning at
all.

On the development install the circuits sum to within **28 W of a 6,556 W
mains reading**. There is nothing meaningful left to disaggregate.

## What ships in 0.1

Three checks that need **no labelled fingerprints** and work the moment you
install it:

| Entity | What it tells you |
|---|---|
| `sensor.unmonitored_load` | Mains minus the sum of the circuits. Near zero on a fully clamped panel. |
| `binary_sensor.ct_coverage_fault` | That figure moved outside tolerance — a clamp came off, a CT reversed, or an unmonitored load appeared. |
| `sensor.standby_power` | Total permanent draw, with a per-circuit ranking in the attributes. |
| `sensor.standby_annual_cost` | What that costs per year at your price. |
| `binary_sensor.state_contradiction` | A switch reports `on` while its circuit draws nothing. |

The last one is worth explaining. It catches a welded relay, a dead lamp, a
smart plug that reports state without actually switching, and a breaker that
tripped under a device still showing its last known state. It is *verify by
effect* as a background service: the state machine says one thing, the current
clamp says another, and the clamp is the one measuring reality.

The CT coverage check earns its place for a similar reason. A current clamp
reading zero is otherwise indistinguishable from an appliance that is switched
off — a distinction that cost real debugging time during development, twice, on
the same circuit.

## Configuration

Settings → Devices & Services → Add Integration → Power Fingerprint.

- **Whole-panel power sensor** — the mains reading
- **Circuit power sensors** — every per-circuit sensor you want included
- **Price per kWh** — for the standby cost figure
- **Coverage tolerance** — percent of mains the remainder may drift before a fault
- **Contradiction pairs** — one `switch.entity: sensor.circuit_power` per line

## Design notes

Three things in `analysis.py` exist because the obvious approach failed first.

**Thresholds are per-circuit and percentile-based.** A first pass used
`idle × 3` as the on-threshold. It scored zero runs on two circuits drawing
663 W and 353 W continuously, because on a circuit with a high constant
baseline that lands above the 99th percentile.

**Minimum run length is configurable and defaults low.** The same pass silently
discarded a garbage disposal with a 423 W peak, because it used a two-minute
floor and a disposal runs for twenty seconds.

**The floor while running is a first-class feature.** A gas dryer and a washing
machine sharing a circuit have near-identical peaks and are not separable on
peak, mean or duration. They separate instantly on the *minimum* while running:
the washer drops to near zero between fill, soak and spin, while the dryer holds
a steady 350–400 W floor. Most published feature sets omit this.

### Known limit

At the ~12 s sample interval an Emporia Vue reports, a 30-second appliance is
two or three data points. Kettles, microwaves and disposals sit at or below the
resolution limit. That is a property of the meter, not something more code
fixes.

## Roadmap

Labelling comes next, and unlocks the rest:

- **Fingerprint learning** — cluster recurring event shapes per circuit, then
  ask once which is which. Unsupervised clustering finds the shapes but cannot
  name them; a human names them in one pass and it remembers.
- **Absence detection** — alert when an expected signature *fails* to appear.
  The fridge stopped cycling, the sump pump was silent through a storm. Every
  tool alerts on too much; the expensive failures are silence.
- **Signature drift as health** — compare an appliance against its own history,
  not a spec. A fridge duty cycle creeping 40% → 70%, a compressor's inrush
  changing, a pump's start current climbing.
- **Runtime-based maintenance** — blower hours since filter change, dryer
  cycles since duct clean, pump starts.
- **Away-mode safety** — alarm armed away plus a 2 kW load on the oven circuit.
- **Tariff-aware shifting** — fingerprints plus a tariff become "run the dryer
  after 9pm".
- **Unknown-signature detection** — an unrecognised shape means something new
  was plugged in, or an appliance changed behaviour.

### Deliberate non-goals

- **No deep learning.** The data volumes do not justify it and an unexplainable
  classifier is worthless the moment it drives an alert or an automation. The
  single most useful feature so far — the floor while running — came from
  domain knowledge, not feature discovery.
- **No whole-house NILM from the mains.** The hard version of a problem the CT
  clamps already solved.
- **No occupancy inference.** Power traces reveal when people shower, sleep and
  leave the house. That capability arrives free whether or not it is wanted, so
  it is declined deliberately rather than by omission.

## Licence

MIT.
