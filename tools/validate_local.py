"""Local stand-in for the checks CI would run.

hassfest and the HACS action run in GitHub Actions; this approximates the
parts of them that can be checked with no network at all, plus the cross-file
consistency that nothing else checks: translation keys against icons,
exceptions raised against exceptions declared, actions registered against
actions described, versions against each other, the quality scale against the
pinned rule list. Run it before a push so the push is not the first
verification.

    python tools/validate_local.py
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import sys
import tomllib
import urllib.parse
from typing import Any

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DOMAIN = "power_fingerprint"
COMP = os.path.join(ROOT, "custom_components", DOMAIN)
PLATFORMS = ("binary_sensor", "sensor")

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
# The two manifest keys that are URLs a user clicks. hassfest checks neither
# for reachability, so a development address committed to either one reaches
# the published repo with every check green.
URL_MANIFEST_KEYS = ("documentation", "issue_tracker")
# Pinned rather than delegated to ipaddress.is_private, whose membership has
# changed between Python releases.
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
PRIVATE_SUFFIXES = (".local", ".lan", ".internal")
# Directories whose text ships to whoever clones the repo, and the suffixes
# worth reading in them.
PUBLISHED_DIRS = (".gitea", ".github", "custom_components", "tests", "tools")
PUBLISHED_SUFFIXES = {".py", ".yml", ".yaml", ".json", ".md", ".toml", ".cfg"}
# A dotted-quad anywhere in that text, tested against PRIVATE_NETWORKS rather
# than listed a second time.
IP_LITERAL_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Any URL in that text, so a development host reached by name rather than by
# address is caught by the same rule the manifest keys use. A bare host named
# in prose with no scheme is not caught by anything here.
URL_RE = re.compile(r"\bhttps?://[^\s\"'`<>)\]]+")

# Pinned from developers.home-assistant.io/docs/core/integration-quality-scale/checklist
# (checked 2026-09-02: 54 rules, none new or deprecated). The list is pinned
# here on purpose: a quality_scale.yaml that is missing a rule reads as
# complete, and checking against the full list turns an omission into a
# failure.
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

failures: list[str] = []
notes: list[str] = []


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def read_json(*parts: str) -> Any:
    return json.loads(read(*parts))


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def private_host(url: str) -> str | None:
    """The host of `url` when nobody outside this network could follow it.

    Returns None for a host a user can reach, so a caller reads a string as
    the reason to refuse. `urlsplit().hostname` lowercases the host and strips
    the brackets from an IPv6 literal.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return None
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A name with no dot in it resolves only against a local search
        # domain, so it is a LAN name rather than a public one.
        return host if "." not in host else None
    return host if any(address in net for net in PRIVATE_NETWORKS) else None


def internal_address(text: str) -> bool:
    """True when `text` is an address only this network can reach."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(address in net for net in PRIVATE_NETWORKS)


def published_files() -> list[str]:
    """Every text file that ships, excluding this one.

    This file carries the forbidden patterns as its own constants, so scanning
    it would report itself.
    """
    this_file = os.path.abspath(__file__)
    found: list[str] = [
        os.path.join(ROOT, f)
        for f in ("README.md", "CHANGELOG.md", "hacs.json", "pyproject.toml")
        if os.path.exists(os.path.join(ROOT, f))
    ]
    for root_dir in PUBLISHED_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(ROOT, root_dir)):
            dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
            for f in files:
                path = os.path.join(dirpath, f)
                if os.path.splitext(f)[1] not in PUBLISHED_SUFFIXES:
                    continue
                if os.path.abspath(path) == this_file:
                    continue
                found.append(path)
    return found


def constants(source: str, prefix: str) -> dict[str, str]:
    """Module-level string assignments whose name starts with prefix."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id.startswith(prefix)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target.id] = node.value.value
    return found


