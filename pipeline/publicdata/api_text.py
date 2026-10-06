"""The query API and agent tool prose from api.json, filled and rendered for each output."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from functools import cache
from pathlib import Path

from . import OPERATOR, REPO, SITE
from .provenance import OPERATOR_URL

_PH = re.compile(r"\{([a-z_]+)\}")
_CODE = re.compile(r"`([^`]+)`")


ICONS = [
    {"src": SITE + "/favicon.svg", "mimeType": "image/svg+xml", "sizes": ["any"]},
    {"src": SITE + "/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]},
]
REPOSITORY = {"url": REPO, "source": "github"}


def _num(v) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


def fill(v, values: dict):
    """Fills {name} placeholders that values knows, anywhere in a nested value."""
    if isinstance(v, str):
        return _PH.sub(lambda m: _num(values[m[1]]) if m[1] in values else m[0], v)
    if isinstance(v, dict):
        return {k: fill(x, values) for k, x in v.items()}
    if isinstance(v, list):
        return [fill(x, values) for x in v]
    return v


@cache
def spec() -> dict:
    raw = json.loads((Path(__file__).parent / "api.json").read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return fill(raw, raw["limits"])


def plain(v):
    """Drops code marks, for tool schemas and anywhere Markdown is not read."""
    if isinstance(v, str):
        return _CODE.sub(r"\1", v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def as_html(s: str, cls: str = "") -> str:
    open_ = f'<span class="{cls}">' if cls else "<code>"
    close = "</span>" if cls else "</code>"
    return _CODE.sub(lambda m: open_ + m[1] + close, html.escape(s, quote=False))


def operator_list() -> str:
    """The operators as one Markdown sentence."""
    return "Operators: " + ", ".join(f"`{o['syntax']}`" for o in spec()["operators"].values()) + "."


def filter_help() -> str:
    return spec()["api"]["filters"] + " " + operator_list()


def param_text(name: str) -> str:
    p = spec()["parameters"][name]
    d = p["description"]
    return f"On `{p['on']}`, {d[0].lower()}{d[1:]}" if p.get("on") else d


def query_params() -> list[str]:
    """The reserved query-string parameters, in the order the spec gives them."""
    return [n for n, p in spec()["parameters"].items() if p.get("in", "query") == "query"]


def tool_names() -> list[str]:
    return list(spec()["webmcp"]["tools"])


def browser_spec() -> dict:
    """What site.js needs: operator labels, parameter text and the tools, without code marks."""
    s = spec()
    return plain(
        {
            "operators": {
                k: {
                    "label": o["label"],
                    **({"not_label": o["not_label"]} if "not_label" in o else {}),
                }
                for k, o in s["operators"].items()
            },
            "parameters": {k: {"description": param_text(k)} for k in s["parameters"]},
            "webmcp": s["webmcp"],
        }
    )


# Each dataset's fields are written for the model to read before it queries.
RESOURCE_ANNOTATIONS = {"audience": ["assistant"], "priority": 0.8}


def release() -> str:
    """The site's semantic version. The deploy sets it from the last tag and the merged PR's
    title (scripts/next-version.mjs); anywhere else it is a development build."""
    return os.environ.get("PUBLICDATA_RELEASE") or "0.0.0-dev"


def resource_text(title: str, publisher: str) -> str:
    return (
        plain(spec()["mcp"]["resource"]).replace("{title}", title).replace("{publisher}", publisher)
    )


def _input_schema(t: dict, version: str | None = None) -> dict:
    """A tool's input schema for any dataset. site.js builds the same, then narrows it on a dataset page."""
    s = spec()
    w = s["webmcp"]
    version = version or w["version"]
    props = {}
    for k, p in t["input"].items():
        if p.get("api") == "filters":
            props[k] = {"type": "object", "description": w["where"], "additionalProperties": True}
            continue
        o = {x: v for x, v in p.items() if x != "api"}
        if "description" not in o and p.get("api") == "version":
            o["description"] = version
        elif "description" not in o and p.get("api"):
            o["description"] = param_text(p["api"])
        props[k] = o
    return {
        "type": "object",
        "properties": props,
        "required": list(t["required"]),
        "additionalProperties": False,
    }


def mcp_spec() -> dict:
    """What the MCP server at /mcp answers initialize and tools/list with."""
    s = spec()
    m = s["mcp"]
    tools = []
    for name, t in s["webmcp"]["tools"].items():
        # The server's row tools read Parquet, so what they say about versions differs from the pages'.
        description = t["description"]
        for old, new in m["tool_text"].items():
            description = description.replace(old, new)
        d = {
            "name": name,
            "title": t["title"],
            "description": description,
            "inputSchema": _input_schema(t, m["version"]),
            "outputSchema": t["output"],
            "annotations": {"title": t["title"], **t["annotations"]},
        }
        # The row tools run the query and then count the match, so each call is two queries.
        d["queries"] = 2 if t.get("api") else 0
        tools.append(d)
    return plain(
        {
            "_comment": "Written from pipeline/publicdata/api.json by python -m publicdata.api_text. Edit that, not this.",
            "server": {
                "name": m["name"],
                "title": m["title"],
                "version": release(),
                "description": m["registry_description"],
                "websiteUrl": SITE + "/agents/#mcp",
                "icons": ICONS,
            },
            "protocol_versions": m["protocol_versions"],
            "instructions": m["instructions"],
            "limits": {k: s["limits"][k] for k in ("requests", "window_seconds")},
            "resource_template": {
                "uriTemplate": SITE + "/d/{slug}/fields.json",
                "name": "dataset-fields",
                "title": "Dataset fields",
                "description": m["resource_template"],
                "mimeType": "application/json",
                "annotations": RESOURCE_ANNOTATIONS,
            },
            "prompts": [
                {
                    "name": name,
                    "title": pr["title"],
                    "description": pr["description"],
                    "arguments": [
                        {"name": a, "description": v["description"], "required": v["required"]}
                        for a, v in pr["arguments"].items()
                    ],
                    "text": pr["text"],
                    **({"question": pr["question"]} if "question" in pr else {}),
                }
                for name, pr in m["prompts"].items()
            ],
            "tools": tools,
        }
    )


