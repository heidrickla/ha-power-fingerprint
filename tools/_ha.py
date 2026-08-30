"""Shared Home Assistant REST client for the offline tools.

⛔ THE `end_time` TRAP LIVES HERE AND NOWHERE ELSE.

`/api/history/period/<start>` returns only about 24 hours from <start> unless
`end_time` is supplied. That is the documented default and it fails SOFT: you
get a plausible-looking slice ending a day after your start, so a "7 day" query
silently answers about last week. Measured on a live install: one entity
returned 60 states without `end_time` and 399 with it.

`end_time` must also be URL-encoded, or the `+00:00` offset is read as a space
and the call 400s.

Do not inline history fetching anywhere else - this is the single copy so the
trap cannot come back one file at a time.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

Sample = tuple[datetime, float]

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # self-signed appliance certs are the norm here


def load_module(name: str):
    """Load one HA-free integration module by path.

    Not a package import: `power_fingerprint/__init__.py` pulls in Home
    Assistant, and these tools run on a workstation without it. `analysis`,
    `fingerprint` and `attribution` import nothing from HA precisely so this
    works.
    """
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "power_fingerprint",
        f"{name}.py",
    )
    spec = importlib.util.spec_from_file_location(f"pf_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pf_{name}"] = mod  # dataclasses need the module resolvable
    spec.loader.exec_module(mod)
    return mod


class _Lazy:
    """Defers loading `analysis` until something needs it.

    Loading at import time would make every tool that only wants `api()` pay
    for it, and would make an unrelated syntax error in `analysis` break the
    tools that do not touch it.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._mod = None

    def __getattr__(self, attr: str):
        if self._mod is None:
            self._mod = load_module(self._name)
        return getattr(self._mod, attr)


_analysis = _Lazy("analysis")


def api(path: str):
    url = os.environ["HA_URL"].rstrip("/") + path
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + os.environ["HA_TOKEN"])
    with urllib.request.urlopen(req, timeout=300, context=CTX) as resp:
        return json.loads(resp.read().decode())


def window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(days=days), now


def history(entity: str, days: int) -> list[Sample]:
    start, end = window(days)
    data = api(
        f"/api/history/period/{urllib.parse.quote(start.isoformat())}"
        f"?end_time={urllib.parse.quote(end.isoformat())}"  # see module docstring
        f"&filter_entity_id={urllib.parse.quote(entity)}&minimal_response"
    )
    out: list[Sample] = []
    for row in data[0] if data else []:
        # Rows with a non-numeric state (unavailable, unknown) are skipped
        # rather than defaulted - a fabricated zero would look like a real
        # reading of nothing.
        with contextlib.suppress(ValueError, TypeError, KeyError):
            # Converted to watts here, at the boundary, exactly as the
            # integration does - so a kW meter does not quietly produce
            # thresholds a thousand times too high and report zero runs.
            watts = _analysis.to_watts(float(row["state"]), unit(entity))
            if watts is not None:
                out.append((datetime.fromisoformat(row["last_changed"]), watts))
    return out


# Units seen while listing sensors, so `history` can convert without a second
# round trip. `minimal_response` strips attributes from history rows, which is
# most of why history is fast, so the unit has to come from the state list.
_UNITS: dict[str, str | None] = {}


def power_sensors(exclude: str | None = None) -> list[str]:
    """Every measurement-class power sensor, optionally excluding a substring."""
    out = []
    for s in api("/api/states"):
        attrs = s.get("attributes", {})
        if (
            s["entity_id"].startswith("sensor.")
            and attrs.get("device_class") == "power"
            and attrs.get("state_class") == "measurement"
        ):
            _UNITS[s["entity_id"]] = attrs.get("unit_of_measurement")
            if exclude is None or exclude not in s["entity_id"]:
                out.append(s["entity_id"])
    return sorted(out)


def unit(entity: str) -> str | None:
    """The unit an entity reports in, if `power_sensors` has been called.

    None also means "not seen yet", which `to_watts` treats as watts. That is
    the same forgiving default the integration uses, and for the same reason:
    the units that actually differ - kW against W - are always declared.
    """
    return _UNITS.get(entity)


def template(tpl: str) -> str:
    """Render a Jinja template server-side. Used to read area assignments,
    which are registry data and not exposed as plain states."""
    url = os.environ["HA_URL"].rstrip("/") + "/api/template"
    req = urllib.request.Request(
        url, data=json.dumps({"template": tpl}).encode(), method="POST"
    )
    req.add_header("Authorization", "Bearer " + os.environ["HA_TOKEN"])
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60, context=CTX) as resp:
        return resp.read().decode().strip()
