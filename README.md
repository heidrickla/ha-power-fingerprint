# Power Fingerprint

Identify what is running on a circuit from the shape of its load.

A Home Assistant integration for panels with per-circuit power monitoring. It
takes `device_class: power` sensors and does not care where they come from, but
see [What it has been tested against](#what-it-has-been-tested-against).

## Why this is not NILM

Non-intrusive load monitoring tries to pull thirty overlapping appliances out of
one meter reading. That is hard, which is why the field uses neural networks.

With a CT on each circuit the separation is already done in hardware. What is
left is telling apart the two or three appliances sharing one circuit, which
needs a handful of features and no machine learning.

On the development install the circuits sum to within 28 W of a 6,556 W mains
reading. There is nothing meaningful left to disaggregate.

## Entities

Available immediately, with no labelled fingerprints:

| Entity | Meaning |
|---|---|
| `sensor.power_fingerprint_unmonitored_load` | Mains minus the sum of the circuits. |
| `binary_sensor.power_fingerprint_ct_coverage_fault` | That figure moved outside tolerance. |
| `sensor.power_fingerprint_standby_power` | Total permanent draw, with a per-circuit ranking in the attributes. |
| `sensor.power_fingerprint_standby_annual_cost` | What that costs per year. |
| `binary_sensor.power_fingerprint_state_contradiction` | A switch reports `on` while its circuit draws nothing. |
| `sensor.power_fingerprint_unnamed_candidates` | Learned shapes waiting for a name, described in the attributes. |
| `binary_sensor.power_fingerprint_silent_appliance` | A named appliance has stopped running when its own history says it should have. |

Every entity belongs to the `Power Fingerprint` service device, so its id
carries the device name as a prefix.

After naming a fingerprint, an appliance sensor per circuit reads `idle`,
`starting`, the appliance name, or `unknown`. Its id is built from the circuit
sensor's own name: a circuit called `Circuit 21 Power` gets
`sensor.power_fingerprint_circuit_21_power_appliance`. It is `unavailable`
while that circuit's sensor cannot be read.

`unknown` means the circuit is drawing power in a shape no named fingerprint
accounts for. That is a signal, not a failure.

### Circuit shown on the device

Once a device is mapped, a Circuit entity appears on that device's own page,
next to its switch and its power reading. The attributes record how it was
established:

| `established_by` | Meaning |
|---|---|
| `probe` | The integration switched the device and watched a circuit move. |
| `correlation` | It observed the two moving together. |

A probe result is never overwritten by a passive one, and a run that resolves
nothing never erases a previous answer.

### Filtering by breaker

`power_fingerprint.apply_circuit_labels` adds a label such as `Circuit 16 Study`
to each mapped device, so devices can be filtered by breaker anywhere in Home
Assistant.

`via_device` would be the natural mechanism, but an integration may only set it
on devices it owns and these belong to ZHA or Z-Wave. A device per circuit was
rejected because entries named "Circuit 30" beside real devices read as
duplicates.

Labels are the user's namespace and no integration API for them is documented,
so the service defaults to a dry run, only adds to a device's existing labels,
and `remove: true` takes them off.

### Attaching an entity to another integration's device

Returning the target device's `identifiers` in `DeviceInfo` no longer merges. On
Home Assistant 2026.8 it creates a second, nameless device beside the real one;
the registry now carries `composite_device_id` and treats shared identifiers as
a composite relationship.

Register with no device and point the entity's registry row at the target in
`async_added_to_hass`. `setup_entry` prunes stray devices left by earlier
versions.

The id is built from the device name plus the entity name at registration,
before the device is attached, so a new Circuit entity supplies the device name
itself and arrives as `sensor.porch_lamp_circuit`. A device with no name of its
own gives `sensor.circuit`.

Entities that already exist keep the id they have. The registry asks for a
suggested id only when it creates a row, so an install from before this change
keeps `sensor.circuit`, `sensor.circuit_2` and so on. Deleting one of those
entities and letting the next poll recreate it is what renames it; the unique
id is the same either way, so nothing else moves.

## What people use it for

- Notify when the laundry finishes, with no sensor attached to the machine.
- Find which of 27 breakers a device is on.
- See standby draw per circuit, and what it costs.
- Detect a CT coming loose, in either direction.
- Catch a switch reporting `on` while its circuit draws nothing.

### Automation examples

Notify when a named appliance finishes:

```yaml
automation:
  - alias: Laundry is done
    triggers:
      - trigger: state
        entity_id: sensor.power_fingerprint_circuit_21_power_appliance
        from: Dryer
        to: idle
    actions:
      - action: notify.mobile_app_phone
        data:
          message: The dryer has finished.
```

Alert on a persistent coverage fault:

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

Re-learn monthly:

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

`learn` replaces a circuit's candidates. Names already given are carried onto the
matching new shapes, but a shape that no longer occurs disappears with its name.

## Installation

Home Assistant 2026.3 or later. The config flow uses APIs added in 2024.12, and
the brand images ship inside the repository, which HACS reads from 2026.3.

HACS (custom repository): add `https://github.com/heidrickla/ha-power-fingerprint`
as a custom repository of type Integration, install, restart Home Assistant.

Manual: copy `custom_components/power_fingerprint/` into your Home Assistant
`config/custom_components/` directory and restart.

Then Settings -> Devices & Services -> Add Integration -> Power Fingerprint.

There is no discovery, and there is nothing to discover: this integration reads
sensors another integration has already created, so it is added by hand and
asks which of those sensors to use. Only one entry is allowed, because a second
would count every circuit twice.

Entities group under a single service device. `Download diagnostics` produces a
report with entity ids pseudonymised, since entity names describe rooms and
appliances and diagnostics files end up in public issue trackers.

Renaming a source sensor on the meter's integration is followed: the circuit
list, the switch/circuit pairs, the learned library and this integration's own
entities all move with it, without a reload.

Publication status is in [PUBLISHING.md](PUBLISHING.md).

### Requirements

| Requirement | Detail |
|---|---|
| A whole-panel power sensor | Any `device_class: power` sensor reading the mains. |
| At least one circuit power sensor | One per breaker. Leave out phase and total sensors or they are counted twice. |
| The recorder | Seeds the 24-hour window at startup and supplies history for `learn` and `map_devices`. Without it the entry still loads, standby figures take a day to mean anything, and those two actions refuse with an error saying the recorder is not running. |

Setup validates this before the entry is created: a sensor that does not exist,
is not reporting a number, or is not in a power unit is refused by name, and
the message says whether it was the mains or a circuit that failed. If a
sensor's unit later changes to a non-power unit, a repair issue names it.

### Removing it

Settings -> Devices & Services -> Power Fingerprint -> Delete. That removes the
entry, its device and every entity, and deletes the fingerprint library in
`.storage/power_fingerprint.<entry_id>`. Nothing is left behind; re-adding the
integration starts from an empty library.

An automation paused for a probe is recorded to storage before being switched
off, and restored at the next startup.

### Reconfiguring

Reconfigure (the entry's own menu) and Configure (the options button) show the
same six fields as setup and run the same validation. Reconfigure writes to the
entry data and clears any saved options; Configure writes options, which take
precedence at runtime. Either one reloads the entry.

## Confidence

One setting rather than a dozen thresholds:

| Setting | Effect |
|---|---|
| Cautious | Answers only where the evidence clearly beats coincidence. Roughly half as many placements. |
| Balanced | Default. What the measurements below were taken with. |
| Eager | More placements, some of them wrong. Useful while exploring a panel, not for driving automations. |

It moves step match, margin, correlation, probes required to agree, cluster
tightness and absence patience together. An unrecognised value falls back to
balanced.

The thresholds are not exposed individually because they were derived by
measuring against a house with 27 clamps to check answers against, which most
installs do not have.

## Controls

Every match rate is reported alongside the score the same test achieves against
data shifted in time. `analysis.control()` scores the real alignment, then scores
it at several offsets where the same events did not happen.

```
measured 0.91   chance 0.40   lift 0.51   verdict: clear
measured 0.95   chance 0.93   lift 0.02   verdict: chance
```

The lowest of several offsets is used. A three-hour shift still overlaps the
house's daily rhythm; nineteen hours does not.

## Breaker walk

Cutting a breaker is the only causal test here. Correlation and probes show that
two things move together; killing the circuit proves a device is fed by it.

The integration cannot flip breakers, so this is a guided manual procedure. It
identifies which breaker was flipped from the circuit clamps, and refuses when:

- no clamp dropped, meaning the breaker is not one it watches;
- two circuits dropped together, meaning a double-pole breaker or a main;
- the candidate circuit was already idle.

Results are split by evidence:

| | Meaning |
|---|---|
| `confirmed` | The device's own meter collapsed. |
| `suspected` | The device vanished, which may mean it lost power or that its Zigbee or Z-Wave parent did. |
| `unaffected` | Still drawing what it was. |

Anything known to route for other devices stays `suspected`.

`describe()` reports what the meter recorded for a circuit: current draw,
permanent floor, hours observed, whether it has ever been idle, and which devices
are attributed to it. It forms no verdict on whether a circuit is safe to cut.
The permanent floor is the useful figure before anything is identified, since a
circuit that never falls below several hundred watts has something on it that
never stops.

## Naming

`power_fingerprint.autolabel` names shapes whose circuit already says what it is,
reading the circuit sensor's title and, in preference, the name on the energy
dashboard.

It applies a name only where the circuit has exactly one learned shape and its
title contains something other than a number. A circuit with several shapes has
its suggestion returned rather than applied. It never overwrites a name a person
gave.

On the development install this named 12 of 21 circuits and returned 8 with
suggestions.

## Learning fingerprints

Two services. Clustering finds the recurring shapes and cannot name them; naming
needs someone who knows what is plugged in.

```yaml
# Propose candidates from history. Returns a description of each shape.
action: power_fingerprint.learn
data:
  days: 7
response_variable: found

# Name one.
action: power_fingerprint.label
data:
  circuit: sensor.circuit_21_power
  current_label: unnamed_0
  new_label: Dryer
```

Matching happens on a partial run rather than waiting for it to finish, which
works because duration and energy are weighted to zero for identity. Three
outcomes: the appliance name, `idle`, or `unknown`.

## Passive and active together

`map_devices` correlates each self-metering device against every circuit.
`verify_circuit` switches a device and watches which circuit moves. Passive
covers everything at once but is inference; active is measurement but touches one
device at a time.

### Limits of active probing

- Only `switch` and `light` entities are probed. Config and diagnostic entities
  are refused: a Z-Wave dimmer publishes its settings in the same domain as its
  load, and flipping `invert_switch` reconfigures the device while moving no
  current.
- A device currently drawing a sustained load is refused, because switching it
  off would interrupt whatever is running. An unmetered device that is on is
  refused rather than guessed at.
- Battery devices are refused: they draw no mains current.
- A single probe never returns `measured`. The defence against coincidence is
  that independent probes agree, which is vacuous with one.
- The device's own power sensor is discovered automatically. Without it, ranking
  falls back to which circuit moved most, and on a house with air conditioning
  the answer is always the air conditioning.

### Pausing interfering automations

`pause_automations: true` switches off automations that reference the probed
device or any circuit, using Home Assistant's `referenced_entities`.

An automation is never paused if it touches any entity in these domains, or any
sensor with one of these device classes. This is the one list; `verify.py`
carries it and `services.yaml` points here.

| Never paused | |
|---|---|
| Domains | `lock`, `alarm_control_panel`, `cover`, `valve`, `water_heater`, `climate`, `siren`, `humidifier`, `vacuum`, `notify`, `persistent_notification`, `device_tracker`, `person` |
| Device classes | `moisture`, `smoke`, `gas`, `carbon_monoxide`, `safety`, `problem`, `tamper` |

The list is a denylist and deliberately broad: the cost of leaving an
automation running is a noisier measurement, the cost of pausing the wrong one
is a door that stays locked or a leak alert that never fires. Refusals are
returned in the action's response, not swallowed.

The pause list is written to storage before anything is switched off, and
restored at the next startup if a probe dies mid-run.

### Field results

18 devices probed across seven rooms. Placements that survived three agreeing
probes:

| Circuit | Devices |
|---|---|
| `circuit_18` | Kitchen counter lights, kitchen ceiling lights, dining room light |
| `circuit_30` | Guest bathroom vent, shared hallway outlet, pantry light, archway lights |
| `circuit_22` | Living room ceiling lights |
| `circuit_12` | Shared bathroom vent |
| `circuit_16` | Kitchen pendant lights |

Six of seven disputed placements collapsed to `unknown` when three probes had to
agree, having been reported as `measured` at `probes: 1`.

## Actions

Six actions under `power_fingerprint.`. Every field is optional unless marked
required. Where a default reads "confidence setting", leaving the field empty
uses the value the [Confidence](#confidence) setting supplies, and a value
overrides it for that call only.

### `learn`

Read history for each circuit, group the appliance runs by shape, and store
them as candidates. Returns a description of every shape found. Names already
given are carried onto the matching new shapes.

| Field | Default | Meaning |
|---|---|---|
| `circuits` | all configured circuits | Limit to these power sensors. |
| `days` | 7 | Days of history to read, 1 to 30. |
| `threshold` | confidence setting (0.7 / 0.9 / 1.2) | How different two runs must be to count as different appliances. Lower splits more. 0.1 to 5.0. |

### `label`

Turn a candidate into an identification. Only named fingerprints drive
entities.

| Field | Default | Meaning |
|---|---|---|
| `circuit` | required | The power sensor the fingerprint was learned on. |
| `current_label` | required | The existing label, for example `unnamed_0`. |
| `new_label` | required | What this appliance actually is. |

### `verify_circuit`

Switch a device and watch which circuit moves. It actuates the device, several
times, and always restores its prior state. Only `switch` and `light` entities
may be probed. See [Limits of active probing](#limits-of-active-probing).

| Field | Default | Meaning |
|---|---|---|
| `device` | required | The switch or light to probe. |
| `power_sensor` | the device's own power sensor, found from its device | Lets the ranking check magnitude, not only direction. |
| `probes` | confidence setting (3 / 2 / 1) | How many times to switch it. All probes must agree. 1 to 5. |
| `settle` | 30 | Seconds to wait for the meter after each switch, 10 to 120. Must be at least twice the meter's reporting interval; the action warns when it is not. |
| `pause_automations` | `false` | Switch off automations that reference the device or a circuit for the duration. See [Pausing interfering automations](#pausing-interfering-automations). |
| `force` | `false` | Probe even though the device is carrying a load. |

### `map_devices`

Correlate every self-metering power sensor against every circuit and record
where each device sits. Reports what it could not place as well as what it
could.

| Field | Default | Meaning |
|---|---|---|
| `days` | 3 | Days of history to compare, 1 to 14. |
| `step` | measured from the meter's own history | Grid seconds to resample onto, 1 to 300. |
| `min_correlation` | confidence setting (0.65 / 0.50 / 0.35) | Below this a device is left unplaced. Lower places more devices and more of them wrongly. 0 to 1. |

### `autolabel`

Name every learned shape whose circuit already says what it is. No fields. See
[Naming](#naming).

### `apply_circuit_labels`

Add a label naming the circuit to each mapped device, so devices can be
filtered by breaker. See [Filtering by breaker](#filtering-by-breaker).

| Field | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | Show what would happen and change nothing. |
| `remove` | `false` | Take the circuit labels back off every device instead. |
| `prefix` | `Circuit` | What each label is called before the circuit's name. |

## Tools

Development scripts in `tools/`, run against the REST API from a workstation.
They load the pure modules by path and need no Home Assistant install.

| Script | Purpose |
|---|---|
| `identify.py` | Segment and cluster history for one circuit. |
| `attribute.py` | Map self-metering devices to circuits. |
| `virtual_circuits.py` | Infer appliances from a whole-house meter, with `--validate` against real clamps. |
| `one_appliance.py` | Test whether the mains contains one known circuit's runs, with its chance baseline. |
| `validate_local.py` | The offline half of the HACS and hassfest checks. |
| `make_brand.py` | Generate and size-check the brand images. |

## Configuration

The same six fields appear at setup, on Reconfigure and on Configure.

| Field | Required | Default | Meaning |
|---|---|---|---|
| Whole-panel power sensor | yes | | The sensor reading the mains. Its total should be close to the sum of the circuits. |
| Circuit power sensors | yes, at least one | | One per breaker. Leave out phase and total sensors, or they are counted twice. The mains may not also be a circuit. |
| Electricity price per kWh | no | the energy dashboard's price, else 0.13 | Turns standby watts into an annual figure. A price set here beats the dashboard's. |
| Coverage tolerance | no | 5 % | How far the circuits may drift from the mains before a fault is raised. |
| How sure do you want to be? | no | Balanced | See [Confidence](#confidence). |
| Switch/circuit pairs | no | none | One `switch.entity: sensor.circuit_power` per line, checked for a switch that says on while its circuit draws nothing. |

## How it updates

The coordinator polls the state machine every 30 seconds and keeps a 24-hour
rolling window in memory, seeded once from the recorder at startup. It does not
query the recorder on every refresh.

30 seconds is the resolution the standby figure needs. The live appliance match
reads whatever the meter last published; the meter's own cadence is measured and
reported in diagnostics.

Learning and probing are actions you run. Nothing switches anything unless
`verify_circuit` is called.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every circuit reports zero runs | Usually a unit mismatch. Check the `source` block in diagnostics for `kW`. |
| Coverage fault that never clears | The mains sensor and the circuits are not measuring the same thing. A panel with two units has a total per unit. |
| A circuit listed as silent | Its maximum over 24 hours is under 5 W. Normal for an unused circuit. |
| `learn` returns `no history in window` | The recorder has nothing for that entity in the window. |
| `learn` or `map_devices` fails with "The recorder is not running" | The recorder integration is disabled. Enable it; the entry itself loads without it. |
| A probe returns `suspect` | An automation fired during it. Re-run with `pause_automations: true`. |
| A probe warns about settle time | The meter reports more slowly than the probe waits. |
| Entities unavailable | Every configured source is gone. A single circuit dropping out does not blank the others. |
| One appliance sensor unavailable | Its circuit's sensor is unavailable. The aggregates stay up and the coverage sensor reports the gap. |
| Setup keeps retrying | A configured power sensor has not appeared yet. The integration card names the first one it is waiting for. |

```yaml
logger:
  logs:
    custom_components.power_fingerprint: debug
```

## Design notes

Four things in `analysis.py` exist because the obvious approach failed, each with
a regression test.

Otsu's method, not a percentile, for the on/off threshold. A percentile assumes a
duty cycle. On a furnace at 68% duty the 40th percentile lands inside a run,
putting the threshold above the load, and the circuit reports zero runs across
98,849 samples with no error anywhere.

Events keep their sub-threshold samples. Storing only samples above the threshold
pins `floor_w` to the threshold by construction, destroying the feature that
separates a washer from a dryer.

Duration and energy are weighted to zero for clustering. With duration counted,
one furnace split into four clusters identical in power and differing only in how
long each run lasted.

The floor while running is the discriminator. A washer and dryer sharing a
circuit have near-identical peaks. The washer drops to near zero between fill,
soak and spin; the dryer holds a 350-400 W floor.

### Resolution limit

An appliance shorter than a few reporting intervals is two or three data points
and cannot be fingerprinted. At the ~6 s the development install publishes, that
covers kettles, microwaves and disposals.

## What it has been tested against

One meter: an ESPHome-flashed Emporia Vue, 2 units, 27 circuits, publishing every
~6 s. Nothing here is written against a vendor API, but that is not the same as
being tested elsewhere.

Two properties of a meter matter, and both are measured rather than assumed:

| Property | Why | Handling |
|---|---|---|
| Unit | Every threshold is in watts. `device_class: power` does not constrain the unit. | Converted at ingestion (W, kW, MW, GW, mW, BTU/h). A non-power unit is dropped and named in the log once. |
| Reporting cadence | Sets the step-matching window. | Measured from your own history as the median inter-sample gap. |

Both assumptions were wrong on the install they came from:

- The mains sensor reports kilowatts while all 27 circuits report watts, on the
  same brand and integration. Configured as mains, coverage compared 1.38 against
  2,316.
- The cadence is ~6.1 s, not the 12 s previously assumed. Measured two ways on
  two circuits: 14,141 rows over 86,395 s, median gap 6.18 s. Both are upper
  bounds, since Home Assistant does not restamp an unchanged value.

## Self-metering devices

Most homes with per-circuit monitoring also have devices that meter themselves.
The development install has 40 alongside 27 circuits. They give labelled
fingerprints without asking anyone, allow a known device to be subtracted from a
shared circuit, and support automatic device-to-circuit mapping.

Attribution should be treated as a suggestion. Correlation alone placed 2 of 20
devices, because a 10 W lamp contributes almost nothing to the variance of a
circuit swinging hundreds of watts. Step matching lifted that to 13. Requiring
the winner to beat the runner-up then correctly refused four office lights that
always switch together.

The grid is measured rather than fixed. At a 12 s grid against a meter publishing
every 6 s, one circuit claimed devices from three unrelated areas; at the measured
6 s that fell to zero such circuits, and placements dropped from 13 to 8.

### No candidate versus nothing to look at

A device that never switched during the window cannot be placed. Correlation and
step matching both work on transitions.

This is a property of the window, not the device. Network gear draws very
differently when it starts; it is simply never switched. Six of seven rack PDU
outlets report `no transition in window` across three days.

The test is steps, not spread. A light on 3% of the time has an identical 95th
and 5th percentile; slow thermal drift has a wide one.

## Absence detection

The only check here that alerts on too little. A freezer that stopped cycling
crosses no threshold.

`learn` records when each shape ran, so every named fingerprint carries its own
cadence: median and 90th-percentile gap between runs. Below five runs it records
nothing, because four gaps cannot distinguish "runs weekly" from "ran four times
and stopped".

`binary_sensor.silent_appliance` turns on when a named appliance has been silent
for more than twice its own p90 gap.

### Blind time is not silence

Unobserved seconds are tracked per circuit and subtracted before judging, from
two sources:

| Source | Why |
|---|---|
| A circuit `unavailable` at a poll | One poll interval unobserved. |
| Home Assistant restarted | The whole gap since the last recorded poll. |

When more than half the window was unobserved the answer is `unknown`. The
`unjudgeable` attribute names which appliances are being declined rather than
counted healthy.

Last-seen times are persisted, so a restart does not reset every appliance to
"never seen".

## Virtual circuits

For houses with no per-circuit clamps. Infers large appliances from a whole-house
meter: a dryer, an oven, an air conditioner, a well pump, an EV charger. It does
not infer lamps.

A load is identified by its on-step and matching off-step. `segment()` is not
usable, because it finds runs by watching a trace fall back to idle and a
whole-house trace never does.

Level shifts, not adjacent samples. A compressor ramps over half a minute, so a
2 kW load arrives as several smaller deltas and no single one clears the bar.
Comparing the median of the samples before a point against the median after sees
the ramp as one step. On the development install that took the result from 0
paired runs to 168, at 1572-2628 W over 19-24 minutes.

### The floor is the meter's own noise floor

Swept against the 27 real clamps, the fraction of inferred runs that
magnitude-match a real circuit:

| Floor | Shapes | Match rate |
|---|---|---|
| 69 W, the measured noise floor | 4 | 69% |
| 138 W | 5 | 52% |
| 345 W | 4 | 29% |
| 967 W | 3 | 19% |
| 1934 W | 2 | 0% |

Raising the floor filters out single appliances rather than noise: what survives
at 2 kW is the moments when several loads moved together, and a combination
matches no individual circuit.

Pair rate, the fraction of level shifts that find a partner, is not used to tune
this. It reaches 97% at the floor that matches 0%.

### A worked example

Circuit 25 carries a garage refrigerator and nothing else, so the mains has to
find something only the CT can see.

```
circuit 25, 2 days     85 runs   median step 103 W   median 1 min
the mains at 69 W      526 inferred runs

55 of the 85 runs are above the mains floor
50 of those were found in the mains          91%
30 were below the floor and unfindable
```

With the same runs shifted in time:

| Shift | Still matched |
|---|---|
| +3 h | 78% |
| +7 h | 64% |
| +13 h | 45% |
| +19 h | 40% |

So 91% against a 40% chance floor. 526 inferred runs across two days is one every
five and a half minutes, and a one-minute compressor cycle overlaps one
constantly.

Detecting an event in the aggregate works. Attributing it to a specific appliance
on time and magnitude alone does not.

### What this house is not representative of

- Measured in the hottest week of the year, with two air conditioners, fans and
  compressors cycling. Its 95th-percentile movement is 69 W, which is high.
- Both air conditioners have soft starts, which most houses do not, and which is
  why a compressor here ramps over half a minute.
- Several large loads run continuously rather than alone. The good case is a
  dryer, an oven or a charger, which run by themselves and stop.

### Subtracting self-metering devices

A device that meters itself needs no inference; it is already an exact virtual
circuit. `--subtract` removes its trace from the aggregate before inferring.

On this install it changed the match rate by nothing, because the metered devices
are lights against a 4-6 kW aggregate. On a house with a smart-plugged dryer or
EV charger it should matter.

## Roadmap

- Signature drift as health: compare an appliance against its own history. A
  fridge duty cycle creeping 40% to 70%, a pump's start current climbing.
- Runtime-based maintenance: blower hours since filter change, dryer cycles since
  duct clean.
- Away-mode safety: alarm armed away plus a 2 kW load on the oven circuit.
- Tariff-aware shifting: fingerprints plus a tariff become "run the dryer after
  9pm".

### Deliberate non-goals

No deep learning. The data volumes do not justify it and an unexplainable
classifier is worthless once it drives an alert. The most useful feature so far,
the floor while running, came from domain knowledge.

No cloud. Everything runs locally against sensors already in Home Assistant.

## Quality scale

Built to Home Assistant's Integration Quality Scale, tracked rule by rule in
[`quality_scale.yaml`](custom_components/power_fingerprint/quality_scale.yaml)
with a reason on every exemption.

Every rule is `done` or `exempt` with a written reason, and the file says
which. `test-coverage` closed on 2026-09-04: coverage is 99% of
`custom_components/power_fingerprint` and the build fails below 95%.

The GitHub `Tests` workflow runs both test suites under coverage with that
gate, mypy with the full strict block and the offline validator on every push,
against Home Assistant 2026.8.3 on Python 3.14. `tools/validate_local.py`
checks the file against the pinned rule list, refuses to let `manifest.json`
claim a tier, and refuses `test-coverage: done` if the workflow ever loses the
gate.

The scale is a core-integration concept. A custom integration builds to the rules
and is not scored.

## Tests

```bash
python -m pytest tests/ -q
python tools/validate_local.py
```

181 tests cover the pure modules - `analysis`, `fingerprint`, `attribution`,
`verify`, `virtual`, `breaker` - which import nothing from Home Assistant and
are loaded by path, so they run on a bare checkout.

156 more in `tests/ha/` cover the Home Assistant layer: setup, unload and
removal, the config, reconfigure and options flows with recovery from every
error, the entities and their availability, the store and its refusal to
overwrite a probe, the energy dashboard reader, the coordinator's recorder seed
and blind-time accounting, and all six actions driven end to end against
recorded history - including the probe's restore path, its safety refusals and
the automations it pauses. They skip when the harness is absent and do not run
on Windows, where Home Assistant's runner imports `fcntl` and the harness
blocks sockets.

GitHub Actions runs both suites on every push under one coverage measurement
and fails the build below 95%. It is 99%; the only statement not exercised is a
guard for a device that leaves the registry mid-run.

## Licence

MIT.
