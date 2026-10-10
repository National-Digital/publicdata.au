"""The query API and agent tool prose from api.json, filled and rendered for each output."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import OPERATOR, REPO, SITE
from .provenance import OPERATOR_URL

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .jsontypes import JSON

    class Schema(TypedDict, total=False):
        """A JSON Schema as api.json and the MCP tools write one; api names the API text."""

        type: str | list[str]
        description: str
        api: str
        default: JSON
        minimum: int | float
        maximum: int | float
        maxItems: int
        enum: list[JSON]
        items: Schema
        properties: dict[str, Schema]
        required: list[str]
        additionalProperties: bool

    class ToolAnnotations(TypedDict, total=False):
        title: str
        readOnlyHint: bool
        destructiveHint: bool
        idempotentHint: bool
        openWorldHint: bool

    class ToolText(TypedDict):
        """A tool as api.json describes it."""

        title: str
        description: str
        annotations: ToolAnnotations
        input: dict[str, Schema]
        required: list[str]
        output: Schema
        api: NotRequired[str]

    class Tool(TypedDict, total=False):
        """A tool as the MCP server lists it, or as a test writes one before a check reads it."""

        name: str
        title: str
        description: str
        inputSchema: Schema
        outputSchema: Schema
        annotations: ToolAnnotations
        queries: int

    class Operator(TypedDict):
        label: str
        syntax: str
        description: str
        not_label: NotRequired[str]

    Parameter = TypedDict(
        "Parameter", {"description": str, "on": NotRequired[str], "in": NotRequired[str]}
    )

    class PathText(TypedDict):
        summary: str
        description: NotRequired[str]

    class Paths(TypedDict):
        rows: PathText
        aggregate: PathText
        versions: PathText

    class Limits(TypedDict):
        requests: int
        window_seconds: int
        block_seconds: int
        limit_default: int
        limit_max: int

    class SiteText(TypedDict):
        summary: str
        suppressed: str
        file_formats: str

    class SkillText(TypedDict):
        """The Agent Skill the discovery manifest lists."""

        name: str
        title: str
        description: str
        queries: list[str]

    class ApiText(TypedDict):
        summary: str
        rows: str
        aggregate: str
        versions: str
        filters: str
        provenance: str
        terms_limits: str
        rate_limit: str

    class WebMCP(TypedDict):
        intro: str
        page_note: str
        tools: dict[str, ToolText]
        where: str
        version: str

    class Connect(TypedDict):
        label: str
        kind: str
        value: str
        note: str

    class Listed(TypedDict):
        name: str
        url: str

    class UseCase(TypedDict):
        use_case: str
        prompt: str

    class Listing(TypedDict):
        categories: list[str]
        listed_at: list[Listed]
        listed_note: str
        use_cases: list[UseCase]
        prerequisites: str
        tools_intro: str
        support: str

    class PromptArgument(TypedDict):
        description: str
        required: bool

    class PromptText(TypedDict):
        title: str
        description: str
        arguments: dict[str, PromptArgument]
        text: str
        question: NotRequired[str]

    class McpText(TypedDict):
        name: str
        registry_name: str
        title: str
        protocol_versions: list[str]
        registry_description: str
        add_command: str
        connect: list[Connect]
        intro: str
        resource: str
        resource_template: str
        listing: Listing
        prompts_note: str
        prompts: dict[str, PromptText]
        tool_text: dict[str, str]
        parameters: dict[str, str]
        privacy: str
        instructions: str

    class ApiSpec(TypedDict):
        """api.json."""

        _comment: NotRequired[str]
        site: SiteText
        skill: SkillText
        limits: Limits
        api: ApiText
        operators: dict[str, Operator]
        metrics: list[str]
        parameters: dict[str, Parameter]
        paths: Paths
        responses: dict[str, str]
        webmcp: WebMCP
        mcp: McpText

    class Icon(TypedDict):
        src: str
        mimeType: str
        sizes: list[str]

    class Repository(TypedDict):
        url: str
        source: str

    class Remote(TypedDict):
        type: str
        url: str
        supportedProtocolVersions: NotRequired[list[str]]

    class ServerInfo(TypedDict):
        name: str
        title: str
        version: NotRequired[str]
        description: str
        websiteUrl: str
        icons: list[Icon]

    class McpLimits(TypedDict):
        requests: int
        window_seconds: int

    class ResourceAnnotations(TypedDict):
        audience: list[str]
        priority: float

    class ResourceTemplate(TypedDict):
        uriTemplate: str
        name: str
        title: str
        description: str
        mimeType: str
        annotations: ResourceAnnotations

    class PromptArg(TypedDict):
        name: str
        description: str
        required: bool

    class Prompt(TypedDict):
        name: str
        title: str
        description: str
        arguments: list[PromptArg]
        text: str
        question: NotRequired[str]

    RegistryListing = TypedDict(
        "RegistryListing",
        {
            "$schema": str,
            "name": str,
            "title": str,
            "description": str,
            "websiteUrl": str,
            "repository": Repository,
            "icons": list[Icon],
            "remotes": list[Remote],
            "version": NotRequired[str],
            "_meta": NotRequired[dict[str, dict[str, str]]],
        },
    )

    class McpSpec(TypedDict):
        """What the MCP server answers initialize and tools/list with."""

        _comment: NotRequired[str]
        server: ServerInfo
        protocol_versions: list[str]
        instructions: str
        limits: McpLimits
        resource_template: ResourceTemplate
        prompts: list[Prompt]
        tools: list[Tool]
        listing: NotRequired[RegistryListing]

    ServerCard = TypedDict(
        "ServerCard",
        {
            "$schema": str,
            "name": str,
            "title": str,
            "version": str,
            "description": str,
            "websiteUrl": str,
            "repository": Repository,
            "icons": list[Icon],
            "remotes": list[Remote],
        },
    )

    class OperatorLabel(TypedDict):
        label: str
        not_label: NotRequired[str]

    class ParameterText(TypedDict):
        description: str

    class BrowserSpec(TypedDict):
        operators: dict[str, OperatorLabel]
        parameters: dict[str, ParameterText]
        webmcp: WebMCP

    class DirectoryListing(TypedDict):
        name: str
        slug: str
        one_liner: str
        description: str
        categories: list[str]
        author_name: str
        author_url: str
        server_url: str
        icon: str
        documentation: str
        privacy_policy: str
        terms_of_service: str
        support: str
        authentication: str
        use_cases: list[UseCase]
        prerequisites: str
        read_write: str


_PH = re.compile(r"\{([a-z_]+)\}")
_CODE = re.compile(r"`([^`]+)`")


ICONS: list[Icon] = [
    {"src": SITE + "/favicon.svg", "mimeType": "image/svg+xml", "sizes": ["any"]},
    {"src": SITE + "/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]},
]
REPOSITORY: Repository = {"url": REPO, "source": "github"}


def _num(v: object) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


def fill[T](v: T, values: Mapping[str, object]) -> T:
    """Fills {name} placeholders that values knows, anywhere in a nested value."""
    # Only strings change, so the value keeps its shape.
    return cast("T", _fill(v, values))


def _fill(v: object, values: Mapping[str, object]) -> object:
    if isinstance(v, str):
        return _PH.sub(lambda m: _num(values[m[1]]) if m[1] in values else m[0], v)
    if isinstance(v, dict):
        return {k: _fill(x, values) for k, x in v.items()}
    if isinstance(v, list):
        return [_fill(x, values) for x in v]
    return v


@cache
def spec() -> ApiSpec:
    raw: ApiSpec = json.loads((Path(__file__).parent / "api.json").read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    return fill(raw, raw["limits"])


def plain[T](v: T) -> T:
    """Drops code marks, for tool schemas and anywhere Markdown is not read."""
    # Only strings change, so the value keeps its shape.
    return cast("T", _plain(v))


def _plain(v: object) -> object:
    if isinstance(v, str):
        return _CODE.sub(r"\1", v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def as_html(s: str, cls: str = "") -> str:
    open_ = f'<span class="{cls}">' if cls else "<code>"
    close = "</span>" if cls else "</code>"
    return _CODE.sub(lambda m: open_ + m[1] + close, html.escape(s, quote=False))


def operator_list() -> str:
    """The operators as one Markdown sentence."""
    return "Operators: " + ", ".join(f"`{o['syntax']}`" for o in spec()["operators"].values()) + "."


def filter_help() -> str:
    filters: str = spec()["api"]["filters"]
    return filters + " " + operator_list()


def param_text(name: str) -> str:
    p = spec()["parameters"][name]
    d: str = p["description"]
    return f"On `{p['on']}`, {d[0].lower()}{d[1:]}" if p.get("on") else d


def query_params() -> list[str]:
    """The reserved query-string parameters, in the order the spec gives them."""
    return [n for n, p in spec()["parameters"].items() if p.get("in", "query") == "query"]


def tool_names() -> list[str]:
    return list(spec()["webmcp"]["tools"])


def _label(o: Operator) -> OperatorLabel:
    label: OperatorLabel = {"label": o["label"]}
    if "not_label" in o:
        label["not_label"] = o["not_label"]
    return label


def browser_spec() -> BrowserSpec:
    """What site.js needs: operator labels, parameter text and the tools, without code marks."""
    s = spec()
    out: BrowserSpec = {
        "operators": {k: _label(o) for k, o in s["operators"].items()},
        "parameters": {k: {"description": param_text(k)} for k in s["parameters"]},
        "webmcp": s["webmcp"],
    }
    return plain(out)


# Each dataset's fields are written for the model to read before it queries.
RESOURCE_ANNOTATIONS: ResourceAnnotations = {"audience": ["assistant"], "priority": 0.8}


def release() -> str:
    """The site's semantic version.

    The deploy sets it from the last tag and the merged PR's title (scripts/next-version.mjs);
    anywhere else it is a development build.
    """
    return os.environ.get("PUBLICDATA_RELEASE") or "0.0.0-dev"


def resource_text(title: str, publisher: str) -> str:
    text: str = spec()["mcp"]["resource"]
    return plain(text).replace("{title}", title).replace("{publisher}", publisher)


def _input_schema(t: ToolText, overrides: Mapping[str, str] | None = None) -> Schema:
    """A tool's input schema for any dataset.

    site.js builds the same, then narrows it on a dataset page. `overrides` gives the server's
    own text for a parameter, by its API name.
    """
    s = spec()
    w = s["webmcp"]
    overrides = overrides or {}
    version = overrides.get("version", w["version"])
    props: dict[str, Schema] = {}
    for k, p in t["input"].items():
        if p.get("api") == "filters":
            props[k] = {"type": "object", "description": w["where"], "additionalProperties": True}
            continue
        o = p.copy()
        o.pop("api", None)
        if "description" not in o and p.get("api") == "version":
            o["description"] = version
        elif "description" not in o and p.get("api") in overrides:
            o["description"] = overrides[p["api"]]
        elif "description" not in o and p.get("api"):
            o["description"] = param_text(p["api"])
        props[k] = o
    return {
        "type": "object",
        "properties": props,
        "required": list(t["required"]),
        "additionalProperties": False,
    }


def _prompt(name: str, pr: PromptText) -> Prompt:
    out: Prompt = {
        "name": name,
        "title": pr["title"],
        "description": pr["description"],
        "arguments": [
            {"name": a, "description": v["description"], "required": v["required"]}
            for a, v in pr["arguments"].items()
        ],
        "text": pr["text"],
    }
    if "question" in pr:
        out["question"] = pr["question"]
    return out


def mcp_spec() -> McpSpec:
    """What the MCP server at /mcp answers initialize and tools/list with."""
    s = spec()
    m = s["mcp"]
    tools: list[Tool] = []
    for name, t in s["webmcp"]["tools"].items():
        # Text the server words differently from the pages, if any; both reach every version.
        description = t["description"]
        for old, new in m["tool_text"].items():
            description = description.replace(old, new)
        d: Tool = {
            "name": name,
            "title": t["title"],
            "description": description,
            "inputSchema": _input_schema(t, m["parameters"]),
            "outputSchema": t["output"],
            "annotations": {"title": t["title"], **t["annotations"]},
        }
        # The row tools run the query and then count the match, so each call is two queries.
        d["queries"] = 2 if t.get("api") else 0
        tools.append(d)
    out: McpSpec = {
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
        "limits": {
            "requests": s["limits"]["requests"],
            "window_seconds": s["limits"]["window_seconds"],
        },
        "resource_template": {
            "uriTemplate": SITE + "/d/{slug}/fields.json",
            "name": "dataset-fields",
            "title": "Dataset fields",
            "description": m["resource_template"],
            "mimeType": "application/json",
            "annotations": RESOURCE_ANNOTATIONS,
        },
        "prompts": [_prompt(name, pr) for name, pr in m["prompts"].items()],
        "tools": tools,
    }
    return plain(out)


def _remote() -> Remote:
    return {"type": "streamable-http", "url": SITE + "/mcp"}


def server_card() -> ServerCard:
    """The MCP server card, served at /mcp/server-card."""
    m = spec()["mcp"]
    out: ServerCard = {
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
    return plain(out)


def _registry_listing() -> RegistryListing:
    m = spec()["mcp"]
    out: RegistryListing = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": m["registry_name"],
        "title": m["title"],
        "description": m["registry_description"],
        "websiteUrl": SITE + "/agents/#mcp",
        "repository": REPOSITORY,
        "icons": ICONS,
        "remotes": [_remote()],
    }
    return plain(out)


def surface() -> str:
    """A hash of the registry listing and of what an MCP client sees, apart from the release."""
    s = mcp_spec()
    server = s["server"].copy()
    server.pop("version", None)
    s["server"] = server
    s.pop("_comment", None)
    s["listing"] = _registry_listing()
    return hashlib.sha256(json.dumps(s, sort_keys=True).encode("utf-8")).hexdigest()


def registry_server() -> RegistryListing:
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
    note: str = spec()["mcp"]["prompts_note"]
    return note.replace("{prompts}", joined)


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
    note: str = spec()["mcp"]["listing"]["listed_note"]
    return note.replace("{listings}", joined)


def directory_listing() -> DirectoryListing:
    """Every field of a connector directory's listing form, from the text the site already uses."""
    s = spec()
    m = s["mcp"]
    # The form caps the description, so each tool gets its first sentence; tools/list has the rest.
    tools = "\n".join(
        f"- {t['title']}: {t['description'].split('. ', 1)[0].rstrip('.')}."
        for t in s["webmcp"]["tools"].values()
    )
    out: DirectoryListing = {
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
    return plain(out)


ROOT = Path(__file__).resolve().parents[2]
TOOLS_FILE = ROOT / "functions" / "_tools.json"


def _dump(v: object) -> str:
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
