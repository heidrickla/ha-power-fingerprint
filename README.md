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

Four things in `analysis.py` exist because the obvious approach failed first,
and each has a regression test.

**Thresholds make no duty-cycle assumption.** A first pass used `idle × 3`,
which scored zero runs on two circuits drawing 663 W and 353 W continuously —
on a high-baseline circuit that lands above the 99th percentile. Switching to a
fixed percentile was no better: the 40th percentile assumes a circuit is mostly
off, and on a furnace at a **68% duty cycle** it lands *inside* a run, reporting
zero runs across 98,849 samples. Both failures are silent. Otsu's method is used
instead, because it assumes nothing about how often a load is on.

**An event keeps its own dips.** If an event held only its above-threshold
samples, the floor would be pinned at the threshold by construction and could
never be low — destroying the very feature below. Events retain every sample
inside their time span.

**Duration does not decide identity.** Weighted into the clustering, it split
one furnace into four clusters that were identical in power (156 W peak, 110 W
floor, 2 plateaus) and differed only in how long each run happened to last. A
machine is identified by the power it draws.

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

## Self-metering devices

Most homes with per-circuit monitoring also have devices that meter themselves —
smart dimmers, metered outlets, a PDU reporting per outlet. The development
install has **40 of them alongside 27 circuits**, and they are worth more than
one extra reading each because they are *already labelled*:
`sensor.front_porch_light_active_power` needs no human to name it.

That gives three things circuit CTs alone cannot: labelled fingerprints for
free, subtraction of a known device from a shared circuit, and automatic
device-to-circuit mapping.

⚠ **Attribution is implemented but NOT yet reliable — treat its output as a
suggestion.** Correlation alone placed only 2 of 20 devices, because a 10 W lamp
contributes almost nothing to the variance of a circuit swinging hundreds of
watts. Step matching — does the circuit show a coincident step of matching
*magnitude* when the device switches — lifted that to 13. Requiring the winner
to beat the runner-up then correctly refused four office lights that always
switch together and so score identically everywhere.

But checked against Home Assistant's own area assignments, one busy circuit
still claims devices from three unrelated areas. Only assignments with a high
step match *and* a clear margin should be trusted, and the tool prints both so
you can judge. Verifying against area data is the next piece of work.

## Roadmap

- **Fingerprint learning** — cluster recurring event shapes per circuit, then
  ask once which is which. Unsupervised clustering finds the shapes but cannot
  name them; a human names them in one pass and it remembers. Metered devices
  skip the asking entirely.
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
