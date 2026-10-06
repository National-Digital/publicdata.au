"""The Parquet files the functions' tests read, written by the pipeline's own writer.

rows.parquet is a file from before the query profile, with 40 rows in five groups. The other two
are the same rows under the profile the MCP server reads (ADR 0008): the profile key in the
footer and year narrowed to INT32. rows-profiled.parquet is sorted, with the sort recorded and a
page index of two-row pages; rows-profiled-unsorted.parquet keeps the publisher's order and has
no page index. Only the row-group and page sizes are shrunk, so a small file has groups and
pages to prune. Run `python -m tests.test_query_fixture` in pipeline/ to write them again.
"""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from publicdata.normalise import ARROW_TYPES
from publicdata.serialise.writers import parquet as writer

FIXTURE = Path(__file__).parent / "fixtures" / "parquet" / "rows.parquet"
PROFILED = FIXTURE.with_name("rows-profiled.parquet")
UNSORTED = FIXTURE.with_name("rows-profiled-unsorted.parquet")
LARGE = FIXTURE.with_name("large-profiled.parquet")
BARE = FIXTURE.with_name("rows-no-provenance.parquet")
LARGE_ROWS = 20_000_000
PROFILE = {b"publicdata.profile": b"1"}
INT32 = ["year"]
GROUP_ROWS = 8
SORT = ["year", "lga"]
FIELDS = {
    "lga": "string",
    "year": "integer",
    "fatal": "boolean",
    "speed": "number",
    "day": "date",
    "seen": "datetime",
    "ref": "integer",
}
HEADER = {
    "dataset": "crashes",
    "version": "2026-04-24",
    "licence": {"id": "CC-BY-4.0"},
    "attribution": "Fixture publisher, licensed under CC BY 4.0.",
}


def rows() -> dict[str, list]:
    places = ["Brisbane", "Gold Coast", "Logan", "Cairns", None]
    out = {k: [] for k in [*FIELDS, "suppressed"]}
    for i in range(40):
        out["lga"].append(places[i % 5])
        out["year"].append(2018 + i // GROUP_ROWS)
        out["fatal"].append(None if i % 7 == 6 else i % 3 == 0)
        out["speed"].append(None if i % 6 == 5 else 40 + 10 * (i % 5) + 0.5 * (i % 2))
        # Blank with the place, so a sorted page of dates and times can be wholly null.
        blank = places[i % 5] is None
        out["day"].append(
            None if blank else dt.date(2018 + i // GROUP_ROWS, 1 + i % 12, 1 + i % 28)
        )
        out["seen"].append(None if blank else dt.datetime(2026, 4, 24, i % 24, i, 5))
        # Above 2**53, so a reader must keep it as text to keep it exact.
        out["ref"].append(2**53 + i if i % 10 == 9 else 1000 * i)
        # The flags normalise writes for cells the publisher suppressed, such as "<5".
        out["suppressed"].append(
            [f for f, v in (("fatal", out["fatal"][-1]), ("speed", out["speed"][-1])) if v is None]
        )
    return out


def write(path: Path, profiled: bool = False, sort: bool = True, provenance: bool = True) -> None:
    data = rows()
    table = pa.table(
        {
            **{k: pa.array(data[k], ARROW_TYPES[t]) for k, t in FIELDS.items()},
            "suppressed": pa.array(data["suppressed"], pa.list_(pa.string())),
        }
    )
    extra = {"row_group_size": GROUP_ROWS}
    if profiled:
        table = table.cast(
            pa.schema([f.with_type(pa.int32()) if f.name in INT32 else f for f in table.schema])
        )
        extra["max_rows_per_page"] = 2
    if profiled and sort:
        table = table.sort_by([(k, "ascending", "at_end") for k in SORT])
        extra |= {
            "sorting_columns": [
                pq.SortingColumn(table.schema.get_field_index(k), nulls_first=False) for k in SORT
            ],
            "write_page_index": True,
        }

    def write_table(t, where, **k):
        if profiled:
            meta = {**t.schema.metadata, **PROFILE}
            if not provenance:
                meta.pop(b"publicdata")
            t = t.replace_schema_metadata(meta)
        pq.write_table(t, where, **{**k, **extra})

    writer.pq = SimpleNamespace(write_table=write_table)
    try:
        writer.write_parquet(SimpleNamespace(table=table), HEADER, path)
    finally:
        writer.pq = pq


def write_large(path: Path) -> None:
    """Twenty million rows of one constant column, in the profile's 500,000-row groups, so a
    reader that holds one index per row runs out of a small heap."""
    table = pa.table({"n": pa.array([0] * LARGE_ROWS, pa.int32())})
    table = table.replace_schema_metadata(
        {b"publicdata": b'{"attribution": "Large fixture."}', **PROFILE}
    )
    pq.write_table(table, path, compression="zstd", row_group_size=500_000, write_statistics=True)


def test_the_committed_fixtures_are_what_the_writer_writes(tmp_path):
    for committed, profiled, sort in (
        (FIXTURE, False, False),
        (PROFILED, True, True),
        (UNSORTED, True, False),
    ):
        fresh = tmp_path / committed.name
        write(fresh, profiled, sort)
        a, b = pq.ParquetFile(committed), pq.ParquetFile(fresh)
        assert a.metadata.num_row_groups == b.metadata.num_row_groups == 5
        assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
        assert a.read().equals(b.read())
        assert bool(a.metadata.row_group(0).sorting_columns) == (profiled and sort)
        assert (b"publicdata.profile" in a.schema_arrow.metadata) == profiled
    large = pq.ParquetFile(LARGE)
    assert large.metadata.num_rows == LARGE_ROWS
    assert b"publicdata.profile" in large.schema_arrow.metadata
    assert b"publicdata" not in pq.ParquetFile(BARE).schema_arrow.metadata


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    write(FIXTURE)
    write(PROFILED, profiled=True)
    write(UNSORTED, profiled=True, sort=False)
    write_large(LARGE)
    write(BARE, profiled=True, provenance=False)
