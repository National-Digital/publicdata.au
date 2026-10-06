"""The Parquet files the functions' tests read, written by the pipeline's own writer.

rows.parquet differs from a published version only in its row-group size, so 40 rows make five
groups. rows-profiled.parquet is the same rows under the query profile the MCP server reads:
sorted, with the sort recorded in the footer and a page index of two-row pages. Run
`python -m tests.test_query_fixture` in pipeline/ to write them again.
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
    out = {k: [] for k in FIELDS}
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
    return out


def write(path: Path, profiled: bool = False) -> None:
    data = rows()
    table = pa.table({k: pa.array(data[k], ARROW_TYPES[t]) for k, t in FIELDS.items()})
    extra = {"row_group_size": GROUP_ROWS}
    if profiled:
        table = table.sort_by([(k, "ascending", "at_start") for k in SORT])
        extra |= {
            "sorting_columns": [
                pq.SortingColumn(table.schema.get_field_index(k), nulls_first=True) for k in SORT
            ],
            "write_page_index": True,
            "max_rows_per_page": 2,
        }
    write_table = writer.pq.write_table
    writer.pq = SimpleNamespace(write_table=lambda *a, **k: write_table(*a, **{**k, **extra}))
    try:
        writer.write_parquet(SimpleNamespace(table=table), HEADER, path)
    finally:
        writer.pq = pq


def test_the_committed_fixtures_are_what_the_writer_writes(tmp_path):
    for committed, profiled in ((FIXTURE, False), (PROFILED, True)):
        fresh = tmp_path / committed.name
        write(fresh, profiled)
        a, b = pq.ParquetFile(committed), pq.ParquetFile(fresh)
        assert a.metadata.num_row_groups == b.metadata.num_row_groups == 5
        assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
        assert a.read().equals(b.read())
        assert bool(a.metadata.row_group(0).sorting_columns) == profiled


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    write(FIXTURE)
    write(PROFILED, profiled=True)
