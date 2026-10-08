"""The period parts the functions' tests read, written by the pipeline's own part writer
(publicdata.parts.write) under the Parquet profile, with each version's manifest.

Two datasets hold the same 150 rows. crashes-by-year is split on year, an INT32 integer, sorted
by place and day with a page index, and its newest version takes three finished parts unchanged
from the version before, so their files sit under that version's folder; it revises one row of
2019, so that part is its own and the parts' URLs do not sort in period order. crashes-by-quarter is
split on day, a date, by quarter, keeps the publisher's order with no page index, and has an
undated part for the rows without a day. Only the row-group and page sizes are shrunk, so a small
part has groups and pages to prune. Run `python -m tests.test_parts_fixture` in pipeline/ to
write them again.
"""

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from publicdata import parts
from publicdata.normalise import ARROW_TYPES, Table
from publicdata.register import Period
from publicdata.serialise import profile

ROOT = Path(__file__).parent / "fixtures" / "parts"
ROWS = 150
FIELDS = {
    "lga": "string",
    "year": "integer",
    "fatal": "boolean",
    "speed": "number",
    "day": "date",
    "seen": "datetime",
    "ref": "integer",
}
ATTRIBUTION = "Fixture publisher, licensed under CC BY 4.0."
# slug: the period, the layout, rows to a group, rows to a page, and the versions written, each
# with the years it holds and the year it revises.
SETS = {
    "crashes-by-year": (
        Period("year", "year"),
        {"sort": ["lga", "day"], "key": [], "lookup": ["lga"], "int32": ["year"]},
        5,
        2,
        (("2026-03-01", range(2018, 2022), None), ("2026-04-01", range(2018, 2023), 2019)),
    ),
    "crashes-by-quarter": (
        Period("day", "quarter"),
        {"sort": [], "key": [], "lookup": [], "int32": ["year"]},
        4,
        4,
        (("2026-04-01", range(2018, 2023), None),),
    ),
}


def rows(revise: int | None = None) -> pa.Table:
    places = ["Brisbane", "Gold Coast", "Logan", "Cairns", "Éden Park", None]
    out = {k: [] for k in [*FIELDS, "suppressed"]}
    for i in range(ROWS):
        year = 2018 + (i * 7) % 5
        out["lga"].append(places[i % 6])
        out["year"].append(year)
        out["fatal"].append(None if i % 7 == 6 else i % 3 == 0)
        out["speed"].append(None if i % 6 == 5 else 40 + 10 * (i % 5) + 0.5 * (i % 2))
        if year == revise and i < 5:
            out["speed"][-1] = 99.0
        out["day"].append(None if i % 11 == 4 else dt.date(year, 1 + (i * 5) % 12, 1 + i % 28))
        out["seen"].append(None if i % 13 == 7 else dt.datetime(2026, 4, 24, i % 24, i % 60, 5))
        # Above 2**53, so a reader must keep it as text to keep it exact.
        out["ref"].append(2**53 + i if i % 10 == 9 else 1000 * i)
        # The flags normalise writes for cells the publisher suppressed, such as "<5".
        out["suppressed"].append(
            [f for f, v in (("fatal", out["fatal"][-1]), ("speed", out["speed"][-1])) if v is None]
        )
    return pa.table(
        {
            **{k: pa.array(out[k], ARROW_TYPES[t]) for k, t in FIELDS.items()},
            "suppressed": pa.array(out["suppressed"], pa.list_(pa.string())),
        }
    )


def write(root: Path, slug: str) -> None:
    import pyarrow.compute as pc

    per, lay, group, page, versions = SETS[slug]
    lay = {"profile": profile.VERSION, **lay}
    was = (parts.FORMATS, profile.ROW_GROUP_ROWS, profile.PAGE_ROWS)
    parts.FORMATS, profile.ROW_GROUP_ROWS, profile.PAGE_ROWS = ("parquet",), group, page
    prior: list[dict] = []
    try:
        for version, years, revise in versions:
            every = rows(revise)
            t = every.filter(pc.is_in(every.column("year"), pa.array(list(years), pa.int64())))
            m = SimpleNamespace(version=version, volatile=[], parquet=lay)
            tbl = Table(dataset=SimpleNamespace(slug=slug), manifest=m, table=t)
            vdir = root / slug / "v" / version

            def hdr(n: int, rel: str, version=version) -> dict:
                return {
                    "dataset": slug,
                    "version": version,
                    "rows": n,
                    "url": f"https://publicdata.au/d/{slug}/v/{version}/{rel}",
                    "licence": {"id": "CC-BY-4.0"},
                    "attribution": ATTRIBUTION,
                }

            recs, _ = parts.write(tbl, t, per, hdr, vdir, version, prior)
            man = {
                "dataset": slug,
                "version": version,
                "rows": t.num_rows,
                "period": {"field": per.field, "grain": per.grain, "revision_window": 2},
                "whole": False,
                "parts": recs,
                "attribution": ATTRIBUTION,
            }
            (vdir / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
            prior = recs
    finally:
        parts.FORMATS, profile.ROW_GROUP_ROWS, profile.PAGE_ROWS = was


def test_the_committed_parts_are_what_the_part_writer_writes(tmp_path):
    for slug in SETS:
        write(tmp_path, slug)
        fresh = sorted(p.relative_to(tmp_path) for p in (tmp_path / slug).rglob("*") if p.is_file())
        committed = sorted(p.relative_to(ROOT) for p in (ROOT / slug).rglob("*") if p.is_file())
        assert fresh == committed
        for rel in committed:
            if rel.suffix == ".json":
                assert (ROOT / rel).read_text("utf-8") == (tmp_path / rel).read_text("utf-8")
                continue
            a, b = pq.ParquetFile(ROOT / rel), pq.ParquetFile(tmp_path / rel)
            assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
            assert a.read().equals(b.read())
            assert a.metadata.num_row_groups == b.metadata.num_row_groups


def test_every_part_follows_the_profile_and_carries_its_provenance():
    for slug, (per, lay, *_rest) in SETS.items():
        man = json.loads(next((ROOT / slug).glob("v/*/manifest.json")).read_text("utf-8"))
        lay = {"profile": profile.VERSION, **lay}
        for path in (ROOT / slug).rglob("*.parquet"):
            meta = pq.read_metadata(path)
            assert profile.follows(meta, lay), path
            head = json.loads(meta.metadata[b"publicdata"])
            assert head["attribution"] == ATTRIBUTION
            assert head["period"]["field"] == per.field
            assert meta.row_group(0).column(0).compression == "ZSTD"
        assert sum(p["rows"] for p in man["parts"]) == man["rows"]


if __name__ == "__main__":
    import shutil

    for slug in SETS:
        shutil.rmtree(ROOT / slug, ignore_errors=True)
        write(ROOT, slug)
