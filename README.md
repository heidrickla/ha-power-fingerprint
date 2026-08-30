# Power Fingerprint

Identify what is running on a circuit from the shape of its load.

Home Assistant integration for panels with per-circuit power monitoring. It
takes `device_class: power` sensors and does not care where they come from —
but see **[What it has actually been tested against](#what-it-has-actually-been-tested-against)**
before assuming that means yours.

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

## What people use it for

- **"Tell me when the laundry is done"** — the appliance sensor goes from the
  dryer's name back to `idle`, with no vibration sensor, no contact sensor and
  nothing stuck to the machine.
- **"Which of these 27 breakers is the garage fridge on?"** — `learn` proposes
  the recurring shapes, `verify_circuit` switches a device and watches which
  circuit moves, and the two agree or the answer is refused.
- **"What is my house drawing while nobody is home?"** — standby power with a
  per-circuit ranking, and what it costs a year.
- **"Did a CT come off?"** — the coverage fault fires when the circuits stop
  adding up to the mains, in either direction. A reversed clamp reads as
  negative unmonitored load.
- **"That switch says it is on but nothing happened"** — the contradiction
  sensor compares what the state machine claims against what the clamp
  measures, and the clamp wins.

### Automation examples

Notify when a named appliance finishes:

```yaml
automation:
  - alias: Laundry is done
    triggers:
      - trigger: state
        entity_id: sensor.circuit_21_appliance
        from: Dryer
        to: idle
    actions:
      - action: notify.mobile_app_phone
        data:
          message: The dryer has finished.
```

Raise the alarm when a clamp comes off, but only once it has persisted:

```yaml
automation:
  - alias: CT coverage fault persists
    triggers:
      - trigger: state
        entity_id: binary_sensor.power_fingerprint_ct_coverage_fault
        to: "on"
        for: "00:15:00"
    actions:
      - action: persistent_notification.create
        data:
          title: Panel coverage
          message: >-
            {{ state_attr('sensor.power_fingerprint_unmonitored_load',
            'mains_w') }} W at the mains against
            {{ state_attr('sensor.power_fingerprint_unmonitored_load',
            'circuits_w') }} W across the circuits.
```

Learn on a schedule, so the library tracks appliances as they age:

```yaml
automation:
  - alias: Re-learn fingerprints monthly
    triggers:
      - trigger: time_pattern
        hours: "3"
    conditions:
      - condition: template
        value_template: "{{ now().day == 1 }}"
    actions:
      - action: power_fingerprint.learn
        data:
          days: 14
        response_variable: found
```

⚠ `learn` **replaces** a circuit's candidates. Names you have already given
survive — they are carried onto the matching new shapes — but a shape that no
longer occurs disappears with its name.

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

### What you need before installing

| Requirement | Detail |
|---|---|
| **A whole-panel power sensor** | Any `device_class: power` sensor reading the mains. ⚠ Check its unit — see below. |
| **At least one circuit power sensor** | One per breaker. Leave out phase and total sensors, or they get counted twice. |
| **The recorder** | Used to seed the 24-hour window at startup and to read history for learning. Without it the integration still runs, but standby figures take a day to converge and `learn` has nothing to read. |

Setup validates all of this before the entry is created: a sensor that does not
exist, is not reporting a number, or reports in something other than a power
unit is refused **by name** at the point you can still pick a different one.
If a sensor's unit changes to a non-power unit later, a repair issue appears
naming it rather than the circuit quietly vanishing from the totals.

### Removing it

Settings → Devices & Services → Power Fingerprint → ⋮ → **Delete**. That
removes the entry, its device and every entity it created. The learned
fingerprint library is stored under `.storage/power_fingerprint.<entry_id>` and
is deleted with the entry.

⭐ Nothing outside the integration is left behind. The one thing that could be
— an automation switched off for an active probe — is recorded to storage
*before* it is switched off and restored at the next startup, so a probe killed
mid-run cannot leave the house silently unresponsive.

### Reconfiguring

Settings → Devices & Services → Power Fingerprint → ⋮ → **Reconfigure** to
change the mains or circuit list after re-clamping a panel, or **Configure**
for the same fields plus price, tolerance and contradiction pairs. Both run the
same validation as setup.

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
  reports every few seconds. Treated as *no reading* rather than a zero-watt
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

## How it updates

The coordinator polls Home Assistant's state machine every **30 seconds** and
keeps its own 24-hour rolling window in memory. It does not query the recorder
on every refresh — the recorder purges (30 days by default), and repeated
history queries against a multi-gigabyte database are slow enough to matter at
that rate. The window *is* seeded from the recorder once at startup, so standby
figures are meaningful immediately after a restart instead of taking a day.

30 seconds is the resolution the standby figure needs (a 5th percentile over 24
hours does not move faster than that), and the live appliance match reads
whatever the meter last published — polling faster would resample the same
value. The meter's own cadence is measured separately and reported in
diagnostics.

Learning and probing are **actions you run**, not background work. Nothing
switches anything in your house unless you call `verify_circuit`.

## Troubleshooting

| Symptom | What it means |
|---|---|
| **Every circuit reports zero runs** | Almost always a unit mismatch. Check the `source` block in diagnostics: if `units` shows `kW`, older versions read those numbers raw and every threshold was 1000× too high. Current versions convert and log a warning naming the sensor. |
| **Coverage fault that never clears** | The mains sensor and the circuits are not measuring the same thing. A panel with two physical units has a total sensor per unit — picking one of them as mains leaves the other's whole load unaccounted for. Look for a `whole_panel`-style sensor covering both. |
| **A circuit is listed as silent** | Its maximum over 24 hours is under 5 W. That is information, not an alarm: an unused circuit legitimately reads zero forever. A circuit you *know* is loaded reading zero means a CT is off or on the wrong conductor. |
| **`learn` returns `no history in window`** | The recorder has nothing for that entity in the requested window. Empty history is UNREAD, not "nothing ran" — the report says which. |
| **A probe says `suspect`** | An automation fired during the probe and may have moved something. Re-run with `pause_automations: true`, or at a quieter time. |
| **A probe says the settle window is too short** | Your meter reports more slowly than the probe waits, so both reads came from the same stale value. The message names the settle time to use. |
| **Entities are unavailable** | Every configured source is gone at once. A *single* circuit dropping out deliberately does not blank the others — that is the condition the coverage sensor exists to report. |

Turn on debug logging with:

```yaml
logger:
  logs:
    custom_components.power_fingerprint: debug
```

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

An appliance shorter than a few reporting intervals is two or three data
points, and cannot be fingerprinted. At the ~6 s the development install
publishes, that puts kettles, microwaves and disposals at or below the
resolution limit; a 1-second meter would resolve all three. That is a property of your meter, not
something more code fixes — and it is why the cadence is measured and reported
rather than assumed.

## What it has actually been tested against

**One meter: an ESPHome-flashed Emporia Vue, 2 units, 27 circuits, publishing
every ~6 s.**
Everything in the field results below comes from that install. Nothing here is
written against a vendor API — the integration only ever sees
`device_class: power` sensors — but "no vendor code" is not the same claim as
"tested elsewhere", and it would be dishonest to make the second one.

Two properties of a meter are what actually matter, and both used to be
assumed. Both are now measured, and both appear in the diagnostics download:

| Property | Why it matters | What happens now |
|---|---|---|
| **Unit** | Every threshold here is in watts. `device_class: power` does not constrain the unit — Vue, Shelly EM and IotaWatt report W; SolarEdge, Powerwall and most Modbus meters report kW. | Converted once at ingestion (W, kW, MW, GW, mW, BTU/h). A sensor whose unit is not power is dropped and **named in the log**, once. |
| **Reporting cadence** | Sets the step-matching window in attribution: too wide and chance coincidences match, too narrow and real ones fall between cells. | Measured from your own history as the median inter-sample gap. `tools/attribute.py --step` overrides it; the default no longer names a cadence. |

⛔ **A kilowatt meter used to fail silently, and that is the failure mode to
expect from any untested assumption here.** Feeding kW into a 15 W margin puts
the threshold above every reading, so every circuit reported **zero runs
forever** — no error, no warning, just an empty result that looks like a quiet
house. `test_kilowatt_circuit_is_detectable_only_after_conversion` pins it.

### Both assumptions were wrong on the install they came from

Measured 2026-08-30, on the same Vue everything above was developed against.
Neither of these was visible from the outside, which is the point.

- ⛔ **The mains sensor reports kilowatts while all 27 circuits report watts.**
  Same brand, same integration, three whole-panel sensors, two different units:
  `emporiavue_total_power` is kW, `emporiavuesecondary_total_power` and
  `whole_panel_total_power` are W. Configure the kW one as mains and coverage
  compared **1.38 against 2,316** — a permanent −2,314 W fault on a panel that
  is actually fine. Converted, the same reading is 1,380 W, and the remaining
  −936 W correctly points at the *second* panel rather than at a fault.
- ⛔ **The cadence is ~6.1 s, not the 12 s written throughout this repo.**
  Measured two independent ways on two circuits: 14,141 rows over 86,395 s =
  6.11 s/row, median inter-sample gap 6.18 s. Because Home Assistant does not
  restamp an unchanged value, both are *upper* bounds — the meter is at least
  that fast. The attribution grid had been running at roughly double the real
  interval, which is the direction that lets chance coincidences match.

If you run this on something other than a Vue, the diagnostics download names
your unit and your measured cadence in the `source` block. That is the useful
thing to paste into an issue.

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

Checked against Home Assistant's own area assignments, one busy circuit was
still claiming devices from three unrelated areas — and the cause turned out to
be the grid, not the scoring. **The grid had been hardcoded at 12 s against a
meter that actually publishes every ~6 s**, so every step-match window was
about double the width it should have been, and on a busy circuit chance
coincidences fit inside one. Measuring the cadence instead:

| | 12 s grid (assumed) | 6 s grid (measured) |
|---|---|---|
| Devices placed | 13 of 21 | **8 of 21** |
| Circuits claiming 3+ areas | 1 | **0** |

Fewer answers, and that is the improvement: Master Bathroom, Guest Bathroom and
Front Porch stopped being assigned to one circuit and are now honestly
**ambiguous**. The four office lights that always switch together still land on
circuit 16 together, which is consistent rather than suspicious, and the shared
bathroom light lands on the circuit whose own label reads *"twins bedrooms,
hallway and bath"* — a name the scorer never sees.

⭐ **An over-wide correlation window buys placements by inventing them.** Only
assignments with a high step match *and* a clear margin should be trusted; the
tool prints both, and prints which grid it used and whether that grid was
measured or given.

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

## Quality scale

Built to Home Assistant's Integration Quality Scale, tracked rule by rule in
[`quality_scale.yaml`](custom_components/power_fingerprint/quality_scale.yaml)
with a written reason on every exemption. What that actually bought:

| Rule | What changed |
|---|---|
| `runtime-data` | Runtime state lives on `entry.runtime_data` behind a typed dataclass, not in an untyped `hass.data` dict. |
| `action-setup` | Actions register at component setup, so an automation calling one still validates while the entry is unloaded — and gets a translated "not loaded" error instead of a `KeyError`. |
| `test-before-configure` | Setup refuses a sensor that does not exist, is not reporting a number, or is not in a power unit — **by name**, while you can still pick another. |
| `test-before-setup` | `ConfigEntryNotReady` while the meter's own integration is still loading, instead of a device full of blank entities. |
| `entity-unavailable` / `log-when-unavailable` | Sources going away is logged once and once on return, not every 30 seconds. A single circuit dropping out deliberately does *not* blank everything — that is what the coverage sensor is for. |
| `repair-issues` | A sensor that starts reporting a non-power unit raises a repair issue naming it, rather than quietly leaving a circuit out of every total. |
| `reconfiguration-flow` | Re-clamped the panel? Reconfigure the entry instead of deleting and re-adding it. |
| `entity-translations` / `icon-translations` / `exception-translations` | Names, icons and error messages come from `strings.json` and `icons.json` and can be translated. |
| `strict-typing` | `mypy --strict` clean across the integration. |
| `parallel-updates` | `PARALLEL_UPDATES = 0`: nothing here talks to a device, so there is nothing to be gentle with. |

Two rules are honestly `todo` — the Home Assistant layer's test coverage,
written but never executed on Windows. `tools/validate_local.py` refuses to let
`manifest.json` claim a tier while anything is `todo`.

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
