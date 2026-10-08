"""The Smithery listing: its quality rules checked offline, and its own fields kept in step.

Smithery scores a server on what tools/list returns and on fields it keeps itself, which a
publish from the MCP URL does not set. `problems` applies the scoring rules to the tools and
the listing text and calls nothing, so CI answers the same way every time. `sync` writes the
listing's fields from the same text and reads them back, for the release job.
"""

from __future__ import annotations

import os
import sys
from http import HTTPStatus
from urllib.parse import quote

import requests

from . import SITE
from .api_text import directory_listing, mcp_spec

API = "https://api.smithery.ai"
NAME = "national-digital/publicdata-au"
ICON = SITE + "/icon-512.png"
UA = "publicdata.au release (+https://publicdata.au/about/)"
# Smithery gives no credit for a shorter description than this.
MIN_DESCRIPTION = 20


def listing() -> dict:
    d = directory_listing()
    return {"displayName": d["name"], "description": d["description"], "homepage": SITE + "/"}


def problems() -> list[str]:
    """Every scoring rule the server can meet.

    Tool naming is left out: Smithery wants dotted names, which the Claude and OpenAI tool APIs
    refuse.
    """
    out = []
    for t in mcp_spec()["tools"]:
        n = t["name"]
        if not t.get("description", "").strip():
            out.append(f"{n}: no description")
        for p, s in (t["inputSchema"].get("properties") or {}).items():
            if not str(s.get("description", "")).strip():
                out.append(f"{n}: parameter {p} has no description")
        if not t.get("outputSchema"):
            out.append(f"{n}: no outputSchema")
        if not t.get("annotations"):
            out.append(f"{n}: no annotations")
    want = listing()
    if len(want["description"].strip()) < MIN_DESCRIPTION:
        out.append(f"listing: the description is under {MIN_DESCRIPTION} characters")
    if not want["displayName"].strip():
        out.append("listing: no display name")
    return out


def _session(key: str) -> requests.Session:
    s = requests.Session()
    # The API refuses a client library's default User-Agent.
    s.headers.update({"Authorization": f"Bearer {key}", "User-Agent": UA})
    return s


def live(s: requests.Session) -> dict:
    r = s.get(f"{API}/servers/{quote(NAME, safe='')}", timeout=30)
    r.raise_for_status()
    got = r.json()
    # Only the search listing carries the homepage.
    r = s.get(
        f"{API}/servers",
        params={"q": NAME, "namespace": NAME.split("/", maxsplit=1)[0]},
        timeout=30,
    )
    r.raise_for_status()
    row = next((x for x in r.json()["servers"] if x["qualifiedName"] == NAME), {})
    return {**got, "homepage": row.get("homepage")}


def drift(current: dict) -> dict:
    return {k: v for k, v in listing().items() if current.get(k) != v}


def sync(key: str) -> list[str]:
    s = _session(key)
    changed = drift(live(s))
    if changed:
        s.patch(
            f"{API}/servers/{quote(NAME, safe='')}", json=changed, timeout=30
        ).raise_for_status()
    now = live(s)
    if not now.get("iconUrl"):
        img = requests.get(ICON, headers={"User-Agent": UA}, timeout=30)
        img.raise_for_status()
        s.put(
            f"{API}/servers/{quote(NAME, safe='')}/icon",
            files={"file": ("icon-512.png", img.content, "image/png")},
            timeout=30,
        ).raise_for_status()
        now = live(s)
    errors = [f"listing: {k} is still not what api.json says" for k in drift(now)]
    if not now.get("iconUrl"):
        errors.append("listing: still has no icon")
    for k in changed:
        print(f"set {k} on {NAME}")
    return errors


def _summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["sync"]:
        errors = problems()
        for e in errors:
            print("Smithery: " + e, file=sys.stderr)
        return 1 if errors else 0
    key = os.environ.get("SMITHERY_API_KEY")
    if not key:
        _summary("SMITHERY_API_KEY is not set, so the Smithery listing's fields were not checked.")
        return 0
    try:
        errors = sync(key)
    except requests.RequestException as e:
        # An outage there is not a fault here and the next release tries again. A 4xx is a
        # fault here, such as a lapsed key or a changed endpoint, so it fails the job.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status is not None and status < HTTPStatus.INTERNAL_SERVER_ERROR:
            print(f"Smithery: {e}", file=sys.stderr)
            _summary(f"The Smithery listing could not be checked: {e}")
            return 1
        print(f"::warning::the Smithery listing could not be checked: {e}")
        _summary(f"The Smithery listing could not be checked: {e}")
        return 0
    for e in errors:
        print("Smithery: " + e, file=sys.stderr)
    _summary(
        "The Smithery listing's fields match api.json."
        if not errors
        else "The Smithery listing's fields do not match api.json: " + "; ".join(errors)
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
