# Changelog

Notable changes to Power Fingerprint, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions are the ones in `manifest.json` and `const.VERSION`, which are kept equal.

This file starts at 0.17.0. Earlier versions were developed against one install and are not documented here.

## [0.19.1] - 2026-10-01

### Fixed

- A circuit back within its 6-hour limit kept the limit running, and an outage ending near it dropped its evidence.
- A circuit that read dead again before its incident closed kept its first return: the controls ran with the circuit dead. The return now waits for the next live reading, and requests around the short return say nothing.
- The controls after the return are cut off 120 s after it, and an answer after that says nothing. A long list of devices that do not answer no longer outlasts the run.
- A disabled lock, cover, valve, alarm panel or battery entity no longer lets its device be probed.
- A TV's `remote` going off while its `media_player` stayed on counted as the TV losing power. A TV is now on while any of its media entities is.
- An outage casualty whose main entities are disabled was stored under its ping button.
- A second `record_breaker_evidence` call for a trip replaced its `outage`; it now adds to it.
- `record_breaker_evidence` refuses `devices` and `recovered` for a trip whose circuit has not read live again, and checks `recovered` times: refused before the fall, answered after the return.

## [0.19.0] - 2026-09-30

### Added

- Outage evidence during a breaker trip, for devices that stay available through an outage of minutes. Mains Zigbee devices get Identify with a time of zero 15 s after the fall and again 30 s after the return to each that failed; mains Z-Wave nodes get a ping. A Zigbee failure with the circuit dead and an answer after the return, a Z-Wave node `dead` with the circuit and `alive` with the power, and a TV `off` within 30 s of the fall with no user context and on again with the power each make the device `suspected` on the tripped circuit. The trip gains `outage`, and the event fires again with `amended: true`.
- Option `Probe devices during a breaker trip`, on by default.
- Action `record_breaker_evidence`: outage evidence gathered by hand, added to a recorded trip.
- `conflicts` on a circuit assignment: the last five answers naming another circuit.

### Changed

- A circuit assignment is replaced by evidence strength: `verify_circuit` `measured`, then a trip's `confirmed` meter, then an `inferred` probe or a device that stopped answering, then a trip's `suspected` device, then correlation. Before, only a probe's answer was protected, from every other source.

## [0.18.0] - 2026-09-29

### Added

- Breaker trips detected automatically. A trip needs exactly one watched circuit to fall to 2 W; a second circuit falling from 30 s before until the incident closes makes it a refusal, and devices dropping with every circuit live are ignored. A circuit is watched when its hourly minimum over 720 hours never reached 2 W and stayed above the eligibility floor the confidence setting sets (6, 4 or 3 W); only a trip that took devices with it is left out of that history. Detection starts 5 minutes after Home Assistant has started or the entry has loaded.
- Per trip: one circuit assignment per device, `established_by: breaker`, `confidence: inferred`, never over a probe's answer; the event `power_fingerprint_breaker_trip`; `breaker_trip_circuits` and `last_breaker_trip` on `sensor.power_fingerprint_unmonitored_load`; each circuit's eligibility, the last trip and the last refusal in diagnostics.
- `tests/fixtures/breaker_trips.json`: recorder windows from one install with generic entity ids, replayed by `tests/test_breaker_replay.py`.

### Changed

- The README's breaker walk, a manual procedure no action exposed, is replaced by the automatic detection.
- The README states behaviour and measurements without version history or dated checks.

## [0.17.2] - 2026-09-28

### Added

- A library audit: names used by two or more shapes on one circuit, and shapes learned from three runs or fewer. It reports and changes nothing. Shown in the attributes of `sensor.power_fingerprint_unnamed_candidates` (`duplicate_labels`, `small_shapes`), in the `learn` response (`audit`) and in diagnostics, where circuits are pseudonymised and labels omitted.
- `tools/labelled_set.py`. Builds a labelled set of runs from exported recorder history (dedicated circuits, metered devices, probe placements and sums of dedicated circuits), freezes it by SHA-256, and scores matching changes on a held-out part, per run or per coordinator poll. `stability` reports each learned shape's bootstrap stability.

### Changed

- Storage minor version 2. Loading a minor-1 store rewrites a circuit assignment marked `measured` whose source is not a probe as `inferred`.
- A circuit assignment is stored `measured` only when a probe made it; any other source is stored `inferred`.

## [0.17.1] - 2026-09-16

### Added

- `SECURITY.md`. It names where to report privately, what the diagnostics redaction covers, and the one file the integration writes at runtime.

### Changed

