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

Plus, once you have named at least one fingerprint on a circuit, an
**appliance sensor** for that circuit reading `idle`, `starting`, the appliance
name, or `unknown`.

`unknown` is not a failure. It means the circuit is drawing power in a shape no
named fingerprint accounts for — something new was plugged in, or an appliance
changed behaviour. Forcing that into the nearest bucket would throw away the
most interesting reading the integration produces.

Matching runs on the samples seen *so far*, not on a completed run, so an
automation can react while the appliance is still going. That is only possible
because duration and energy carry zero weight for identity — every feature that
counts is computable mid-run.

The contradiction sensor is worth explaining. It catches a welded relay, a dead lamp, a
smart plug that reports state without actually switching, and a breaker that
tripped under a device still showing its last known state. It is *verify by
effect* as a background service: the state machine says one thing, the current
clamp says another, and the clamp is the one measuring reality.

The CT coverage check earns its place for a similar reason. A current clamp
reading zero is otherwise indistinguishable from an appliance that is switched
off — a distinction that cost real debugging time during development, twice, on
the same circuit.

## Installation

**HACS (custom repository)** — add this repository as a custom repository of
type *Integration*, install, restart Home Assistant.

**Manual** — copy `custom_components/power_fingerprint/` into your Home
Assistant `config/custom_components/` directory and restart.

Then: Settings -> Devices & Services -> Add Integration -> Power Fingerprint.

All entities group under a single service device, and
`Download diagnostics` on that device produces a report with entity ids
pseudonymised — this integration's entity names describe rooms and appliances,
and diagnostics files routinely end up in public issue trackers.

Publication status and the HACS submission checklist are in
[PUBLISHING.md](PUBLISHING.md).

## Learning fingerprints

Two services. The split matters: clustering finds the recurring shapes and
genuinely cannot name them, because naming needs someone who knows what is
plugged in.

```yaml
# Propose candidates from history. Returns a description of each shape.
action: power_fingerprint.learn
data:
  days: 7
```

```text
sensor.circuit_21_power: 22 runs -> 4 distinct shape(s)
  [unnamed_0]  7 runs - runs 5 min,  peaks 335 W, holds a 212 W floor, 3 levels
  [unnamed_1]  7 runs - runs 8 min,  peaks 703 W, holds a 3 W floor,  12 levels
  [unnamed_2]  7 runs - runs 59 min, peaks 839 W, holds a 202 W floor, 11 levels
```

Read the floors: the ones holding ~200 W are the dryer, the one dropping to 3 W
is the washer. Then name them:

```yaml
action: power_fingerprint.label
data:
  circuit: sensor.circuit_21_power
  current_label: unnamed_1
  new_label: Washing machine
```

**Only named fingerprints drive anything.** An `unnamed_N` centroid is a
candidate, not an identification, and reporting "unnamed_2 is running" would be
worse than reporting nothing.

Re-running `learn` later keeps the names you applied, matched by position.
Position is used because clusters come back largest-first and a re-run over more
data usually preserves that order. When it does not, a label lands on the wrong
shape and you can see it and fix it — a visible wrong label beats silently
throwing your naming work away.

## Passive and active, together

Working out which circuit a device sits on is done two ways, and they are
complementary rather than alternatives.

**Passive** correlates a metered device's history against every circuit. It
covers every device that ever switches, across days, for free, without touching
anything. Assignment is iterative: the most confident match is committed first,
then that device's trace is **subtracted** from its circuit before anything else
is scored. A step already explained by a confirmed device is no longer available
to explain a different one — without that, one busy circuit keeps looking like a
plausible home for everything.

**Active** settles what passive cannot. It switches the device and watches which
circuit moves:

```yaml
action: power_fingerprint.verify_circuit
data:
  device: switch.office_lamp
  power_sensor: sensor.office_lamp_power   # strongly recommended
  probes: 2
```

The circuit's step is compared against what the *device* reported it drew, so an
unrelated load switching at the same moment is rejected rather than credited.
All probes must agree — one toggle can coincide with a fridge starting, two
cannot.

| Both agree | `confirmed` |
|---|---|
| Active only | `measured` |
| Passive only | `inferred` |
| **They disagree** | **`conflict` — neither is trusted** |

Disagreement deliberately returns nothing rather than preferring the
measurement. One of them is wrong about a physical fact that cannot be both
ways, and which one is not knowable from here. Reporting the conflict is
information; silently preferring a method hides a real problem behind a
confident answer.

### What active probing cannot do

Three limits, all found by running it against a real panel rather than reasoning
about it:

- **A device meter can be slower than the circuit meter.** A Z-Wave dimmer
  reported no power change at all across a 25-second probe, while the circuit CT
  reports every 12 seconds. Treated as *no reading* rather than a zero-watt
  change, and the probe falls back to "which circuit moved most" and says it did.
