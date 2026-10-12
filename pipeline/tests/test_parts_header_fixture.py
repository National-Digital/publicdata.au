"""The provenance headers functions/_parts.test.mjs holds a parts answer to.

A finished part can be an earlier version's file, written under the register as it was then, so
its footer carries that version's header and that register's dataset fields. D1 loads a version
stored as parts with the version's own header (d1.parts_header, as d1.as_built joins it). From a
copy of a real entry and its two stored manifests (fixtures/parts/headers/), the fixture answers
the older of the two, pinned to fewer layers than the newest, whose newest part an even earlier
version wrote under a since-retitled entry. It holds that part's header, the answered version's
manifest as the build writes it (build.version_manifest), the dataset's part of the header as the
newest version's fields.json gives it (d1.dataset_header), and the header D1 loads the answered
version with, so the function's header for a parts answer is held to D1's own code. Run
`python -m tests.test_parts_header_fixture` in pipeline/ to write it again.
"""

import dataclasses
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from publicdata import SITE, d1, store
from publicdata.build import version_manifest
from publicdata.provenance import header
from publicdata.register import load

if TYPE_CHECKING:
    from publicdata.normalise import Table

HERE = Path(__file__).parent / "fixtures" / "parts"
FIXTURE = HERE / "headers.json"
SOURCES = HERE / "headers"
SLUG = "au-eucalypt-records"
ROWS = 1234


def headers() -> dict[str, object]:
    (ds,) = load(SOURCES / "register")
    answered, newest = store.manifests(SOURCES / "store", SLUG)
    # The answered version was joined to two layers; the newest to every layer the entry names.
    pins = (answered.spine or [])[:2]
    answered = dataclasses.replace(answered, spine=pins)
    # Its newest part was written by a version before it, under an entry since retitled.
    then = dataclasses.replace(ds, title=f"{ds.title} (as first registered)")
    writer = dataclasses.replace(
        answered,
        version="2026-09-01",
        as_at="2026-06-30",
        fetched_at="2026-09-01T00:00:00+00:00",
        sha256="0" * 64,
    )
    part_url = f"{SITE}/d/{SLUG}/v/{writer.version}/parts/2019.parquet"
    part = dict(header(then, writer, 30, part_url))
    part["period"] = {"field": "year", "grain": "year", "value": "2019"}
    tbl = SimpleNamespace(
        rows=ROWS,
        unknown_columns=[],
        suppressed_cells=0,
        short_rows=0,
        omitted_columns=[],
        places=pins,
    )
    split = {"period": {"field": "year", "grain": "year"}, "whole": False, "parts": []}
    built = version_manifest(
        ds,
        answered,
        cast("Table", tbl),
        split,  # type: ignore[arg-type]  # the parts are left out; the header reads none of them
        f"{SITE}/d/{SLUG}/v/{answered.version}/",
    )
    # d1 reads the built manifest back from the build's tree, as here, and joins the entry as the
    # version was built.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "d" / SLUG / "v" / answered.version / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(built), encoding="utf-8")
        m = store.Manifest.read(path)
        loaded = d1.parts_header(ds, m, built)
        # The loader passes the entry as the version was built; the header is the same either way.
        assert d1.parts_header(d1.as_built([root], ds, answered.version), m, built) == loaded
    newest_built = {"rows": ROWS, "places": newest.spine or []}
    return {
        "slug": SLUG,
        "version": answered.version,
        "part": part,
        "manifest": built,
        "dataset": d1.dataset_header(ds, newest, newest_built),
        "d1": loaded,
    }


def text() -> str:
    return json.dumps(headers(), indent=2, ensure_ascii=False) + "\n"


def test_the_parts_header_fixture_is_current() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == text(), (
        "run python -m tests.test_parts_header_fixture in pipeline/"
    )


def test_the_part_header_differs_from_the_version_header_where_the_function_must_fix_it() -> None:
    h = headers()
    part, loaded, manifest = h["part"], h["d1"], h["manifest"]
    assert isinstance(part, dict)
    assert isinstance(loaded, dict)
    assert isinstance(manifest, dict)
    for k in ("version", "as_at", "url", "title", "attribution", "cite", "source", "rows"):
        assert part[k] != loaded[k], k
    assert "period" in part
    assert "period" not in loaded
    # The answered version's places and field count are its own, not the newest version's.
    newest = store.manifests(SOURCES / "store", SLUG)[-1]
    assert len(loaded["places"]["datasets"]) == 2 < len(newest.spine or [])
    assert loaded["fields"] == manifest["fields"]


def test_a_built_manifest_never_carries_the_spine_pin() -> None:
    h = headers()
    manifest = h["manifest"]
    assert isinstance(manifest, dict)
    assert "spine" not in manifest
    assert manifest["places"]


def test_a_pinned_version_publishes_the_manifest_it_did_before_the_pin(fixture_site: Path) -> None:
    # The fixture crash version is joined and pinned. Its manifest.json is held to the bytes the
    # build published before pins existed (fixtures/built/), so the pin never reaches a published
    # file and the deploy's real-data check finds the cache entries it already has.
    built = fixture_site / "d" / "qld-road-crash-locations" / "v" / "2026-04-24" / "manifest.json"
    before = Path(__file__).parent / "fixtures" / "built"
    assert "spine" not in json.loads(built.read_text(encoding="utf-8"))
    assert (
        built.read_bytes()
        == (before / "qld-road-crash-locations-2026-04-24-manifest.json").read_bytes()
    )


def test_only_a_joined_dataset_names_the_place_attribution(fixture_site: Path) -> None:
    def provenance(slug: str) -> dict[str, object]:
        body = json.loads((fixture_site / "d" / slug / "fields.json").read_text(encoding="utf-8"))
        out = body["provenance"]
        assert isinstance(out, dict)
        return out

    assert "places_attribution" in provenance("qld-road-crash-locations")
    unjoined = provenance("qld-road-casualties")
    assert "places_attribution" not in unjoined
    assert unjoined["title"]


if __name__ == "__main__":
    FIXTURE.write_text(text(), encoding="utf-8")
