# Publishing to HACS

Everything in this repo is built to HACS default-store standards. It is public
on GitHub with green checks and **not yet submitted**. This is the checklist
for the rest.

## Status

| Step | State |
|---|---|
| Public GitHub repository | Done 2026-09-04: `heidrickla/ha-power-fingerprint`, issues on, topics set. The personal forge stays the canonical remote (`gitea`); GitHub is `origin`. |
| HACS and hassfest actions green | Done on `main` at `462200e`; both run on every push. The first public run failed hassfest on manifest key order and on an undeclared `energy` import; both fixed the same hour. |
| Quality scale | Done 2026-09-04: every rule `done` or `exempt`, coverage gated at 95% in the `Tests` workflow, mypy strict on every push. |
| Changelog | Done 2026-09-04: `CHANGELOG.md`, starting at 0.17.0. |
| Release | **Not created.** The version to tag is whatever `manifest.json` and `const.VERSION` carry at the time; they are 0.17.0 now. |
| `hacs/default` pull request | **Not opened.** |

## The Home Assistant layer tests run on Linux, not here

`tests/ha/` is 156 tests covering setup, unload and removal (the store goes
with the entry), the config, reconfigure and options flows with every
validation refusal followed by a recovery to a created entry, the dashboard
price and circuit names in every shape the energy schema has had, the derived
sensor values, the coverage fault in both directions, the appliance sensor
appearing when a shape is named and following its circuit's availability,
device grouping, the store's refusal to overwrite a probe with a passive
answer, the rename migration, the recorder seed and the blind-time accounting
behind the absence alert, the orphaned-pause safety backstop, component-level
action registration, the recorder guard on `learn`, `ConfigEntryNotReady`
while the power sensors are missing, and unit conversion at ingestion —
including the kilowatt-mains-against-watt-circuits case that was found on the
development install itself.

All six actions are driven end to end against recorded history rather than
patched out: `learn` from a trace through segmenting, clustering and cadence
into the store, `label` and `autolabel` naming what it found, `verify_circuit`
probing a switch against services that really move a circuit — with its
restore path, its five refusals and the automations it pauses — `map_devices`
placing a device by correlation, and `apply_circuit_labels` in dry run, apply
and remove.

 **Installing the integration on a real Home Assistant found two defects the
same afternoon, one of which this suite would have caught immediately**: a
`len()` on an int that made the diagnostics download return HTTP 500, and a
recorder seed that asked for every entity in the house, failed, swallowed the
failure into a debug line, and left standby reporting 5,017 W against a true
figure of 3 W. Written tests that never run are not coverage.

They run in GitHub Actions on every push and pull request, in the `Tests`
workflow, against Home Assistant 2026.8.3 on Python 3.14, alongside mypy with
the full strict block and the offline validator. Coverage spans both suites and
**the build fails below 95%**; it is 99%. The forge's `Home Assistant layer`
job runs the same steps on manual dispatch. They do not run on Windows: Home
Assistant's runner imports `fcntl`, and the harness blocks sockets. That is
expected rather than a defect — Home Assistant supports Linux, macOS and the
devcontainer for development.

They skip when the harness is absent, so a bare checkout reports **181 passed,
1 skipped** and does not imply coverage it does not have. With Home Assistant
installed, the extra 156 run as well.

## Quality scale

`custom_components/power_fingerprint/quality_scale.yaml` tracks every rule in
Home Assistant's Integration Quality Scale, with a written reason on each
exemption. `tools/validate_local.py` checks it against the pinned rule list, so
a rule that is simply *missing* from the file fails rather than reading as
complete.

No rule is `todo`. Every one is `done` or `exempt`, each with a written reason;
`test-coverage` closed on 2026-09-04 when the four large actions, the store and
the dashboard reader got Home Assistant layer tests and the 95% gate landed in
the workflow. mypy with the full strict block passes against Home Assistant
2026.8.3, verified locally on 2026-09-04 and run again on every push.

 **`quality_scale` is deliberately absent from `manifest.json`.** The validator
