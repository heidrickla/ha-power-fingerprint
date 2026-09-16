# Changelog

Notable changes to Power Fingerprint, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions are
the ones in `manifest.json` and `const.VERSION`, which are kept equal.

This file starts at 0.17.0. Earlier versions were developed against one install
and are not documented here.

## [0.17.1] - 2026-09-16

### Changed

- `PUBLISHING.md` no longer ships. It described a maintainer workflow, not the
  integration, and was the only file a HACS download carried that a user had
  no use for.
- `tools/validate_local.py` fails on a private-network host in `documentation`,
  in `issue_tracker`, or anywhere in the published tree. It previously printed
  a note and exited 0. It also refuses a development host named bare in prose;
  the names come from `PF_INTERNAL_HOSTS`, the gitignored `.internal-hosts`
  file or the configured git remotes, so no name is published to enforce the
  rule.
- `tools/validate_local.py` refuses the carrier-grade NAT range, the
  unspecified address, the reserved ranges and the `.corp`, `.home`,
  `.home.arpa`, `.intranet` and `.localdomain` suffixes. Its file list comes
  from `git ls-files` instead of a directory walk, so it reads extensionless
  published files such as `.gitattributes`, which the walk skipped, and skips
  an ignored path.
- `tools/validate_local.py` refuses a version string that equals the newest
  reachable tag when commits sit on top of it. The three version strings were
  only ever checked against each other, which passes when all three are stale
  together.
- The installation section states which reader gets the in-repo brand icon and
  which gets the CDN placeholder.
- `tools/identify.py` states `HA_URL` and `HA_TOKEN` in its usage line, and a
  missing one names itself instead of raising `KeyError`.
- The workflow headers and `.gitattributes` describe what the jobs do without
  naming the machines they run on.
- The Home Assistant layer tests run on Windows. `tests/winposix.py` supplies
  `fcntl` and `resource`, releases `socketpair` from the harness's socket
  block, and selects the selector event loop; it is inert on every other
  platform.
- `tools/validate_local.py` fails under `CI` when it can derive no development
  host name. A checkout has neither the environment variable nor the gitignored
  `.internal-hosts` file, and the code host's name parts are generic, so the
  bare-name rule matched nothing and the step went green on a rule that did not
  run. Both workflows now pass `PF_INTERNAL_HOSTS` from a repository secret.
- The ruff target is `py314`, the version both workflows install and the
  version the mypy block pins. At that target `ruff format` writes the PEP 758
  form of a multi-exception `except`, so eleven clauses across six files lost
  their parentheses and the source now needs Python 3.14. Home Assistant
  2026.3.0, the floor the manifest declares, itself declares
  `requires-python >=3.14.2`, so no interpreter able to run a supported Home
  Assistant can fail to parse it. `requires-python` in `pyproject.toml` states
  3.14 for the same reason.
- `tools/validate_local.py` scans `.html` as well. The scan selects files by
  suffix, so an HTML file naming a development host shipped with the scan
  green.

### Fixed

- Renaming a circuit no longer rewrites the unique id of a circuit whose id has
  the renamed one as a prefix. Renaming `sensor.kitchen_power` also rewrote
  `sensor.kitchen_power_2`, which detached that circuit's recorder history and
  minted a duplicate entity on the next reload. The same exact-id match now
  applies to the contradiction pairs, where a comment or a malformed line is
  left as written.

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

- Minimum Home Assistant is now 2026.3. The config flow uses APIs added in
  2024.12 and the brand images ship inside the repository, which Home
  Assistant serves from 2026.3.
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
  3.14. They previously ran only on manual dispatch.
- `tools/validate_local.py` checks versions across `manifest.json`,
  `const.VERSION` and `pyproject.toml`, scans every module for an untranslated
  exception, and refuses `test-coverage: done` if the coverage gate is
  removed.
