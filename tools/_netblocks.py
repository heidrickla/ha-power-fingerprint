"""The private address ranges tools/validate_local.py refuses.

Kept out of that file so it can scan itself. These CIDRs are dotted quads that
the scan matches, so a file holding them reports itself; this module is the
one published file the scan skips, and it holds nothing else.

Pinned rather than delegated to ipaddress.is_private, whose membership has
changed between Python releases.
"""

from __future__ import annotations

PRIVATE_CIDRS = (
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)
PRIVATE_SUFFIXES = (".local", ".lan", ".internal")
