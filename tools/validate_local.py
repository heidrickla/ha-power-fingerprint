"""Local stand-in for the checks CI would run.

hassfest and the HACS action run in GitHub Actions; this approximates the
parts of them that can be checked with no network at all, plus the cross-file
consistency that nothing else checks: translation keys against icons,
exceptions raised against exceptions declared, actions registered against
actions described, versions against each other, the quality scale against the
pinned rule list. Run it before a push so the push is not the first
verification.

    python tools/validate_local.py

It also refuses a development host anywhere in the published tree. The file
list comes from `git ls-files --cached --others --exclude-standard`, not a
directory walk, so an ignored path stays out and a file staged for this commit
is read. Addresses, URLs and internal-suffix names are caught from the tree
alone. A bare host named in prose has to be named somewhere, and naming it in
this file would publish it, so the names are read from outside the tree: the
`PF_INTERNAL_HOSTS` environment variable, the gitignored `.internal-hosts`
file, and the hosts of the configured git remotes.

A CI checkout supplies none of the three: the environment variable comes from a
repository secret, `.internal-hosts` is gitignored so a clone never has it, and
the only remote is the code host, whose name parts are all generic. Under `CI`
an empty name list is a failure, because the run that guards the public push is
the one that cannot derive a name. Off CI it is a note, and the address, URL and
internal-suffix rules apply either way.

The same rules run over every commit the tracking branch does not have, both
the files each one changes and its message. A push carries the history, and a
name the newest commit removes is still in the one before it, where nothing
that reads the working tree can see it. `PF_PUSH_RANGE` names the range
directly; without a tracking branch there is nothing queued to read and the
run says so.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import subprocess
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
# Host name parts that name no particular machine, so a remote URL does not
# turn the forge or the code host into a forbidden word.
GENERIC_HOST_PARTS = frozenset(
    {
        "com",
        "dev",
        "git",
        "gitea",
        "github",
        "gitlab",
        "home",
        "http",
        "https",
        "internal",
        "io",
        "lan",
        "local",
        "localhost",
        "net",
        "org",
        "ssh",
        "www",
    }
)
# Where the development host names are read from, none of them in the tree.
INTERNAL_HOSTS_ENV = "PF_INTERNAL_HOSTS"
INTERNAL_HOSTS_FILE = os.path.join(ROOT, ".internal-hosts")
# The commits the range scan reads. Unset, it is the tracking branch to HEAD.
PUSH_RANGE_ENV = "PF_PUSH_RANGE"
# A local absolute path in a commit message or a commit's files names the
# machine it was written on and reaches no reader.
LOCAL_PATH_RE = re.compile(r"(?<![\w:])[A-Za-z]:[\\/][\\/]?[A-Za-z0-9_.\-]")

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


def host_tokens(url: str) -> set[str]:
    """The machine-naming parts of a git remote URL's host.

    `urlsplit` returns no host for the scp-style `git@host:path`, so that form
    is split by hand. A trailing digit is dropped as well as kept, so one
    remote named after a numbered box also covers its siblings.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        head = url.split(":", 1)[0]
        host = head.split("@")[-1] if "@" in head else ""
    tokens: set[str] = set()
    for part in re.split(r"[.\-_]", host):
        part = part.lower()
        if not part or part in GENERIC_HOST_PARTS or part.isdigit():
            continue
        tokens.add(part)
        stripped = part.rstrip("0123456789")
        if len(stripped) >= 4:
            tokens.add(stripped)
    return tokens


def in_ci() -> bool:
    """Whether this run is a CI job. GitHub Actions and the CI runner both set CI."""
    return os.environ.get("CI", "").strip().lower() not in {"", "0", "false"}


