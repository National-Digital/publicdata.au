import html
import json
import re
import shutil
from pathlib import Path

import pytest

from publicdata import api_text as at
from publicdata import register

from .conftest import ROOT

FUNCTIONS = ROOT / "functions"
S = at.spec()
REG = {d.slug: d for d in register.load(ROOT / "register")}


def test_limits_operators_and_parameters_match_the_functions():
    q = (FUNCTIONS / "_query.js").read_text(encoding="utf-8")
    assert int(re.search(r"LIMIT_DEFAULT = (\d+)", q)[1]) == S["limits"]["limit_default"]
    assert int(re.search(r"LIMIT_MAX = (\d+)", q)[1]) == S["limits"]["limit_max"]
    ops = set(re.findall(r"(\w+): '", re.search(r"const OPS = \{(.*?)\};", q)[1]))
    handled = set(re.findall(r"op === '(\w+)'", q))
    assert set(S["operators"]) == (ops | handled | {"is.null"}) - {"is"}
    assert re.findall(r"'(\w+)'", re.search(r"const METRICS = \[(.*?)\]", q)[1]) == S["metrics"]
    reserved = set(
        re.findall(r"'(\w+)'", re.search(r"const RESERVED = new Set\(\[(.*?)\]\)", q)[1])
    )
    assert reserved == set(at.query_params())
    a = (FUNCTIONS / "_api.js").read_text(encoding="utf-8")
    q_, w = re.search(r'"fair-use";q=(\d+);w=(\d+)', a).groups()
    assert (int(q_), int(w)) == (S["limits"]["requests"], S["limits"]["window_seconds"])


def test_no_api_prose_is_written_outside_api_json():
    sources = [
        ROOT / "pipeline" / "publicdata" / "site.py",
        ROOT / "pipeline" / "publicdata" / "static" / "site.js",
        *(ROOT / "pipeline" / "publicdata" / "templates").glob("*.html"),
    ]
    n = str(S["limits"]["requests"])
    for f in sources:
        t = f.read_text(encoding="utf-8")
        assert not re.search(rf"\b{n} requests|q={n}\b", t), f.name
        assert "registerTool({" not in t and "description: '" not in t, f.name


@pytest.fixture
def site(fixture_site) -> Path:
    return fixture_site


def test_site_js_carries_the_spec_and_an_executor_for_every_tool(site):
    js = (site / "static" / "site.js").read_text(encoding="utf-8")
    assert "/*API_SPEC*/" not in js
    injected = json.loads(re.search(r"var SPEC = (\{.*?\});\n", js)[1])
    assert injected == at.browser_spec()
    assert set(re.findall(r"EXEC\.(\w+) = function", js)) == set(at.tool_names())


def test_the_mcp_server_has_every_tool_from_api_json(site):
    # The committed copy is a development build; the deploy rewrites it with the release.
    assert (FUNCTIONS / "_tools.json").read_text(encoding="utf-8") == at.tools_json(), (
        "run python -m publicdata.api_text in pipeline/"
    )
    card = json.loads((site / "mcp" / "server-card").read_text(encoding="utf-8"))
    assert card == at.server_card()
    assert (
        json.loads((site / ".well-known" / "mcp" / "server-card.json").read_text("utf-8")) == card
    )
    reg = at.registry_server()
    assert len(reg["description"]) <= 100 and reg["name"] == card["name"]
    auth = (site / ".well-known" / "mcp-registry-auth").read_text(encoding="utf-8")
    assert re.fullmatch(r"v=MCPv1; k=ed25519; p=[A-Za-z0-9+/]{43}=\n", auth)
    glama = json.loads((site / ".well-known" / "glama.json").read_text(encoding="utf-8"))
    assert glama["claim"].startswith("glama_claim_")
    challenge = (site / ".well-known" / "openai-apps-challenge").read_text(encoding="utf-8")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
    for t in S["webmcp"]["tools"].values():
        a = t["annotations"]
        assert t["title"] and set(a) == {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        }
    # Directories read the name from annotations.title; clients read the top-level title.
    for d in at.mcp_spec()["tools"]:
        assert d["annotations"]["title"] == d["title"], d["name"]
    js = (FUNCTIONS / "_tools.js").read_text(encoding="utf-8")
    assert set(re.findall(r"^EXEC\.(\w+) = async", js, re.MULTILINE)) == set(at.tool_names())
    assert "/mcp" in json.loads((site / "_routes.json").read_text(encoding="utf-8"))["include"]
    ard = json.loads((site / ".well-known" / "ard.json").read_text(encoding="utf-8"))
    assert any(e["url"] == "https://publicdata.au/mcp/server-card" for e in ard["entries"])
    privacy = (site / "privacy" / "index.html").read_text(encoding="utf-8")
    assert at.as_html(S["mcp"]["privacy"]) in privacy