- **Very small loads are below the noise floor.** An RGB light drawing 1.1 W
  cannot be picked out of a circuit's normal variation. The probe refuses rather
  than guessing.
- **Battery devices cannot be attributed to a circuit at all** and are refused
  up front. They draw no mains current, so no CT will ever see them switch;
  probing one burns the full settle time to reach "no answer", and an unrelated
  load that moved during those minutes could be credited to it.
  ⚠ The test is *not* "has a battery sensor" — a UPS reports one and draws
  600 W, as do some thermostats and mains smoke alarms with a backup cell. It is
  a battery reading **and** no power, energy or current reading: a device that
  meters its own consumption is by definition consuming. Checked against a
  499-device install: 34 correctly excluded, and the UPS correctly kept.
- **An automation can move the device mid-probe and nothing in the power trace
  reveals it.** So the probe snapshots `last_triggered` for every automation
  that references the device or a circuit, either side of the measurement, and
  reports any that fired. A result with interference is graded `suspect` and the
  automation is named, rather than being silently trusted or silently discarded.

### Field results, 2026-08-30

First live run, three devices, two probes each, on a 27-circuit panel at 01:00.

| Device | Result |
|---|---|
| Foyer archway *(control)* | probe 2 → **circuit 30**, match **0.998** (39.3 W expected, 39.4 W measured) — **matching the passive inference exactly** |
| Front porch | probe 1 → circuit 30, match **0.68** (12.0 W expected, 17.6 W measured) |
| Office desk light | **1.1 W** — below the noise floor, refused on both probes |

All three devices restored to their prior state, verified afterwards rather than
assumed.

The control is the important one: two independent methods, one statistical and
one by measurement, produced the same circuit. That is what makes the rest of
this trustworthy.

⚠ **And eight automations fired during that window, three of them touching a
probed device** — including the outside-lights automation firing *during* the
front porch probe. That is very likely why the porch match was 0.68 rather than
0.99: the automation also controls the driveway and side-door lights, which may
share the circuit, adding a delta that was not the porch light.

So the porch result is **suspect, not accepted**. The interference detector was
built because of this run, and would now grade it as such automatically.

The wider lesson: 01:00 was chosen because the house should be quiet, and it was
not. Motion automations fired throughout — laundry, kitchen, garage. Probing
when the house *seems* quiet is not a substitute for detecting interference.

### Pausing interfering automations

Detecting interference is the default. Removing it is opt-in:

```yaml
action: power_fingerprint.verify_circuit
data:
  device: light.hall
  power_sensor: sensor.hall_power
  pause_automations: true
```

This switches off automations that touch the device or a circuit, probes, then
switches them back on. It is more invasive than detection, so it is guarded
three ways:

- **A denylist, deliberately over-broad.** Anything referencing a lock, alarm
  panel, cover, valve, water heater, climate, siren, notification, presence
  entity, or a moisture / smoke / gas / CO / safety sensor is **never paused**,
  and is reported back as refused. Refusing costs a noisier measurement; pausing
  the wrong thing costs a leak alert that never fires.
- **The record is written before the switch-off, not after.** If the process
  dies between the two, the worst case is a stale record that re-enables
  something already running.
- **Orphans are restored at startup.** If a probe dies mid-run — killed process,
  Home Assistant restart, power cut — the next setup re-enables anything left
  paused and logs a warning for each. A house whose automations are silently off
  is far worse than a bad measurement.

### Safety

Active probing actuates real devices, so the rules are enforced in code:

- **Only `switch` and `light` entities may be probed.** An allowlist, not a
  denylist, so a domain nobody anticipated fails closed. Locks, alarm panels,
  covers, valves, climate, water heaters and sirens are refused outright.
- **The prior state is always restored**, from a `finally` block rather than the
  happy path — an exception mid-probe is exactly when that would otherwise be
  skipped.
- **It never runs on its own.** There is no automatic probing; it happens only
  when someone calls the service.

## Tools

Two offline tools run against a live Home Assistant from a workstation that does
not have Home Assistant installed. `analysis.py`, `fingerprint.py` and
`attribution.py` import nothing from HA precisely so this works.

```bash
export HA_URL=https://homeassistant.local:8123 HA_TOKEN=...

python tools/identify.py --all --days 7          # cluster runs, describe shapes
python tools/attribute.py --days 3               # place metered devices on circuits
python tools/make_brand.py                       # regenerate brand images
```

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

## Tests

```bash
python -m pytest tests/          # 53 pure tests, no Home Assistant needed
```

The analysis, fingerprint, attribution and verify modules import nothing from
Home Assistant, so the bulk of the suite runs on a bare checkout.

`tests/ha/` covers the Home Assistant layer and is **skipped unless
`pytest-homeassistant-custom-component` is installed**. It runs in CI against
Home Assistant on Linux, which is where it belongs — see
[PUBLISHING.md](PUBLISHING.md).

## Licence

MIT.
