"""Offline circuit identifier.

Pulls history for each circuit, segments it into appliance runs, clusters the
runs by shape, and prints what it found in plain English so a human can name
them. Optionally writes the named result out as a fingerprint library for the
integration to match against.

    HA_URL=https://10.10.52.10:8123 HA_TOKEN=... python tools/identify.py --all
    python tools/identify.py --circuits sensor.circuit_21_power --days 7 --out lib.json

⛔ ONE TRAP WORTH KNOWING ABOUT THIS API.

`/api/history/period/<start>` returns only about 24 hours from <start> unless
`end_time` is supplied. It is the documented default and it fails SOFT: you get
a plausible-looking slice that ends a day after your start, so a "7 day" query
silently answers about last week. Measured on a live install: one entity
returned 60 states without `end_time` and 399 with it.

`end_time` must also be URL-encoded, or the `+00:00` offset is read as a space
and the call 400s.

Both are handled below. Do not remove `end_time`.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

def _load(name: str):
    """Load one integration module by path.

    Deliberately not a package import: `power_fingerprint/__init__.py` pulls in
    Home Assistant, and this tool is meant to run on a workstation that has no
    HA installed. `analysis` and `fingerprint` import nothing from HA precisely
    so this works.
    """
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "custom_components", "power_fingerprint", f"{name}.py",
    )
    spec = importlib.util.spec_from_file_location(f"pf_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pf_{name}"] = mod  # dataclasses need the module resolvable
    spec.loader.exec_module(mod)
    return mod


_analysis = _load("analysis")
_fp = _load("fingerprint")
segment = _analysis.segment
Fingerprint = _fp.Fingerprint
cluster = _fp.cluster
normalize = _fp.normalize
save_library = _fp.save_library
summarize = _fp.summarize

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # self-signed appliance certs are the norm here


def api(path: str) -> object:
    url = os.environ["HA_URL"].rstrip("/") + path
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + os.environ["HA_TOKEN"])
    with urllib.request.urlopen(req, timeout=300, context=CTX) as resp:
        return json.loads(resp.read().decode())


def history(entity: str, days: int) -> list[tuple[datetime, float]]:
    now = datetime.now(timezone.utc)
    start = urllib.parse.quote((now - timedelta(days=days)).isoformat())
    end = urllib.parse.quote(now.isoformat())  # see the module docstring
    data = api(
        f"/api/history/period/{start}?end_time={end}"
        f"&filter_entity_id={urllib.parse.quote(entity)}&minimal_response"
    )
    out = []
    for row in (data[0] if data else []):
        try:
            out.append(
                (datetime.fromisoformat(row["last_changed"]), float(row["state"]))
            )
        except (ValueError, TypeError, KeyError):
            pass
    return out


def power_sensors() -> list[str]:
    return sorted(
        s["entity_id"]
        for s in api("/api/states")
        if s["entity_id"].startswith("sensor.")
        and s.get("attributes", {}).get("device_class") == "power"
        and s.get("attributes", {}).get("state_class") == "measurement"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--circuits", nargs="*", help="entity ids; omit with --all")
    ap.add_argument("--all", action="store_true", help="every power sensor")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-duration", type=float, default=30.0)
    ap.add_argument("--threshold", type=float, default=0.9,
                    help="cluster cut distance; lower splits more")
    ap.add_argument("--out", help="write the fingerprint library here")
    args = ap.parse_args()

    circuits = args.circuits or (power_sensors() if args.all else [])
    if not circuits:
        ap.error("give --circuits or --all")

    # CONTROL: if history comes back empty for everything, the window or the
    # token is wrong and every 'no events' below would be a false negative.
    probe = sum(len(history(c, args.days)) for c in circuits[:3])
    print(f"control: {probe} samples across the first {min(3, len(circuits))} circuits")
    if probe == 0:
        print("ABORT: no history at all - this is UNREAD, not 'nothing ran'.")
        return 2

    library: list[Fingerprint] = []
    for entity in circuits:
        samples = history(entity, args.days)
        events = segment(samples, min_duration_s=args.min_duration)
        name = entity.replace("sensor.", "")
        if not events:
            print(f"\n{name}: no runs in {args.days}d ({len(samples)} samples)")
            continue

        rows = [e.as_features() for e in events]
        labels = cluster(normalize(rows), threshold=args.threshold)
        fps = summarize(entity, rows, labels)

        print(f"\n{name}: {len(events)} runs -> {len(fps)} distinct shape(s)")
        for fp in fps:
            share = 100.0 * fp.count / len(events)
            print(f"   [{fp.label}] {fp.count} runs ({share:.0f}%) - {fp.describe()}")
        library.extend(fps)

    if args.out:
        save_library(library, args.out)
        print(f"\nwrote {len(library)} fingerprints to {args.out}")
        print("Rename each 'unnamed_N' label to the appliance, then feed it back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