def test_every_query_parameter_is_described_and_every_tool_maps_onto_one(site):
    doc = json.loads((site / "openapi.json").read_text(encoding="utf-8"))
    ds_doc = json.loads(
        (site / "d" / "qld-road-casualties" / "openapi.json").read_text(encoding="utf-8")
    )
    by_op = {}
    for d in (doc, ds_doc):
        for path, item in d["paths"].items():
            if not path.startswith("/api/v1/datasets/"):
                continue
            params = item["get"]["parameters"]
            for p in params:
                assert p.get("description"), (path, p["name"])
            op = path.rsplit("/", 1)[1]
            names = {p["name"] for p in params}
            by_op.setdefault(op, set()).update(names)
            if "{version}" in path:
                by_op.setdefault(op + "@version", set()).update(names)
    reserved = set(at.query_params())
    assert reserved <= by_op["rows"] | by_op["aggregate"]
    for name, t in S["webmcp"]["tools"].items():
        for k, p in t["input"].items():
            if "api" not in p:
                continue
            assert t.get("api") in ("rows", "aggregate"), name
            if p["api"] == "filters":
                # Field filters come from the dataset's own document.
                assert by_op[t["api"]] - reserved - {"slug", "version"}, (name, k)
            elif p["api"] == "version":
                assert "version" in by_op[t["api"] + "@version"], (name, k)
            else:
                assert p["api"] in by_op[t["api"]], (name, k)


def test_each_sentence_reaches_every_output(site):
    rate = S["api"]["rate_limit"]
    assert at.as_html(rate) in (site / "agents" / "index.html").read_text(encoding="utf-8")
    assert rate in (site / "llms.txt").read_text(encoding="utf-8")
    assert rate in json.loads((site / "openapi.json").read_text("utf-8"))["info"]["description"]
    page = (site / "d" / "qld-road-casualties" / "index.html").read_text(encoding="utf-8")
    assert at.as_html(rate, "mono") in page
    assert at.as_html(S["api"]["filters"], "mono") in page
    for o in S["operators"].values():
        assert f'<td class="mono">{o["syntax"]}</td>' in page
    assert at.as_html(S["mcp"]["intro"], "mono") in page
    agents = (site / "agents" / "index.html").read_text(encoding="utf-8")
    assert at.prompts_note() in agents
    assert all(f"<code>{n}</code>" in agents for n in S["mcp"]["prompts"])
    assert at.as_html(S["mcp"]["intro"]) in agents
    assert at.plain(S["mcp"]["intro"]) in (site / "llms.txt").read_text(encoding="utf-8")
    md = (site / "d" / "qld-road-casualties" / "index.md").read_text(encoding="utf-8")
    assert at.filter_help() in md and rate in md
    for n in at.tool_names():
        assert f"<code>{n}</code>" in (site / "agents" / "index.html").read_text("utf-8")


def test_the_release_version_reaches_every_output(site, monkeypatch):
    # The fixture build ran without PUBLICDATA_RELEASE, as a local build does.
    assert json.loads((site / "openapi.json").read_text("utf-8"))["info"]["version"] == "0.0.0-dev"
    assert json.loads((site / "health.json").read_text("utf-8"))["release"] == "0.0.0-dev"
    assert json.loads((site / "mcp" / "server-card").read_text("utf-8"))["version"] == "0.0.0-dev"
    monkeypatch.setenv("PUBLICDATA_RELEASE", "1.4.2")
    assert at.mcp_spec()["server"]["version"] == "1.4.2"
    assert at.server_card()["version"] == at.registry_server()["version"] == "1.4.2"


def test_every_queryable_dataset_is_an_mcp_resource(site, tmp_path):
    from publicdata import gate

    listed = json.loads((site / "mcp" / "resources.json").read_text(encoding="utf-8"))["resources"]
    assert listed and not gate._mcp_resources(site)
    for r in listed:
        f = json.loads((site / "d" / r["name"] / "fields.json").read_text(encoding="utf-8"))
        assert r["title"] == f["title"]
        assert r["description"] == at.resource_text(f["title"], f["publisher"])
    # A fields file that drifts from its page stops the deploy.
    bad = tmp_path / "dist"
    shutil.copytree(site / "d" / listed[0]["name"], bad / "d" / listed[0]["name"])
    shutil.copytree(site / "mcp", bad / "mcp")
    f = bad / "d" / listed[0]["name"] / "fields.json"
    f.write_text(json.dumps({"fields": []}), encoding="utf-8")
    assert any("differ" in e for e in gate._mcp_resources(bad))


