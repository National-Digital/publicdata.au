"""The provenance headers functions/_parts.test.mjs holds a parts answer to.

A finished part can be an earlier version's file, so its footer carries that version's header.
D1 loads a version stored as parts with the version's own header (d1._parts). From a copy of a
real entry and its two stored manifests (fixtures/parts/headers/), the fixture holds the header a
part written by the earlier version carries, the newer version's manifest as the build writes it,
and the header D1 loads the newer version with, so the function's header for a parts answer can be
held to D1's. Run
`python -m tests.test_parts_header_fixture` in pipeline/ to write it again.
"""

import json
from pathlib import Path

from publicdata import SITE, store
from publicdata.provenance import attribution, header
from publicdata.register import load

HERE = Path(__file__).parent / "fixtures" / "parts"
FIXTURE = HERE / "headers.json"
SOURCES = HERE / "headers"
SLUG = "au-eucalypt-records"
ROWS = 1234


def headers() -> dict[str, object]:
    (ds,) = load(SOURCES / "register")
    old, new = store.manifests(SOURCES / "store", SLUG)
    part = dict(header(ds, old, 30, f"{SITE}/d/{SLUG}/v/{old.version}/parts/2019.parquet"))
    part["period"] = {"field": "year", "grain": "year", "value": "2019"}
    manifest = {
        **json.loads(new.to_json()),
        "rows": ROWS,
        "attribution": attribution(ds, new),
    }
    d1 = header(ds, new, ROWS, f"{SITE}/d/{SLUG}/v/{new.version}/data.duckdb")
    return {"slug": SLUG, "version": new.version, "part": part, "manifest": manifest, "d1": d1}


def text() -> str:
    return json.dumps(headers(), indent=2, ensure_ascii=False) + "\n"


def test_the_parts_header_fixture_is_current() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == text(), (
        "run python -m tests.test_parts_header_fixture in pipeline/"
    )


def test_the_part_header_differs_from_the_version_header_where_the_function_must_fix_it() -> None:
    h = headers()
    part, d1 = h["part"], h["d1"]
    assert isinstance(part, dict)
    assert isinstance(d1, dict)
    for k in ("version", "url", "attribution", "cite", "source", "rows"):
        assert part[k] != d1[k], k
    assert "period" in part
    assert "period" not in d1


if __name__ == "__main__":
    FIXTURE.write_text(text(), encoding="utf-8")