def internal_names() -> list[str]:
    """Development host names, read from outside the published tree.

    Naming them in this file would publish them, which is the disclosure the
    rule exists to prevent.
    """
    names: set[str] = set()
    for raw in re.split(r"[,\s]+", os.environ.get(INTERNAL_HOSTS_ENV, "")):
        if raw:
            names.add(raw.strip().lower())
    if os.path.exists(INTERNAL_HOSTS_FILE):
        for line in read(INTERNAL_HOSTS_FILE).splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                names.add(line)
    try:
        remotes = subprocess.run(
            ["git", "config", "--get-regexp", r"^remote\..*\.url$"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        remotes = None
    if remotes is not None and remotes.returncode == 0:
        for line in remotes.stdout.splitlines():
            _, _, url = line.partition(" ")
            names |= host_tokens(url.strip())
    return sorted(n for n in names if len(n) >= 4)


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


def git_text(*args: str) -> str | None:
    """A git command's stdout, or None when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def check_version_is_released(version: str) -> None:
    """Refuse a version string that is the last release with commits after it.

    The three version strings are checked against each other elsewhere, which
    passes when all three are stale together: manifest.json, const.VERSION and
    pyproject.toml all said 0.17.0 with eleven commits sitting on top of tag
    v0.17.0. An install from that tree reports a version that never carried
    those commits, and HACS reads the same string.

    A tag with no commits after it is the released state and passes. A tree
    with no tags, or no git, cannot answer, and says so rather than passing
    quietly.
    """
    tag = git_text("describe", "--tags", "--abbrev=0")
    if not tag:
        notes.append(
            "no reachable git tag, so the version could not be compared "
            "against the last release"
        )
        return
    ahead = git_text("rev-list", "--count", f"{tag}..HEAD")
    if ahead is None or not ahead.isdigit():
        notes.append(f"git could not count the commits after {tag}")
        return
    count = int(ahead)
    check(
        not (count and version == tag.lstrip("v")),
        f"version {version} is tag {tag} with {count} commits after it - "
        "manifest.json, const.VERSION and pyproject.toml agree with each "
        "other and with the last release, so an install reports a version "
        "that never shipped these commits",
    )


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


REPO_ALLOWED_HOSTS: frozenset[str] = frozenset()


# ------------------------------------------------- development-host refusal
# hassfest and the HACS action read the manifest and nothing else, so a
# development address anywhere in the tree - a workflow comment, a README, a
# docstring - ships with every check green. Two rules, one strict and one
# narrower: the manifest URLs are refused for anything a user cannot open,
# and every published file is refused for anything that names this network.
import subprocess as _subprocess
from urllib.parse import urlsplit as _urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _netblocks

MANIFEST_NETS = tuple(
    ipaddress.ip_network(c)
    for c in _netblocks.TREE_CIDRS + _netblocks.MANIFEST_ONLY_CIDRS
)
TREE_NETS = tuple(ipaddress.ip_network(c) for c in _netblocks.TREE_CIDRS)
MANIFEST_ONLY_NAMES = ("localhost",)
PRIVATE_SUFFIXES = _netblocks.PRIVATE_SUFFIXES

# Hosts that look like a development host and are not one. Every entry is
# load-bearing in this repository and carries the reason on its line; an
# entry added without one is how the rule stops working.
ALLOWED_HOSTS = frozenset(
    {
        # The default Home Assistant address, in every install document.
        "homeassistant.local",
    }
    | REPO_ALLOWED_HOSTS
)

# Text that ships to whoever clones or installs the repository. The file list
# comes from git rather than a walk: git already knows what is ignored, which
# is how private operational notes under an ignored directory stay out, and
# --others adds a file staged for this commit but not yet added.
PUBLISHED_SUFFIXES = {
    ".cfg",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PUBLISHED_NAMES = {
    ".gitattributes",
    ".gitignore",
    "CODEOWNERS",
    "LICENSE",
    "LICENSE-APACHE",
    "NOTICE",
}
# The one published file the scan skips: it holds the CIDRs the scan matches
# on, so it would report itself. Nothing else may live in it.
SCAN_EXEMPT = ("tools/_netblocks.py",)

IP_LITERAL_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# A candidate IPv6 literal, not a validator: `blocked_address` parses each
# match with `ipaddress` and drops what will not parse. Three colon-separated
# groups is the floor, which leaves a clock time and a two-group MAC prefix
# out. The two IPv6 CIDRs in `_netblocks.py` had no path to a match before
# this: IP_LITERAL_RE matches dotted quads only, and URL_RE stopped at the
# opening bracket of a bracketed host.
IPV6_LITERAL_RE = re.compile(
    r"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:.])"
)
# The bracketed-host branch exists so `http://[2001:db8::1]:8123/` reaches
# `urlsplit` whole. Without it the match stopped at `[`, `urlsplit` raised on
# the unbalanced bracket and the loop swallowed it.
URL_RE = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://(?:\[[0-9A-Fa-f:.]+\])?[^\s\"'`<>)\]},]*"
)
# A host written in prose with no scheme. The suffix must end the name:
# \b would match the "home" of home-assistant.io.
BARE_HOST_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+"
    r"(?:corp|home|home\.arpa|intranet|internal|lan|local|localdomain)(?![\w-])",
    re.IGNORECASE,
)
# A host made of anything else is a template - f"http://{host}/" - not a host.
HOST_CHARS_RE = re.compile(r"^[a-z0-9.\-\[\]:]+$", re.IGNORECASE)


def is_netmask(text: str) -> bool:
    """A dotted quad written as a contiguous subnet mask, 255.255.255.0 and up.

    Every such mask sits in the top reserved block and would otherwise be
    refused as a reserved address. The all-zero mask is a mask too, and is
    deliberately not exempt: as a host it is the unspecified address.
    """
    if not text.startswith("255."):
        return False
    try:
        value = int(ipaddress.IPv4Address(text))
    except ipaddress.AddressValueError:
        return False
    inverted = (~value) & 0xFFFFFFFF
    return inverted & (inverted + 1) == 0


def blocked_address(text: str, nets: tuple[Any, ...]) -> bool:
    """Whether text is an address literal inside one of nets."""
    if is_netmask(text):
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(address in net for net in nets)


def blocked_host(host: str, nets: tuple[Any, ...], names: tuple[str, ...] = ()) -> bool:
    """Whether a hostname resolves or routes inside one network only.

    `names` are hosts refused by spelling rather than by address family. Only
    the manifest rule passes any: "localhost" names no machine on this
    network, so it is a dead documentation link but not a disclosure.
    """
    host = host.strip().rstrip(".").lower()
    if not host or host in ALLOWED_HOSTS:
        return False
    if host in names:
        return True
    if blocked_address(host, nets):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A literal outside nets is a public address, whatever its shape.
        return False
    if host.endswith(PRIVATE_SUFFIXES):
        return True
    # A name with no dot is resolved against whatever search domain the reader
    # happens to have, so it names a machine on a LAN rather than on the net.
    return "." not in host


def unreachable_host(url: str) -> str | None:
    """The host of a manifest URL no user outside this network can open."""
    if not isinstance(url, str) or not url:
        return None
    try:
        host = _urlsplit(url).hostname or ""
    except ValueError:
        return None
    if host and not HOST_CHARS_RE.match(host):
        return None
    return host if blocked_host(host, MANIFEST_NETS, MANIFEST_ONLY_NAMES) else None


def malformed_url(url: Any) -> bool:
    """A manifest URL that is not an absolute http(s) URL with a host.

    unreachable_host cannot report this: its answer is a host or None, and
    "not-a-url" has no host to report.
    """
    if not isinstance(url, str) or not url:
        return True
    try:
        parts = _urlsplit(url)
    except ValueError:
        return True
    return parts.scheme not in {"http", "https"} or not parts.hostname


def published_files() -> list[str]:
    """Every text file that ships, relative to ROOT, from git's own index.

    Falls back to a walk when git is not there - an extracted tarball - so the
    rule still runs, and says so, rather than passing on an empty list.
    """
    paths: list[str] = []
    try:
        listing = _subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, _subprocess.SubprocessError:
        listing = None
    if listing is not None and listing.returncode == 0:
        paths = [p for p in listing.stdout.split("\0") if p]
    else:
        notes.append("git not available - the tree scan walked the directory instead")
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [
                d
                for d in dirs
                if d not in {"__pycache__", "venv", "htmlcov", "node_modules"}
                and not (d.startswith(".") and d not in {".gitea", ".github"})
            ]
            for f in files:
                paths.append(
                    os.path.relpath(os.path.join(dirpath, f), ROOT).replace("\\", "/")
                )
    return sorted(p for p in paths if published_name(p))


def published_name(path: str) -> bool:
    """Whether a repository-relative path is one of the text files that ship."""
    if path in SCAN_EXEMPT:
        return False
    name = path.rsplit("/", 1)[-1]
    suffix = os.path.splitext(name)[1].lower()
    return suffix in PUBLISHED_SUFFIXES or name in PUBLISHED_NAMES


def is_network_address(literal: str, tail: str) -> bool:
    """Whether literal is a CIDR's own network address, as in 10.0.0.0/8.

    A CIDR names a network and no machine, which is why `_netblocks.py` can
    list the refused ranges at all. The host bits have to be zero: a literal
    with a host part, written with a prefix length after it, is still one
    machine's address.
    """
    prefix = re.match(r"/(\d{1,3})(?!\d)", tail)
    if not prefix:
        return False
    try:
        ipaddress.ip_network(f"{literal}/{prefix.group(1)}", strict=True)
    except ValueError:
        return False
    return True


def tree_hits(text: str, name_re: Any = None) -> list[tuple[int, str]]:
    """Every development host named in text, as (line number, host)."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if name_re is not None:
            for name in name_re.findall(line):
                if name.lower() not in ALLOWED_HOSTS:
                    hits.append((number, name.lower()))
        for pattern in (IP_LITERAL_RE, IPV6_LITERAL_RE):
            for match in pattern.finditer(line):
                literal = match.group(0).lower()
                if literal in ALLOWED_HOSTS:
                    continue
                if is_network_address(literal, line[match.end() :]):
                    continue
                if blocked_address(literal, TREE_NETS):
                    hits.append((number, literal))
        for url in URL_RE.findall(line):
            try:
                host = _urlsplit(url).hostname or ""
            except ValueError:
                continue
            if not host or not HOST_CHARS_RE.match(host):
                continue
            if blocked_host(host, TREE_NETS):
                hits.append((number, host))
        for name in BARE_HOST_RE.findall(line):
            if name.lower() not in ALLOWED_HOSTS:
                hits.append((number, name.lower()))
    # A bare literal inside a URL matches two branches and would be reported
    # twice on the same line. First occurrence wins, so the order stays the
    # order the line reads in.
    return list(dict.fromkeys(hits))


def name_pattern() -> Any:
    """The development-host pattern, or None when no name was supplied.

    The names are read from outside the tree, because naming them in a
    published file is the disclosure the rule exists to prevent.

    NO LEFT WORD BOUNDARY. `\b` before the name failed on the one occurrence
    that mattered: inside a regex source the name sat after the `b` of `\b`,
    which is a word character, so the boundary did not hold and the scan read
    clean over a line that named a host. A name this long is a disclosure
    wherever it appears, including inside a longer word.
    """
    finder = globals().get("internal_names")
    if not callable(finder):
        return None
    names = finder()
    if names:
        notes.append(f"{len(names)} development host names given to the scans")
        return re.compile(
            r"(?:" + "|".join(re.escape(n) for n in names) + r")\w*",
            re.IGNORECASE,
        )
    if in_ci():
        failures.append(
            "no development host names given to the scans, so the bare-name "
            f"rule matched nothing - set {INTERNAL_HOSTS_ENV} from a "
            "repository secret, or this job passes on a tree that names a "
            "development host in prose"
        )
    else:
        notes.append("no development host names given to the scans")
    return None


def git_out(*args: str) -> str | None:
    """stdout of a git command, or None when git or the command fails."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout if done.returncode == 0 else None


def queued_range() -> str | None:
    """The commits on HEAD that the tracking branch does not have."""
    override = os.environ.get(PUSH_RANGE_ENV, "").strip()
    if override:
        return override
    upstream = git_out(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    if not upstream or not upstream.strip():
        return None
    return f"{upstream.strip()}..HEAD"


def scan_queued_commits(name_re: Any) -> None:
    """Refuse a development host in a commit that has not been pushed.

    THE WORKING TREE IS NOT THE PUSH. `scan_published_tree` reads what
    `git ls-files` lists, so a name removed by the newest commit is gone from
    its view while every earlier commit still carries it, and a push carries
    all of them. Commit messages are published the same way and are checked
    here too; nothing else in this file reads one.

    A shallow CI checkout has no tracking branch and nothing queued, so the
    range cannot be derived there. That case states what it did not read.
    """
    commit_range = queued_range()
    if commit_range is None:
        notes.append(
            "no tracking branch, so no commit range was read - set "
            f"{PUSH_RANGE_ENV} to scan one"
        )
        return
    listing = git_out("rev-list", "--reverse", commit_range)
    if listing is None:
        failures.append(f"{commit_range} is not a range this repository can resolve")
        return
    commits = listing.split()
    notes.append(f"{len(commits)} commits queued for push in {commit_range}")
    if not commits:
        return

    def report(where: str, number: int, host: str) -> None:
        failures.append(
            f"{where}:{number} names {host} - that commit has not been pushed "
            "and carries a name that must not leave this network"
        )

    for sha in commits:
        short = sha[:9]
        message = git_out("log", "-1", "--format=%B", sha) or ""
        for number, host in tree_hits(message, name_re):
            report(f"{short} (message)", number, host)
        for number, line in enumerate(message.splitlines(), 1):
            if LOCAL_PATH_RE.search(line):
                report(f"{short} (message)", number, "a local absolute path")
        changed = git_out("diff-tree", "-r", "--no-commit-id", "--name-only", sha)
        for path in (changed or "").split("\n"):
            path = path.strip()
            if not path or not published_name(path):
                continue
            blob = git_out("show", f"{sha}:{path}")
            if blob is None:
                continue
            for number, host in tree_hits(blob, name_re):
                report(f"{short}:{path}", number, host)


def scan_published_tree(name_re: Any) -> None:
    """Refuse a development host anywhere in the published tree."""
    exempt = os.path.join(ROOT, *SCAN_EXEMPT[0].split("/"))
    if os.path.isfile(exempt):
        allowed_names = {"TREE_CIDRS", "MANIFEST_ONLY_CIDRS", "PRIVATE_SUFFIXES"}
        for node in ast.parse(read(exempt)).body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else []
            if not all(
                isinstance(t, ast.Name) and t.id in allowed_names for t in targets
            ):
                failures.append(
                    f"{SCAN_EXEMPT[0]} holds more than the pinned address space; "
                    "the tree scan skips this file, so nothing else may live in it"
                )
                break
    seen = 0
    for path in published_files():
        full = os.path.join(ROOT, *path.split("/"))
        if not os.path.isfile(full):
            continue
        try:
            text = read(full)
        except OSError, UnicodeDecodeError:
            continue
        seen += 1
        for number, host in tree_hits(text, name_re):
            failures.append(
                f"{path}:{number} names {host} - that host is on the development "
                "network and means nothing to a user who installs this"
            )
    check(seen > 0, "the published-tree scan read no files, so it proved nothing")


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
        host = unreachable_host(str(manifest.get(url_key, "")))
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
    check_version_is_released(str(manifest.get("version", "")))

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
            # `name` and `description` are legible in both files and Home
            # Assistant reads both: `async_get_all_descriptions` returns the
            # services.yaml text while the `services` translation category
            # returns the strings.json text. Two copies drift, and comparing
            # field key sets above does not see it. strings.json is the one
            # source; services.yaml carries selectors, defaults and `required`.
            inline = [
                f"{name}.{key}"
                for key in ("name", "description")
                if key in (spec or {})
            ]
            inline += [
                f"{name}.fields.{field}.{key}"
                for field, field_spec in (spec or {}).get("fields", {}).items()
                for key in ("name", "description")
                if key in (field_spec or {})
            ]
            check(
                not inline,
                f"services.yaml carries text that belongs in strings.json: "
                f"{sorted(inline)}",
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

    # ---------------------------------------------- development-host refusal
    for _key in ("documentation", "issue_tracker"):
        if _key in manifest:
            check(
                not malformed_url(manifest.get(_key)),
                f"manifest {_key} is not an absolute http(s) URL a user can open",
            )
    host_names = name_pattern()
    scan_published_tree(host_names)
    scan_queued_commits(host_names)

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