refuses a manifest that claims a tier. The scale is a core-integration concept —
a custom integration builds to the rules, it does not get the badge.

## Already done

- `custom_components/power_fingerprint/` layout, `hacs.json` with `name`
- All mandatory `manifest.json` keys, `version` matching `const.VERSION`
- **Brand images in-repo** at `custom_components/power_fingerprint/brand/`
 (256×256, 512×512, and logos), generated and size-checked by
 `tools/make_brand.py`
- Both required workflows: `hacs/action` with `category: integration` and
 **no `ignore:` key**, plus `home-assistant/actions/hassfest`
- Tests and lint green, config flow, options flow, translations, diagnostics,
 device grouping, MIT licence, README

## Remaining, in order — the order is load-bearing

Done on 2026-09-04: public repo with description, topics, licence, README and
issues; `manifest.json` pointing at GitHub; `codeowners` `@heidrickla`;
pushed and green.

1. **Bump `manifest.json` AND `const.VERSION` together** to the version about to
 be tagged, push, wait for green *again*.
2. **Create a full GitHub release on that green commit.** Not a tag — a release.
 `--target` needs a branch name or a full 40-character SHA; a short SHA fails
 with the unhelpful pair `tag_name is not a valid tag` /
 `Release.target_commitish is invalid`.
3. **Submit to `hacs/default`**: fork, branch (never the default branch),
 one-line textual insert, PR titled `Adds new integration [<owner>/<repo>]`,
 body = their template with every box ticked and three links — the release,
 the successful HACS action job, the successful hassfest job.
4. **Then leave it alone.** Commenting, opening a second PR, or asking others to
 comment all *delay* review. The queue is oldest-first. Updating your own
 repository while queued is allowed.

## The four traps

These come from `publishing-a-home-assistant-integration-to-hacs` in the Brain,
derived from actually submitting `ha-culligan-azure` (`hacs/default#9914`) and
re-releasing `ha-tuxedo-touch` v0.2.1.

1. **No brands PR is needed.** Since Home Assistant 2026.3 a custom integration
 carries its own brand images and HACS reads the in-repo `brand/` directory
 first. `home-assistant/brands` now calls `custom_integrations/` a legacy
 folder. Opening a brands PR is dead work. **Already satisfied here.**
2. **Ordering is load-bearing.** The release must be created *after* a
 successful validation run, and must be a full release rather than a tag. Cut
 the release first and the submission fails even though the repo is green,
 because the check is about the order the artifacts were created in. Nothing
 looks wrong — that is the expensive part.
3. **`manifest.json` version must match the release tag.** Home Assistant
 reports the manifest version while HACS surfaces the tag, so a mismatch shows
 users a wrong version number. Here `const.VERSION` must match too — it feeds
 the device registry entry.
4. **The `hacs/default` diff must be exactly one line.** Their `integration`
 file is a ~3,100-entry JSON array containing at least one entry missing its
 indentation, so `json.dumps` silently "fixes" it and turns a 1-line diff into
 a 2-line diff. **Insert the line textually**, and prove it before pushing:

 ```python
 json.loads(new) # still valid JSON
 set(new_data) - set(old_data) == {NEW} # only my entry appeared
 added == 1 and removed == 0 # single-line diff
 key(prev) < key(NEW) < key(next) # correct sorted position
 ```

 Their order is case-insensitive sorted — measure it rather than assume.
 Confirm with the compare API before opening the PR:

 ```bash
 gh api repos/hacs/default/compare/master...<you>:default:<branch> \
 --jq '"files=\(.files|length) +\(.files[0].additions)/-\(.files[0].deletions)"'
 # want: files=1 +1/-0
 ```

## One known race

`Editable PR` failed on the first workflow run of `#9914` and passed on a re-run
seconds later with nothing changed; `maintainer_can_modify` read `true`
throughout. Treat a lone `Editable PR` failure as a race and confirm against the
later run before acting on it.
