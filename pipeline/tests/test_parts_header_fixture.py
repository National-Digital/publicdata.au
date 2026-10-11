"""The provenance headers functions/_parts.test.mjs holds a parts answer to.

A finished part can be an earlier version's file, written under the register as it was then, so
its footer carries that version's header and that register's dataset fields. D1 loads a version
stored as parts with the version's own header (d1.parts_header). From a copy of a real entry and
its two stored manifests (fixtures/parts/headers/), the fixture holds the header of a part the
earlier version wrote under an older register entry, the newer version's manifest as the build
writes it (build.version_manifest), the dataset's part of the header as fields.json gives it
(d1.dataset_header), and the header D1 loads the newer version with, so the function's header for
a parts answer is held to D1's own code. Run `python -m tests.test_parts_header_fixture` in
pipeline/ to write it again.
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
    old, new = store.manifests(SOURCES / "store", SLUG)
    # The earlier version, written under an entry since retitled, with an as_at of its own.
    then = dataclasses.replace(ds, title=f"{ds.title} (as first registered)")
    old = dataclasses.replace(old, as_at="2026-06-30")
    part = dict(header(then, old, 30, f"{SITE}/d/{SLUG}/v/{old.version}/parts/2019.parquet"))
    part["period"] = {"field": "year", "grain": "year", "value": "2019"}
    tbl = SimpleNamespace(
        rows=ROWS,
        unknown_columns=[],
        suppressed_cells=0,
        short_rows=0,
        omitted_columns=[],
        places=None,
    )
    split = {"period": {"field": "year", "grain": "year"}, "whole": False, "parts": []}
    built = version_manifest(
        ds,
        new,
        cast("Table", tbl),
        split,  # type: ignore[arg-type]  # the parts are left out; the header reads none of them
        f"{SITE}/d/{SLUG}/v/{new.version}/",
    )
    # d1 reads the built manifest back from the build's tree, as here.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps(built), encoding="utf-8")
        loaded = d1.parts_header(ds, store.Manifest.read(path), built)
    return {
        "slug": SLUG,
        "version": new.version,
        "part": part,
        "manifest": built,
        "dataset": d1.dataset_header(ds, new, {"rows": ROWS}),
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
    part, loaded = h["part"], h["d1"]
    assert isinstance(part, dict)
    assert isinstance(loaded, dict)
    for k in ("version", "as_at", "url", "title", "attribution", "cite", "source", "rows"):
        assert part[k] != loaded[k], k
    assert "period" in part
    assert "period" not in loaded


if __name__ == "__main__":
    FIXTURE.write_text(text(), encoding="utf-8")
