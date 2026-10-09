import json
from typing import TYPE_CHECKING, TypedDict

from publicdata import SITE
from publicdata import api_text as at
from publicdata.ard import CATALOGUE, DISCOVERY_TYPES, SKILL, problems
from publicdata.gate import ard_errors

from .conftest import read_json

if TYPE_CHECKING:
    from pathlib import Path


class Entry(TypedDict, total=False):
    """The keys of a catalogue entry these tests read."""

    identifier: str
    type: str
    url: str
    capabilities: list[str]
    version: str
    representativeQueries: list[str]


class Catalogue(TypedDict):
    entries: list[Entry]


def _catalogue(path: Path) -> Catalogue:
    got: Catalogue = json.loads(path.read_text(encoding="utf-8"))
    return got


def _ard(site: Path) -> Catalogue:
    return _catalogue(site / ".well-known" / "ard.json")


def test_the_manifest_lists_only_discovery_types_and_every_catalogue_it_links_passes(
    fixture_site: Path,
) -> None:
    doc = _ard(fixture_site)
    assert {e["type"] for e in doc["entries"]} <= set(DISCOVERY_TYPES)
    assert ard_errors(fixture_site) == []
    assert (fixture_site / ".well-known" / "ai-catalog.json").read_bytes() == (
        fixture_site / ".well-known" / "ard.json"
    ).read_bytes()
    live = read_json(fixture_site / "latest.json")
    bundles = {
        e["identifier"].rsplit(":", 1)[1]: e
        for e in doc["entries"]
        if ":dataset:" in e["identifier"]
    }
    assert set(bundles) == set(live)
    mcp = next(e for e in doc["entries"] if e["type"] == "application/mcp-server-card+json")
    assert mcp["capabilities"] == at.tool_names()
    for slug, e in bundles.items():
        assert e["type"] == CATALOGUE
        assert e["url"] == f"{SITE}/d/{slug}/ai-catalog.json"
        nested = _catalogue(fixture_site / "d" / slug / "ai-catalog.json")
        files = [n for n in nested["entries"] if n["type"] != mcp["type"]]
        assert files
        assert all(f"/d/{slug}/v/{e['version']}/" in n["url"] for n in files)
        assert nested["entries"][-1] == mcp
    skill = next(e for e in doc["entries"] if e["type"] == SKILL)
    text = (fixture_site / skill["url"].removeprefix(SITE + "/")).read_text(encoding="utf-8")
    assert text.startswith(f"---\nname: {at.spec()['skill']['name']}\ndescription: ")
    assert at.spec()["site"]["suppressed"] in text


def test_a_file_at_the_top_level_or_a_missing_bundle_fails(site_copy: Path) -> None:
    path = site_copy / ".well-known" / "ard.json"
    doc = _ard(site_copy)
    bundle = next(e for e in doc["entries"] if ":dataset:" in e["identifier"])
    nested = _catalogue(site_copy / bundle["url"].removeprefix(SITE + "/"))
    doc["entries"].append(nested["entries"][0] | {"representativeQueries": ["a", "b"]})
    path.write_text(json.dumps(doc), encoding="utf-8")
    found = ard_errors(site_copy)
    assert len(found) == 1, found
    assert "is not a discovery type" in found[0], found

    doc["entries"].pop()
    (site_copy / bundle["url"].removeprefix(SITE + "/")).unlink()
    path.write_text(json.dumps(doc), encoding="utf-8")
    found = ard_errors(site_copy)
    assert len(found) == 1, found
    assert "which the build did not write" in found[0], found


def test_each_conformance_rule_fires() -> None:
    entry = {
        "identifier": "urn:air:example.org:mcp:server",
        "displayName": "Server",
        "type": "application/mcp-server-card+json",
        "url": "https://example.org/card",
        "representativeQueries": ["one", "two"],
    }
    good = {"specVersion": "1.0", "entries": [entry]}
    assert problems(good) == []
    cases = {
        "specVersion": {**good, "specVersion": "0.9"},
        "unknown root keys": {**good, "collections": []},
        "not urn:air": {**good, "entries": [entry | {"identifier": "server"}]},
        "exactly one of url and data": {**good, "entries": [entry | {"data": {}}]},
        "no representativeQueries": {
            **good,
            "entries": [{k: v for k, v in entry.items() if k != "representativeQueries"}],
        },
        "2 to 5 strings": {**good, "entries": [entry | {"representativeQueries": ["one"]}]},
        "identifier repeated": {**good, "entries": [entry, entry]},
        "unknown keys": {**good, "entries": [entry | {"mediaType": "x"}]},
        "missing displayName": {**good, "entries": [entry | {"displayName": ""}]},
    }
    for want, doc in cases.items():
        found = problems(doc)
        assert any(want in p for p in found), (want, found)
    held = entry | {
        "type": "application/vnd.apache.parquet",
        "url": "https://example.org/x.parquet",
    }
    inline = entry | {
        "identifier": "urn:air:example.org:catalog:bundle",
        "type": CATALOGUE,
        "data": {"specVersion": "1.0", "entries": [held]},
    }
    del inline["url"]
    assert problems({**good, "entries": [inline]}) == []
    held["identifier"] = "bad"
    assert any("not urn:air" in p for p in problems({**good, "entries": [inline]}))