def _remote() -> dict:
    return {"type": "streamable-http", "url": SITE + "/mcp"}


def server_card() -> dict:
    """The MCP server card, served at /mcp/server-card."""
    m = spec()["mcp"]
    return plain(
        {
            "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
            "name": m["registry_name"],
            "title": m["title"],
            "version": release(),
            "description": m["registry_description"],
            "websiteUrl": SITE + "/agents/#mcp",
            "repository": REPOSITORY,
            "icons": ICONS,
            "remotes": [{**_remote(), "supportedProtocolVersions": m["protocol_versions"]}],
        }
    )


def _registry_listing() -> dict:
    m = spec()["mcp"]
    return plain(
        {
            "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
            "name": m["registry_name"],
            "title": m["title"],
            "description": m["registry_description"],
            "websiteUrl": SITE + "/agents/#mcp",
            "repository": REPOSITORY,
            "icons": ICONS,
            "remotes": [_remote()],
        }
    )


def surface() -> str:
    """A hash of the registry listing and of what an MCP client sees, apart from the release."""
    s = mcp_spec()
    s["server"] = {k: v for k, v in s["server"].items() if k != "version"}
    s.pop("_comment", None)
    s["listing"] = _registry_listing()
    return hashlib.sha256(json.dumps(s, sort_keys=True).encode("utf-8")).hexdigest()


def registry_server() -> dict:
    """The official MCP Registry entry, published with mcp-publisher."""
    return {
        **_registry_listing(),
        "version": release(),
        # The registry does not hold the tools, so the hash is how the release job tells a change.
        "_meta": {
            "io.modelcontextprotocol.registry/publisher-provided": {"surface_sha256": surface()}
        },
    }


def prompts_note() -> str:
    """The prompts by name, as a sentence on the agents page."""
    names = [f"<code>{n}</code>" for n in spec()["mcp"]["prompts"]]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return spec()["mcp"]["prompts_note"].replace("{prompts}", joined)


def listed_at() -> list[str]:
    """The public directory pages for the MCP server, which its JSON-LD names as sameAs."""
    return [e["url"] for e in spec()["mcp"]["listing"]["listed_at"]]


def listed_note() -> str:
    """The same directories as visible links, since markup should describe what the page shows."""
    links = [
        f'<a href="{html.escape(e["url"])}">{html.escape(e["name"])}</a>'
        for e in spec()["mcp"]["listing"]["listed_at"]
    ]
    if not links:
        return ""
    joined = links[0] if len(links) == 1 else ", ".join(links[:-1]) + " and " + links[-1]
    return spec()["mcp"]["listing"]["listed_note"].replace("{listings}", joined)


def directory_listing() -> dict:
    """Every field of a connector directory's listing form, from the text the site already uses."""
    s = spec()
    m = s["mcp"]
    # The form caps the description, so each tool gets its first sentence; tools/list has the rest.
    tools = "\n".join(
        f"- {t['title']}: {t['description'].split('. ', 1)[0].rstrip('.')}."
        for t in s["webmcp"]["tools"].values()
    )
    return plain(
        {
            "name": m["title"],
            "slug": m["name"],
            "one_liner": m["registry_description"],
            "description": "\n\n".join(
                [
                    s["site"]["summary"],
                    f"{m['listing']['tools_intro']}\n{tools}",
                    m["resource_template"],
                    m["privacy"],
                ]
            ),
            "categories": m["listing"]["categories"],
            "author_name": OPERATOR,
            "author_url": OPERATOR_URL,
            "server_url": SITE + "/mcp",
            "icon": SITE + "/icon-512.png",
            "documentation": SITE + "/agents/#mcp",
            "privacy_policy": SITE + "/privacy/",
            "terms_of_service": SITE + "/terms/",
            "support": m["listing"]["support"],
            "authentication": "none",
            "use_cases": m["listing"]["use_cases"],
            "prerequisites": m["listing"]["prerequisites"],
            # The form checks this against the tool annotations, so it is read from them.
            "read_write": "Read and write"
            if any(not t["annotations"]["readOnlyHint"] for t in s["webmcp"]["tools"].values())
            else "Read only",
        }
    )


ROOT = Path(__file__).resolve().parents[2]
TOOLS_FILE = ROOT / "functions" / "_tools.json"


def _dump(v) -> str:
    return json.dumps(v, indent=2, ensure_ascii=False) + "\n"


def tools_json() -> str:
    return _dump(mcp_spec())


if __name__ == "__main__":
    # With no argument, rewrites functions/_tools.json; the deploy runs it again with the release
    # set. With a path, writes the MCP Registry's server.json there for publishing.
    import sys

    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(_dump(registry_server()), encoding="utf-8")
    else:
        TOOLS_FILE.write_text(tools_json(), encoding="utf-8")