def test_the_directory_listing_is_built_from_the_site_text_and_fits_the_forms(site):
    got = json.loads((site / "mcp" / "listing.json").read_text(encoding="utf-8"))
    assert got == at.directory_listing()
    from publicdata import gate

    assert not gate._mcp_listing(site), "shorten a tool description or the site summary"
    over = site.parent / "over"
    (over / "mcp").mkdir(parents=True)
    (over / "mcp" / "listing.json").write_text(
        json.dumps({**got, "description": "x" * 2001}), encoding="utf-8"
    )
    assert gate._mcp_listing(over) == [
        "mcp/listing.json: description is 2001 characters, over 2000"
    ]
    for t in S["webmcp"]["tools"].values():
        first = at.plain(t["description"]).split(". ", 1)[0]
        assert f"- {t['title']}: {first}" in got["description"]
    assert S["site"]["summary"] in (site / "llms.txt").read_text(encoding="utf-8")
    assert got["read_write"] == "Read and write"  # vote records a vote


def test_resources_carry_their_size_and_date(site):
    for r in json.loads((site / "mcp" / "resources.json").read_text("utf-8"))["resources"]:
        f = site / "d" / r["name"] / "fields.json"
        assert r["size"] == f.stat().st_size, r["name"]
        body = json.loads(f.read_text(encoding="utf-8"))
        version = body["version"]
        assert body["partition_by"] == list(REG[r["name"]].partition_by), r["name"]
        assert body["key"] == list(REG[r["name"]].key), r["name"]
        assert r["annotations"]["lastModified"] == f"{version}T00:00:00Z"
        assert r["annotations"]["audience"] == ["assistant"]


def test_the_mcp_server_is_an_entity_the_directories_identify(site):
    def nodes(rel):
        page = (site / rel).read_text(encoding="utf-8")
        return [
            json.loads(m)
            for m in re.findall(
                r'<script type="application/ld\+json">(.*?)</script>', page, re.DOTALL
            )
        ]

    for rel in ("index.html", "agents/index.html"):
        api = next(n for n in nodes(rel) if n.get("@type") == "WebAPI")
        assert (
            api["@id"] == "https://publicdata.au/mcp#server"
            and api["url"] == "https://publicdata.au/mcp"
        )
        assert api["sameAs"] == [e["url"] for e in S["mcp"]["listing"]["listed_at"]]
    # Each sameAs is also a visible link where the server is described.
    agents = (site / "agents" / "index.html").read_text(encoding="utf-8")
    assert at.listed_note() in agents
    assert all(
        f'href="{html.escape(e["url"])}"' in agents for e in S["mcp"]["listing"]["listed_at"]
    )
    catalog = next(n for n in nodes("index.html") if n.get("@type") == "DataCatalog")
    assert "sameAs" not in catalog
    dataset = next(
        n for n in nodes("d/qld-road-casualties/index.html") if n.get("@type") == "Dataset"
    )
    assert (
        dataset["includedInDataCatalog"]["@id"]
        == catalog["@id"]
        == "https://publicdata.au/#catalog"
    )


def test_the_terms_page_is_linked_everywhere_a_directory_looks(site):
    page = (site / "terms" / "index.html").read_text(encoding="utf-8")
    assert at.as_html(S["api"]["terms_limits"]) in page and "Australian Consumer Law" in page
    assert "https://publicdata.au/terms/" in S["mcp"]["instructions"]
    assert 'href="/terms/"' in (site / "index.html").read_text(encoding="utf-8")
    sitemaps = "".join(f.read_text(encoding="utf-8") for f in (site / "sitemaps").glob("*.xml"))
    assert "<loc>https://publicdata.au/terms/</loc>" in sitemaps
    assert at.directory_listing()["terms_of_service"] == "https://publicdata.au/terms/"
    home = (site / "index.html").read_text(encoding="utf-8")
    nodes = [
        json.loads(m)
        for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', home, re.DOTALL)
    ]
    api = next(n for n in nodes if n.get("@type") == "WebAPI")
    assert api["termsOfService"] == "https://publicdata.au/terms/"


def test_the_terms_date_moves_with_the_terms_text():
    from publicdata import site

    assert site.terms_hash() == site.TERMS_CHANGED[1], (
        "The terms text changed: set TERMS_CHANGED in site.py to today's date and "
        f"hash {site.terms_hash()}"
    )
