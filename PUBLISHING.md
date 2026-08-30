# Publishing to HACS

Everything in this repo is built to HACS default-store standards, but it is
**not submitted and cannot be yet**. This is the checklist for when it is.

## What blocks submission today

| Blocker | Detail |
|---|---|
| **Repository is private** | HACS requires a public repository. Deliberate for now. |
| **Repository is on Gitea, not GitHub** | HACS submission targets a GitHub repo, and `hacs/default` is a GitHub PR. |
| **GitHub Actions unavailable** | The account has hit its spend cap. The two required workflows exist in `.github/workflows/` but have never run, and a submission needs links to *successful* job runs. |

None of these are code problems. The code is ready; the hosting is not.

## ⚠ The Home Assistant layer tests have never run

`tests/ha/` covers setup and unload, the config and options flow, the derived
sensor values, the coverage fault in both directions, device grouping, and the
orphaned-pause safety backstop. **None of it has ever been executed.**

`pytest-homeassistant-custom-component` does not run on Windows: the harness
blocks sockets during tests, and the Windows ProactorEventLoop builds its own
self-pipe out of a local socket pair, so the block kills the event loop before
any test starts. Home Assistant supports Linux, macOS and the devcontainer for
development. Neither WSL (only a `docker-desktop` distro here) nor disabling
pytest-socket worked, and the harness calls `disable_socket()` programmatically
so the plugin cannot simply be turned off.

They are skipped when the harness is absent, so the default run reports
**53 passed, 1 skipped** and does not imply coverage it does not have.

**Expect some to fail the first time they genuinely execute**, on Linux or in
CI. That is the point at which this section can be deleted.

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

1. **Make the GitHub repo public** with description, topics, licence, README and
   issues enabled. Update `documentation` and `issue_tracker` in
   `manifest.json`, which currently point at the LAN Gitea host and would be
   useless to a user.
2. **Change `codeowners`** if `@heidrickla` is not the right handle.
3. **Push and wait for green.** A push is not done until CI is green.
4. **Bump `manifest.json` AND `const.VERSION` together** to the version about to
   be tagged, push, wait for green *again*.
5. **Create a full GitHub release on that green commit.** Not a tag — a release.
   `--target` needs a branch name or a full 40-character SHA; a short SHA fails
   with the unhelpful pair `tag_name is not a valid tag` /
   `Release.target_commitish is invalid`.
6. **Submit to `hacs/default`**: fork, branch (never the default branch),
   one-line textual insert, PR titled `Adds new integration [<owner>/<repo>]`,
   body = their template with every box ticked and three links — the release,
   the successful HACS action job, the successful hassfest job.
7. **Then leave it alone.** Commenting, opening a second PR, or asking others to
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
   json.loads(new)                          # still valid JSON
   set(new_data) - set(old_data) == {NEW}   # only my entry appeared
   added == 1 and removed == 0              # single-line diff
   key(prev) < key(NEW) < key(next)         # correct sorted position
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
