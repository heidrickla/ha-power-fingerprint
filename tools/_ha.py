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

import importlib.util
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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
        "..", "custom_components", "power_fingerprint", f"{name}.py",
    )
    spec = importlib.util.spec_from_file_location(f"pf_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pf_{name}"] = mod  # dataclasses need the module resolvable
    spec.loader.exec_module(mod)
    return mod


def api(path: str):
    url = os.environ["HA_URL"].rstrip("/") + path
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + os.environ["HA_TOKEN"])
    with urllib.request.urlopen(req, timeout=300, context=CTX) as resp:
        return json.loads(resp.read().decode())


def window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now


def history(entity: str, days: int) -> list[Sample]:
    start, end = window(days)
    data = api(
        f"/api/history/period/{urllib.parse.quote(start.isoformat())}"
        f"?end_time={urllib.parse.quote(end.isoformat())}"  # see module docstring
        f"&filter_entity_id={urllib.parse.quote(entity)}&minimal_response"
    )
    out: list[Sample] = []
    for row in (data[0] if data else []):
        try:
            out.append(
                (datetime.fromisoformat(row["last_changed"]), float(row["state"]))
            )
        except (ValueError, TypeError, KeyError):
            pass
    return out


def power_sensors(exclude: str | None = None) -> list[str]:
    """Every measurement-class power sensor, optionally excluding a substring."""
    return sorted(
        s["entity_id"]
        for s in api("/api/states")
        if s["entity_id"].startswith("sensor.")
        and s.get("attributes", {}).get("device_class") == "power"
        and s.get("attributes", {}).get("state_class") == "measurement"
        and (exclude is None or exclude not in s["entity_id"])
    )
