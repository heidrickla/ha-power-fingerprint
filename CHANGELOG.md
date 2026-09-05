# Changelog

Notable changes to Power Fingerprint, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions are
the ones in `manifest.json` and `const.VERSION`, which are kept equal.

This file starts at 0.17.0. Earlier versions were developed against one install
and are not documented here.

## [0.17.0] - 2026-09-04

### Added

- The learned fingerprint library in `.storage` is now deleted when the entry
  is removed. The README had said so for several versions and nothing did it.
- A repair issue names any configured sensor whose unit stops being a power
  unit, so a circuit silently missing from every total is visible in the UI
  rather than only in the log.
- `CHANGELOG.md`, and a README section for each action's fields, the removal
  steps, the reconfigure and options behaviour, and the entity ids as they
  actually appear.

### Changed

- **Minimum Home Assistant is now 2026.3.** The config flow uses APIs added in
  2024.12 and the brand images ship inside the repository, which HACS reads
  from 2026.3.
- A new Circuit entity on a mapped device is named after that device, so it
  arrives as `sensor.porch_lamp_circuit` instead of `sensor.circuit_2`.
  Entities that already exist keep the id they have; the unique id is
  unchanged either way, so no history moves.
- Setup and recorder errors carry translated messages, so the integration card
  and the automation trace say what to do rather than showing a raw string.
- A configured sensor going away is logged once at info when it goes and once
  when it returns, instead of every thirty seconds at warning.
- `services.yaml` no longer prefills `threshold`, `probes` and
  `min_correlation` in the UI. A prefilled field overrode the value the
  confidence setting is there to supply.

### Fixed

- A config flow error about a circuit sensor said "the mains reading", which
  sent people to fix the wrong sensor. Errors now name the failing entity and
  say whether it was the mains or a circuit.
- An appliance sensor whose circuit cannot be read reports `unavailable`
  instead of `unknown`, which means "a shape no fingerprint accounts for".
- Renaming a switch used in a switch/circuit pair is followed. The rename
  tracker split the pair field the wrong way, so the switch half was never
  watched and the contradiction check quietly stopped working after a rename.
- `learn` and `map_devices` with the recorder disabled now refuse with a
  message naming the recorder, instead of raising a bare `KeyError` that
  reached the frontend as a stack trace.

### Quality scale

- Every rule in `quality_scale.yaml` is now `done` or `exempt` with a written
  reason. `test-coverage` was the last one open.
- Coverage is 99% of `custom_components/power_fingerprint` and the GitHub
  `Tests` workflow fails below 95%. Both suites run under one measurement.
- Every push runs the Home Assistant layer tests, mypy with the full strict
  block and the offline validator against Home Assistant 2026.8.3 on Python
  3.14. They previously ran only on manual dispatch on the private forge.
- `tools/validate_local.py` checks versions across `manifest.json`,
  `const.VERSION` and `pyproject.toml`, scans every module for an untranslated
  exception, and refuses `test-coverage: done` if the coverage gate is
  removed.
