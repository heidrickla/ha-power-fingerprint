"""Local stand-in for the checks CI would run.

GitHub Actions cannot run on this account (spend cap), so hassfest and the HACS
action have never executed against this repo. This approximates the parts that
can be checked offline, so a submission is not the first time anything is
verified. It is NOT a substitute for the real runs - see PUBLISHING.md.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
COMP = os.path.join(ROOT, "custom_components", "power_fingerprint")

# hassfest requires these for a custom integration.
REQUIRED_MANIFEST = [
    "domain",
    "name",
    "documentation",
    "codeowners",
    "iot_class",
    "version",
]
VALID_IOT_CLASS = {
    "assumed_state",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
    "calculated",
}

failures: list[str] = []
notes: list[str] = []


# Pinned from developers.home-assistant.io/docs/core/integration-quality-scale/checklist
ALL_RULES = {
    # Bronze
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    # Silver
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
    # Gold
    "devices",
    "diagnostics",
    "discovery-update-info",
    "discovery",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
    # Platinum
    "async-dependency",
    "inject-websession",
    "strict-typing",
}


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def read_json(*parts: str):
    return json.loads(read(*parts))


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    # ---------------------------------------------------------- manifest
    manifest = read_json(COMP, "manifest.json")
    for key in REQUIRED_MANIFEST:
        check(key in manifest, f"manifest.json missing required key {key!r}")
    check(
        manifest.get("iot_class") in VALID_IOT_CLASS,
        f"manifest iot_class {manifest.get('iot_class')!r} is not a valid value",
    )
    check(
        isinstance(manifest.get("codeowners"), list)
        and all(c.startswith("@") for c in manifest["codeowners"]),
        "manifest codeowners entries must start with @",
    )
    if not manifest.get("documentation", "").startswith("https://"):
        notes.append(
            "documentation URL is not https - must be public before submitting"
        )
    if "10.10." in manifest.get("documentation", ""):
        notes.append("documentation URL points at a LAN host - useless to a user")

    # version must agree with const.py, or HA and HACS report different numbers
    const_src = read(COMP, "const.py")
    const_version = None
    for node in ast.parse(const_src).body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "VERSION":
            const_version = node.value.value
    check(
        const_version == manifest.get("version"),
        f"const.VERSION {const_version!r} != manifest version "
        f"{manifest.get('version')!r} - HA reports one and HACS the other",
    )

    # ---------------------------------------------------------- hacs.json
    hacs = read_json(ROOT, "hacs.json")
    check("name" in hacs, "hacs.json must contain name")

    # ---------------------------------------------------------- brand images
    brand = os.path.join(COMP, "brand")
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        check(os.path.isfile(os.path.join(brand, name)), f"missing brand/{name}")

    # ---------------------------------------------------------- translations
    strings = read_json(COMP, "strings.json")
    en = read_json(COMP, "translations", "en.json")
    check(
        strings.keys() == en.keys(),
        f"strings.json and en.json top-level keys differ: {set(strings) ^ set(en)}",
    )
    for svc in ("learn", "label"):
        check(
            svc in strings.get("services", {}),
            f"service {svc!r} has no translation entry",
        )

    # ---------------------------------------------------------- services.yaml
    services_yaml = os.path.join(COMP, "services.yaml")
    check(os.path.isfile(services_yaml), "services.yaml is missing")
    try:
        import yaml

        declared = set(yaml.safe_load(read(services_yaml)))
        src = read(COMP, "services.py")
        registered = {
            node.value.value
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "").startswith("SERVICE_")
            and isinstance(node.value, ast.Constant)
        }
        check(
            declared == registered,
            f"services.yaml declares {declared} but code registers {registered}",
        )
    except ImportError:
        notes.append("PyYAML not installed - services.yaml not parsed")

    # ---------------------------------------------------------- quality scale
    # The rule list is pinned here on purpose. A quality_scale.yaml that is
    # missing a rule reads as complete - nothing complains, the file just does
    # not mention it - which is the same silent-empty failure this project
    # keeps running into. Checking against the full list turns an omission
    # into a failure.
    scale_path = os.path.join(COMP, "quality_scale.yaml")
    if os.path.isfile(scale_path):
        try:
            import yaml

            declared = yaml.safe_load(read(scale_path)).get("rules", {})
            missing = ALL_RULES - set(declared)
            check(not missing, f"quality_scale.yaml does not mention {sorted(missing)}")
            unknown = set(declared) - ALL_RULES
            check(not unknown, f"quality_scale.yaml invents rules {sorted(unknown)}")
            for rule, value in sorted(declared.items()):
                if isinstance(value, dict):
                    check(
                        value.get("status") in {"done", "todo", "exempt"},
                        f"{rule}: status must be done/todo/exempt",
                    )
                    check(
                        bool(str(value.get("comment", "")).strip()),
                        f"{rule}: a non-done status needs a comment saying why",
                    )
                else:
                    check(value == "done", f"{rule}: bare value must be 'done'")
            todo = sorted(
                r
                for r, v in declared.items()
                if isinstance(v, dict) and v.get("status") == "todo"
            )
            if todo:
                notes.append(f"quality scale still todo: {', '.join(todo)}")
            claimed = manifest.get("quality_scale")
            if claimed:
                check(
                    not todo,
                    f"manifest claims quality_scale {claimed!r} while "
                    f"{len(todo)} rule(s) are still todo",
                )
        except ImportError:
            notes.append("PyYAML not installed - quality_scale.yaml not parsed")
    else:
        notes.append("no quality_scale.yaml")

    # ------------------------------------------------------ icon translations
    # Every translation_key used by an entity needs an icon entry, and every
    # icon entry needs an entity using it - a stale icons.json key is dead
    # weight that looks like coverage.
    icons_path = os.path.join(COMP, "icons.json")
    if os.path.isfile(icons_path):
        icons = read_json(COMP, "icons.json")
        for platform in ("sensor", "binary_sensor"):
            used = set(
                re.findall(
                    r'_attr_translation_key = "([^"]+)"', read(COMP, f"{platform}.py")
                )
            )
            declared_icons = set(icons.get("entity", {}).get(platform, {}))
            check(
                used <= declared_icons,
                f"{platform}: no icon for {sorted(used - declared_icons)}",
            )
            check(
                declared_icons <= used,
                f"{platform}: icons.json has unused keys "
                f"{sorted(declared_icons - used)}",
            )
            entity_strings = set(strings.get("entity", {}).get(platform, {}))
            check(
                used == entity_strings,
                f"{platform}: translation keys {sorted(used)} do not match "
                f"strings.json entity names {sorted(entity_strings)}",
            )
    else:
        notes.append("no icons.json")

    # ------------------------------------------------- exception translations
    raised = set(re.findall(r'translation_key="([^"]+)"', read(COMP, "services.py")))
    declared_exc = set(strings.get("exceptions", {}))
    check(
        raised <= declared_exc,
        f"services.py raises undeclared translation keys "
        f"{sorted(raised - declared_exc)}",
    )
    check(
        declared_exc <= raised,
        f"strings.json declares unused exceptions {sorted(declared_exc - raised)}",
    )

    # ---------------------------------------------------------- syntax
    for dirpath, _dirs, files in os.walk(COMP):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                try:
                    ast.parse(read(path))
                except SyntaxError as err:
                    failures.append(f"{f}: {err}")

    # ---------------------------------------------------------- report
    print(
        f"manifest version {manifest.get('version')}, {len(REQUIRED_MANIFEST)} "
        "required keys checked"
    )
    for n in notes:
        print(f"  NOTE   {n}")
    for f in failures:
        print(f"  FAIL   {f}")
    if not failures:
        print("  all offline checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
