"""Cloudflare's edge cache.

A dated file is cached as immutable, so a version replaced in R2 is served stale until its
prefix is purged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

import requests

if TYPE_CHECKING:

    class Session(Protocol):
        def get(
            self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: int
        ) -> requests.Response: ...

        def post(
            self, url: str, *, json: dict[str, list[str]], headers: dict[str, str], timeout: int
        ) -> requests.Response: ...


API = "https://api.cloudflare.com/client/v4"
HOST = "publicdata.au"
BATCH = 30  # prefixes per purge request
VERSION = re.compile(r"^d/([a-z0-9][a-z0-9-]*)/v/(\d{4}-\d{2}-\d{2})/$")


def with_answers(prefixes: list[str]) -> list[str]:
    """Each d/<slug>/v/<date>/ prefix and the query API answers of that version, which the API
    caches for a year as well."""
    out = []
    for p in prefixes:
        out.append(p)
        if m := VERSION.match(p):
            out.append(f"api/v1/datasets/{m[1]}/versions/{m[2]}/")
    return out


def purge(prefixes: list[str], token: str, host: str = HOST, session: Session = requests) -> int:
    """Purges each d/<slug>/v/<date>/ prefix on host. Returns the number of prefixes purged."""
    auth = {"Authorization": f"Bearer {token}"}
    r = session.get(f"{API}/zones", params={"name": host}, headers=auth, timeout=30)
    r.raise_for_status()
    zones = r.json().get("result") or []
    if not zones:
        msg = f"no zone named {host} for this token"
        raise RuntimeError(msg)
    url = f"{API}/zones/{zones[0]['id']}/purge_cache"
    full = [f"{host}/{p.lstrip('/')}" for p in prefixes]
    for i in range(0, len(full), BATCH):
        r = session.post(url, json={"prefixes": full[i : i + BATCH]}, headers=auth, timeout=30)
        body = r.json()
        if not r.ok or not body.get("success"):
            msg = f"purge refused: {body.get('errors') or r.status_code}"
            raise RuntimeError(msg)
    return len(full)