- `PUBLISHING.md` no longer ships. It described a maintainer workflow, not the integration, and was the only file a HACS download carried that a user had no use for.
- `tools/validate_local.py` fails on a private-network host in `documentation`, in `issue_tracker`, or anywhere in the published tree. It previously printed a note and exited 0. It also refuses a development host named bare in prose; the names come from `PF_INTERNAL_HOSTS`, the gitignored `.internal-hosts` file or the configured git remotes, so no name is published to enforce the rule.
- `tools/validate_local.py` refuses the carrier-grade NAT range, the unspecified address, the reserved ranges and the `.corp`, `.home`, `.home.arpa`, `.intranet` and `.localdomain` suffixes. Its file list comes from `git ls-files` instead of a directory walk, so it reads extensionless published files such as `.gitattributes`, which the walk skipped, and skips an ignored path.
- `tools/validate_local.py` refuses a version string that equals the newest reachable tag when commits sit on top of it. The three version strings were only ever checked against each other, which passes when all three are stale together.
- The installation section states which reader gets the in-repo brand icon and which gets the CDN placeholder.
- `tools/identify.py` states `HA_URL` and `HA_TOKEN` in its usage line, and a missing one names itself instead of raising `KeyError`.
- The workflow headers and `.gitattributes` describe what the jobs do without naming the machines they run on.
- The Home Assistant layer tests run on Windows. `tests/winposix.py` supplies `fcntl` and `resource`, releases `socketpair` from the harness's socket block, and selects the selector event loop; it is inert on every other platform.
- `tools/validate_local.py` states which of its host rules ran instead of failing when it can derive no development host name. A checkout has neither the environment variable nor the gitignored `.internal-hosts` file, and the code host's name parts are generic, so the bare-name half cannot run there. The note names `PF_INTERNAL_HOSTS` and lists the rules the result covers, so a scan with one rule off cannot read as a scan that found nothing. Neither workflow supplies the names: a report names the file and the line, which on a public repository locates the string whether or not the string itself is printed, and a repository secret would be a second copy of the names outside the network.
- `tools/validate_local.py` prints no matched string under `CI`. Every report carries the file, the line and the rule that matched, with the matched text replaced by `[redacted]`; a local run prints it, which is what makes the line findable. A fixed placeholder rather than a digest or a truncation, because both of those still narrow a host name short enough to guess.
- `tools/validate_local.py` fires each matcher on a control line before it reports a clean result. The controls are built at runtime from the pinned CIDRs and the pinned suffix list, one per rule, and each asserts its own rule fired rather than that any rule did. A tree that holds nothing and a matcher that matches nothing otherwise print the same result.
- The ruff target is `py314`, the version both workflows install and the version the mypy block pins. At that target `ruff format` writes the PEP 758 form of a multi-exception `except`, so ten clauses across six files lost their parentheses and the source now needs Python 3.14. Home Assistant 2026.3.0, the floor the manifest declares, itself declares `requires-python >=3.14.2`, so no interpreter able to run a supported Home Assistant can fail to parse it. `requires-python` in `pyproject.toml` states 3.14 for the same reason.
- `tools/validate_local.py` scans `.html` as well. The scan selects files by suffix, so an HTML file naming a development host shipped with the scan green.
- `services.yaml` carries selectors, defaults and `required` only. Every action name, field name and description comes from `strings.json`, which is what the three other actions already did. Home Assistant reads both files - `async_get_all_descriptions` serves the YAML text, the `services` translation category serves the JSON text - so the two copies drifted on 15 of 28 strings. `tools/validate_local.py` now refuses `name` or `description` in `services.yaml`.
- `tools/validate_local.py` matches an IPv6 literal in the published tree, and a bracketed IPv6 host inside a URL. The two IPv6 ranges in `tools/_netblocks.py` had no path to a match: the literal pattern took dotted quads only, and the URL pattern stopped at the opening bracket, which left `urlsplit` raising on an unbalanced bracket. A line naming either is now reported once rather than once per pattern that matched it.
- `tools/validate_local.py` runs the host rules over every commit the tracking branch does not have, both each commit's changed files and its message. It read the working tree only, so a name removed by the newest commit was invisible to it while every earlier commit still carried it and a push carried them all. `PF_PUSH_RANGE` sets the range, and both workflows set it from the push event.
- `tools/validate_local.py` matches a development host name with no left word boundary. `\b` before the name did not hold where the name sat after a backslash escape in a regex source, which is where one was.
- `tools/validate_local.py` allows a CIDR whose host bits are zero, such as `10.0.0.0/8`, and still refuses a literal with a host part written with a prefix length after it.
- The four `tools/` scripts print their usage block as written. `argparse` rewrapped it by default, which ran the example commands together with the prose around them.
- `tools/validate_local.py` refuses a MAC, EUI-48 or EUI-64 literal that carries a vendor OUI. `ipaddress` parses an eight-group EUI-64 as a global IPv6 address, which sits in none of the refused ranges, so the address rule dropped it and a real Zigbee identifier passed the scan clean. Locally administered addresses and the pinned documentation forms are exempt.
- `tools/validate_local.py` asserts its own redaction under `CI` whatever the scans were given. The assertion sat after an early return taken when no host name was supplied, which is every CI configuration this repository has, so a regression in the redaction would have printed an estate address in a public log on a green run.
- `tools/validate_local.py` reports a derived commit range that read nothing as coverage the run did not have, not as a count of zero. A checkout points the tracking branch at the commit it checked out, so `origin/main..HEAD` resolves there and is empty however many commits the push carried, and the run printed a count of zero while reading no commit.
- Both workflows set `PF_PUSH_RANGE` from the push event and check out with `fetch-depth: 0` and `fetch-tags: true`. The default `--no-tags --depth=1` leaves `git describe --tags` with nothing to find, so the released-version check noted that it could not compare rather than running.
- `tools/hooks/pre-push` runs the validator at push time. The bare-name half of the host scan reads a gitignored file and so reaches no CI run, which left it a step someone remembers rather than one the push runs. The hook picks its interpreter by version, because the validator is 3.14 source and an older interpreter reports a SyntaxError in place of a verdict.

