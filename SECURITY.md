# Security

Power Fingerprint reads `device_class: power` sensors that Home Assistant
already has, learns fingerprints from them and writes the result to Home
Assistant's own storage. It holds no credential, opens no listener and contacts
no service. Its attack surface is what it puts in a diagnostics download and
what its actions write.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting on
this repository: the Security tab, then Report a vulnerability. An
acknowledgement follows when the report is read.

Include the integration version from `manifest.json`, the Home Assistant
version, and what you did.

## What counts as a security issue

| Case | Why |
|---|---|
| An entity id, area name, device name or free-text option value reaching a diagnostics download unredacted | `diagnostics.py` replaces every entity id with `<domain>.redacted_<8 hex>` and reports `CONF_PAIRS` as a key name only, because an id such as `sensor.master_bathroom_motion_light_power` says where someone lives. A download is routinely pasted into a public issue. |
| An action writing outside the integration's own store | The six actions are `learn`, `label`, `verify_circuit`, `map_devices`, `autolabel` and `apply_circuit_labels`. At runtime the integration writes one file, its own `Store` entry under `.storage`, and reads the recorder. `save_library` writes a path the caller names and is reached only from `tools/identify.py`, which runs on a workstation. |
| A crafted sensor state or unit driving the coordinator into an unhandled exception that stops the entry | The entry has to fail with a message, not take the event loop with it. |

A command that fails safely, raising an error and writing nothing, is an
ordinary bug for the public issue tracker.

## Supported versions

The newest release receives fixes. Earlier ones do not.

## Scope

Home Assistant's own authentication and access control are outside this
project. Report those to
[home-assistant/core](https://github.com/home-assistant/core/security/policy).