# An exception is raised with translation_domain=DOMAIN right before its key;
# entity and issue keys never carry translation_domain. Subtracted from the
# entity scan so an error raised inside a platform file is not read as one of
# that platform's entities.
EXC_RE = re.compile(r'translation_domain=DOMAIN,\s*translation_key="([^"]+)"')
# The user-facing exception classes. Any raise of one of these must carry a
# translation key, in every module - not only services.py, which is how two
# f-string ConfigEntryNotReady messages went unnoticed.
RAISE_RE = re.compile(
    r"raise\s+(ConfigEntryNotReady|ConfigEntryAuthFailed|ConfigEntryError|"
    r"UpdateFailed|HomeAssistantError|ServiceValidationError)\s*\("
)
# A repair issue's key follows the async_create_issue call.
ISSUE_RE = re.compile(
    r'async_create_issue\((?:(?!\)\s*\n\s*\n).)*?translation_key="([^"]+)"',
    re.DOTALL,
)


def main() -> int:
    manifest = read_json(COMP, "manifest.json")
    const_src = read(COMP, "const.py")
    strings = read_json(COMP, "strings.json")

    # ---------------------------------------------------------- manifest
    for key in REQUIRED_MANIFEST:
        check(key in manifest, f"manifest.json missing required key {key!r}")
    check(
        manifest.get("domain") == DOMAIN,
        f"manifest domain is {manifest.get('domain')!r}",
    )
    check(
        manifest.get("iot_class") in VALID_IOT_CLASS,
        f"manifest iot_class {manifest.get('iot_class')!r} is not a valid value",
    )
    check(
        isinstance(manifest.get("codeowners"), list)
        and all(c.startswith("@") for c in manifest["codeowners"]),
        "manifest codeowners entries must start with @",
    )
    keys = list(manifest)
    check(
        keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:]),
        "manifest keys must be domain, name, then alphabetical (hassfest MANIFEST)",
    )
    for url_key in URL_MANIFEST_KEYS:
        host = private_host(str(manifest.get(url_key, "")))
        check(
            host is None,
            f"manifest {url_key} host {host!r} is not reachable from outside "
            "this network - a user cannot follow it",
        )
    check(
        "quality_scale" not in manifest,
        "quality_scale in manifest.json: the badge is core-only, a custom "
        "integration builds to the rules and does not claim a tier",
    )

    # ---------------------------------------------------------- versions
    # Three places carry the version and each is read by someone: Home
    # Assistant reports the manifest, the device registry shows const.VERSION,
    # and pyproject is what a packaging tool would read.
    const_version = constants(const_src, "VERSION").get("VERSION")
    check(
        const_version == manifest.get("version"),
        f"const.VERSION {const_version!r} != manifest version "
        f"{manifest.get('version')!r} - HA reports one and HACS the other",
    )
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        pyproject = tomllib.load(fh)
    project_version = pyproject.get("project", {}).get("version")
    check(
        project_version == manifest.get("version"),
        f"pyproject version {project_version!r} != manifest version "
        f"{manifest.get('version')!r}",
    )

    # ---------------------------------------------------------- hacs.json
    hacs = read_json(ROOT, "hacs.json")
    check("name" in hacs, "hacs.json must contain name")
    check(
        "homeassistant" in hacs,
        "hacs.json must declare a minimum homeassistant version",
    )

    # ---------------------------------------------------------- brand images
    brand = os.path.join(COMP, "brand")
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        check(os.path.isfile(os.path.join(brand, name)), f"missing brand/{name}")

    # ---------------------------------------------------------- translations
    en = read_json(COMP, "translations", "en.json")
    check(
        strings == en,
        "strings.json and translations/en.json differ - copy strings.json over",
    )
    # The same six fields appear on the user, reconfigure and options forms;
    # each block must describe them all or the form shows raw keys.
    step_labels = [
        strings["config"]["step"]["user"]["data"],
        strings["config"]["step"]["reconfigure"]["data"],
        strings["options"]["step"]["init"]["data"],
    ]
    conf_fields = set(constants(const_src, "CONF_").values())
    for labels in step_labels:
        check(
            set(labels) == conf_fields,
            f"a form labels {sorted(labels)} but const.py declares "
            f"{sorted(conf_fields)}",
        )
    # Both flows validate with the same function and the same error keys.
    check(
        set(strings["config"]["error"]) == set(strings["options"]["error"]),
        "config.error and options.error declare different keys",
    )
    flow_src = read(COMP, "config_flow.py")
    used_errors = set(
        re.findall(r'"((?:mains|circuit)_[a-z_]+|no_circuits)"', flow_src)
    )
    # The kind-prefixed keys are built as f"{kind}_missing"; expand them.
    for suffix in re.findall(r'f"\{kind\}_([a-z_]+)"', flow_src):
        used_errors |= {f"mains_{suffix}", f"circuit_{suffix}"}
    used_errors |= set(re.findall(r'"(not_power_unit)"', flow_src))
    check(
        used_errors <= set(strings["config"]["error"]),
        f"config_flow.py returns undeclared error keys "
        f"{sorted(used_errors - set(strings['config']['error']))}",
    )
    check(
        set(strings["config"]["error"]) <= used_errors,
        f"strings.json declares unused error keys "
        f"{sorted(set(strings['config']['error']) - used_errors)}",
    )

    # ---------------------------------------------------------- actions
    services_yaml = os.path.join(COMP, "services.yaml")
    check(os.path.isfile(services_yaml), "services.yaml is missing")
    services_src = read(COMP, "services.py")
    service_consts = set(constants(services_src, "SERVICE_").values())
    declared_services = set(strings.get("services", {}))
    check(
        declared_services == service_consts,
        f"strings.json describes {sorted(declared_services)} but services.py "
        f"registers {sorted(service_consts)}",
    )
    try:
        import yaml

        services = yaml.safe_load(read(services_yaml)) or {}
        check(
            set(services) == service_consts,
            f"services.yaml declares {sorted(services)} but services.py registers "
            f"{sorted(service_consts)}",
        )
        for name, spec in services.items():
            yaml_fields = set((spec or {}).get("fields", {}))
            described = set(strings.get("services", {}).get(name, {}).get("fields", {}))
            check(
                yaml_fields == described,
                f"action {name}: services.yaml fields {sorted(yaml_fields)} != "
                f"strings.json fields {sorted(described)}",
            )
            for field_spec in (spec or {}).get("fields", {}).values():
                selector = (field_spec or {}).get("selector", {})
                tkey = (selector.get("select") or {}).get("translation_key")
                if tkey:
                    check(
                        tkey in strings.get("selector", {}),
                        f"selector translation {tkey!r} missing from strings.json",
                    )
    except ImportError:
        notes.append("PyYAML not installed - services.yaml not parsed")

    # ---------------------------------------------------------- quality scale
    scale_path = os.path.join(COMP, "quality_scale.yaml")
    check(os.path.isfile(scale_path), "quality_scale.yaml is missing")
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
                    if value.get("status") != "done":
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
            # `test-coverage: done` is a claim about what CI enforces, not
            # about what a suite happened to reach on someone's laptop. The
            # rule asks for above 95%, so the workflow has to fail below it,
            # and it has to count both suites or the number means nothing.
            coverage_rule = declared.get("test-coverage")
            coverage_status = (
                coverage_rule.get("status")
                if isinstance(coverage_rule, dict)
                else coverage_rule
            )
            if coverage_status == "done":
                workflow = read(ROOT, ".github", "workflows", "tests.yml")
                check(
                    "--cov-fail-under=95" in workflow,
                    "test-coverage is done but the Tests workflow does not gate "
                    "on --cov-fail-under=95",
                )
                check(
                    "--cov-append" in workflow,
                    "test-coverage is done but the gate does not span both "
                    "suites - --cov-append is missing",
                )
        except ImportError:
            notes.append("PyYAML not installed - quality_scale.yaml not parsed")

    # ------------------------------------------------------ icon translations
    # Every translation key an entity uses needs an icon and a name, and every
    # icon and name needs an entity using it. Both forms are matched: the
    # class attribute and the EntityDescription keyword.
    icons = read_json(COMP, "icons.json")
    key_re = re.compile(r'(?:_attr_translation_key\s*=|\btranslation_key=)\s*"([^"]+)"')
    for platform in PLATFORMS:
        source = read(COMP, f"{platform}.py")
        used = set(key_re.findall(source)) - set(EXC_RE.findall(source))
        declared_icons = set(icons.get("entity", {}).get(platform, {}))
        named = set(strings.get("entity", {}).get(platform, {}))
        check(
            used == declared_icons,
            f"{platform}: icons {sorted(declared_icons ^ used)} out of step",
        )
        check(used == named, f"{platform}: names {sorted(named ^ used)} out of step")
    service_icons = set(icons.get("services", {}))
    check(
        service_icons == service_consts,
        f"icons.json services {sorted(service_icons)} != {sorted(service_consts)}",
    )

    # ------------------------------------------------- exception translations
    # Every module, not only services.py: setup raises ConfigEntryNotReady in
    # __init__.py and the coordinator can raise UpdateFailed.
    raised: set[str] = set()
    for f in sorted(os.listdir(COMP)):
        if not f.endswith(".py"):
            continue
        source = read(COMP, f)
        raised |= set(EXC_RE.findall(source))
        for match in RAISE_RE.finditer(source):
            # The arguments run to the matching close paren; a translated
            # raise names its key within them.
            tail = source[match.end() : match.end() + 400]
            check(
                "translation_key=" in tail.split("\n\n", 1)[0],
                f"{f}: {match.group(1)} raised without a translation key near "
                f"offset {match.start()}",
            )
    declared_exc = set(strings.get("exceptions", {}))
    check(
        raised <= declared_exc,
        f"code raises undeclared exception keys {sorted(raised - declared_exc)}",
    )
    check(
        declared_exc <= raised,
        f"strings.json declares unused exceptions {sorted(declared_exc - raised)}",
    )

    # ----------------------------------------------------- issue translations
    issue_keys: set[str] = set()
    for f in sorted(os.listdir(COMP)):
        if f.endswith(".py"):
            issue_keys |= set(ISSUE_RE.findall(read(COMP, f)))
    declared_issues = set(strings.get("issues", {}))
    check(
        issue_keys == declared_issues,
        f"repair issues raised {sorted(issue_keys)} != strings.json issues "
        f"{sorted(declared_issues)}",
    )

    # ------------------------------------------------------------ platforms
    init_src = read(COMP, "__init__.py")
    for platform in PLATFORMS:
        check(
            f"Platform.{platform.upper()}" in init_src,
            f"{platform}.py exists but Platform.{platform.upper()} is not forwarded",
        )
        check(
            "PARALLEL_UPDATES" in read(COMP, f"{platform}.py"),
            f"{platform}.py does not set PARALLEL_UPDATES",
        )
    check(
        "CONFIG_SCHEMA" in init_src,
        "__init__.py has async_setup but no CONFIG_SCHEMA (hassfest)",
    )
    check(
        "async def async_remove_entry" in init_src,
        "__init__.py has no async_remove_entry - the store would outlive the entry",
    )

    # -------------------------------------------------- published tree
    # The manifest is not the only file that ships. A workflow header and a
    # tool docstring are published too, and neither hassfest nor the HACS
    # action reads them.
    for path in published_files():
        text = read(path)
        hits = {m for m in IP_LITERAL_RE.findall(text) if internal_address(m)}
        hits |= {h for url in URL_RE.findall(text) if (h := private_host(url))}
        for hit in sorted(hits):
            failures.append(
                f"{os.path.relpath(path, ROOT)} names {hit!r}, which is on the "
                "development network and means nothing to a user"
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
    print(f"manifest {manifest.get('domain')} {manifest.get('version')}")
    for n in notes:
        print(f"  NOTE   {n}")
    for f in failures:
        print(f"  FAIL   {f}")
    if not failures:
        print("  all offline checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