### Fixed

- Docstrings, comments and test fixtures no longer name the maintainer's own devices or breaker assignments. The example entity ids are invented and the example circuit numbers match no panel. `tools/one_appliance.py` prints the circuit it was given rather than a fixed one, and its `--mains` default is `sensor.mains_power`, as is `tools/virtual_circuits.py`'s.
- The diagnostics pseudonym is `<domain>.redacted_<n>`, numbered from one per download, and no longer a truncated digest of the entity id. The digest was unsalted over a low-entropy string: a 14,300-candidate dictionary built from five domains, 22 room words, 26 device words and five suffixes recovered all three of the ids this integration names as examples in 0.01 s on one core. A download is pasted into a public issue, and correlating one circuit within one report is all the pseudonym has to do.
- Renaming a circuit no longer rewrites the unique id of a circuit whose id has the renamed one as a prefix. Renaming `sensor.kitchen_power` also rewrote `sensor.kitchen_power_2`, which detached that circuit's recorder history and minted a duplicate entity on the next reload. The same exact-id match now applies to the contradiction pairs, where a comment or a malformed line is left as written.

## [0.17.0] - 2026-09-04

### Added

- The learned fingerprint library in `.storage` is now deleted when the entry is removed. The README had said so for several versions and nothing did it.
- A repair issue names any configured sensor whose unit stops being a power unit, so a circuit silently missing from every total is visible in the UI rather than only in the log.
- `CHANGELOG.md`, and a README section for each action's fields, the removal steps, the reconfigure and options behaviour, and the entity ids as they actually appear.

### Changed

- Minimum Home Assistant is now 2026.3. The config flow uses APIs added in 2024.12 and the brand images ship inside the repository, which Home Assistant serves from 2026.3.
- A new Circuit entity on a mapped device is named after that device, so it arrives as `sensor.porch_lamp_circuit` instead of `sensor.circuit_2`. Entities that already exist keep the id they have; the unique id is unchanged either way, so no history moves.
- Setup and recorder errors carry translated messages, so the integration card and the automation trace say what to do rather than showing a raw string.
- A configured sensor going away is logged once at info when it goes and once when it returns, instead of every thirty seconds at warning.
- `services.yaml` no longer prefills `threshold`, `probes` and `min_correlation` in the UI. A prefilled field overrode the value the confidence setting is there to supply.

### Fixed

- A config flow error about a circuit sensor said "the mains reading", which sent people to fix the wrong sensor. Errors now name the failing entity and say whether it was the mains or a circuit.
- An appliance sensor whose circuit cannot be read reports `unavailable` instead of `unknown`, which means "a shape no fingerprint accounts for".
- Renaming a switch used in a switch/circuit pair is followed. The rename tracker split the pair field the wrong way, so the switch half was never watched and the contradiction check quietly stopped working after a rename.
- `learn` and `map_devices` with the recorder disabled now refuse with a message naming the recorder, instead of raising a bare `KeyError` that reached the frontend as a stack trace.

### Quality scale

- Every rule in `quality_scale.yaml` is now `done` or `exempt` with a written reason. `test-coverage` was the last one open.
- Coverage is 99% of `custom_components/power_fingerprint` and the GitHub `Tests` workflow fails below 95%. Both suites run under one measurement.
- Every push runs the Home Assistant layer tests, mypy with the full strict block and the offline validator against Home Assistant 2026.8.3 on Python 3.14. They previously ran only on manual dispatch.
- `tools/validate_local.py` checks versions across `manifest.json`, `const.VERSION` and `pyproject.toml`, scans every module for an untranslated exception, and refuses `test-coverage: done` if the coverage gate is removed.
