"""Checks an Agentic Resource Discovery catalogue against the spec's schema and conformance rules.

The rules follow the conformance tool Lighthouse runs, at
https://github.com/GoogleChrome/lighthouse/blob/main/third-party/ard/ard.js, and the schema at
https://github.com/ards-project/ard-spec/blob/main/spec/schemas/ai-catalog.schema.json. Lighthouse
holds the entries of the manifest it finds to the discovery types and does not read the catalogues
they hold. A nested catalogue lists what a bundle holds, such as a dataset's files, under their own
media types, as the spec's own example bundle does.
"""

import re

CATALOGUE = "application/ai-catalog+json"
SKILL = 'text/markdown; profile="urn:air:agent-skills"'
DISCOVERY_TYPES = (
    CATALOGUE,
    "application/agent-card+json",
    "application/a2a-agent-card+json",
    "application/mcp-server-card+json",
    "application/agent-skills+zip",
    "application/agent-skills+gzip",
    SKILL,
    "application/ai-registry",
    "application/ai-registry+json",
)
URN = re.compile(r"^urn:air:([a-zA-Z0-9.-]+)(?::([a-zA-Z0-9._:-]+))?:([a-zA-Z0-9._-]+)$")
ROOT_KEYS = {"specVersion", "host", "entries"}
HOST_KEYS = {"displayName", "identifier", "documentationUrl", "logoUrl", "trustManifest"}
ENTRY_KEYS = {
    "identifier",
    "displayName",
    "type",
    "url",
    "data",
    "description",
    "tags",
    "capabilities",
    "representativeQueries",
    "version",
    "updatedAt",
    "metadata",
    "trustManifest",
}


def problems(doc: dict, top: bool = True, where: str = "catalogue") -> list[str]:
    """Every way doc breaks the schema or a conformance rule, the rule's warnings included, since
    Lighthouse marks the audit down for a warning. A bundle given inline is checked as a nested
    catalogue."""
    out = []
    if not isinstance(doc, dict):
        return [f"{where}: not a JSON object"]
    if extra := set(doc) - ROOT_KEYS:
        out.append(f"{where}: unknown root keys {sorted(extra)}")
    if doc.get("specVersion") != "1.0":
        out.append(f"{where}: specVersion is {doc.get('specVersion')!r}, expected '1.0'")
    host = doc.get("host")
    if host is not None:
        if not isinstance(host, dict) or "displayName" not in host:
            out.append(f"{where}: host needs a displayName")
        elif extra := set(host) - HOST_KEYS:
            out.append(f"{where}: unknown host keys {sorted(extra)}")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return out + [f"{where}: entries is not an array"]
    seen = set()
    for i, e in enumerate(entries):
        label = f"{where}: {e.get('identifier') or f'entry {i}'}"
        for k in ("identifier", "displayName", "type"):
            if not isinstance(e.get(k), str) or not e.get(k):
                out.append(f"{label}: missing {k}")
        if extra := set(e) - ENTRY_KEYS:
            out.append(f"{label}: unknown keys {sorted(extra)}")
        ident = e.get("identifier", "")
        if ident and not URN.match(ident):
            out.append(f"{label}: identifier is not urn:air:<publisher>:<namespace>:<name>")
        if ident in seen:
            out.append(f"{label}: identifier repeated")
        seen.add(ident)
        if ("url" in e) == ("data" in e):
            out.append(f"{label}: needs exactly one of url and data")
        if top and e.get("type") not in DISCOVERY_TYPES:
            out.append(f"{label}: type {e.get('type')!r} is not a discovery type")
        q = e.get("representativeQueries")
        if q is None:
            if top:
                out.append(f"{label}: no representativeQueries")
        elif (
            not isinstance(q, list)
            or not 2 <= len(q) <= 5
            or not all(isinstance(x, str) for x in q)
        ):
            out.append(f"{label}: representativeQueries must be 2 to 5 strings")
        for k in ("tags", "capabilities"):
            if k in e and not (isinstance(e[k], list) and all(isinstance(x, str) for x in e[k])):
                out.append(f"{label}: {k} must be an array of strings")
        if "data" in e and e.get("type") == CATALOGUE:
            out += problems(e["data"], top=False, where=label)
    return out
