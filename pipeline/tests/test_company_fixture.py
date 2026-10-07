"""A fixture shaped like the ASIC company register, which functions/_companies.test.mjs reads.

The rows are made up. As in the register, ACNs run with the registration date, a few companies
registered this year hold an old number, a company that changed its name has a row per name with
the current name marked true and the others blank, and a few rows have no registration date.

data.parquet is the published file, in the publisher's order. profiled.parquet is the query
copy under the profile: ordered by ACN, then the publisher's position, with a page index. Both are
shrunk to row groups of 1,000 rows and pages of 100, so a small file has groups and pages to
prune. rollup.json.gz is the rollup the deploy builds from the published file, with the cube the
register declares for it, and expected.json holds DuckDB's answers to the questions the functions'
tests ask of it. Run `python -m tests.test_company_fixture` in pipeline/ to write them again.
"""

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from publicdata import rollup
from publicdata.normalise import ARROW_TYPES
from publicdata.serialise.writers import parquet as writer

DIR = Path(__file__).parent / "fixtures" / "companies"
DATA, PROFILED = DIR / "data.parquet", DIR / "profiled.parquet"
ROLLUP, EXPECTED = DIR / "rollup.json.gz", DIR / "expected.json"
SLUG, VERSION = "companies", "2026-10-05"
ROWS = 6_000
GROUP_ROWS, PAGE_ROWS = 1_000, 100
FIELDS = {
    "company_name": "string",
    "acn": "string",
    "type": "string",
    "status": "string",
    "registration_date": "date",
    "current_name_indicator": "boolean",
}
DECLARED = (("type", "registration_date", "current_name_indicator"),)
HEADER = {
    "dataset": SLUG,
    "version": VERSION,
    "licence": {"id": "CC-BY-3.0-AU"},
    "attribution": "Fixture register, licensed under CC BY 3.0 AU.",
    "source": {"sha256": "0" * 64},
}
TYPES = ["APTY"] * 16 + ["APUB", "APUB", "FNOS", "RACN", "CCIV"]
STATUSES = ["REGD"] * 8 + ["DRGD", "DRGD", "SOFF", "EXAD"]


def rows() -> list[dict]:
    out = []
    company = 0
    while len(out) < ROWS:
        company += 1
        # Every 60 days from 1960, so a day holds a dozen companies, and the newest sixth all
        # registered on one day in 2024, so a year's matches fill the end of the ACN order.
        day = dt.date(1960, 1, 1) + dt.timedelta(days=60 * min(company * 400 // 4_000, 390))
        acn = f"{company * 7:09d}"
        # A company registered this year that holds an old number, as a re-registered one does.
        if company % 211 == 0:
            day = dt.date(2024, 1 + company % 12, 1 + company % 27)
        names = 1 + (company % 5 == 0) + (company % 13 == 0)
        for n in range(names):
            out.append(
                {
                    "company_name": f"Company {company} name {n}",
                    "acn": acn,
                    "type": TYPES[company % len(TYPES)],
                    "status": STATUSES[company % len(STATUSES)],
                    "registration_date": None if company % 401 == 0 else day,
                    "current_name_indicator": True if n == names - 1 else None,
                }
            )
    out = out[:ROWS]
    # The publisher lists companies by name, so its order is not the ACN's.
    return sorted(out, key=lambda r: (r["company_name"][::-1], r["acn"]))


def _table(data: list[dict]) -> pa.Table:
    return pa.table({k: pa.array([r[k] for r in data], ARROW_TYPES[t]) for k, t in FIELDS.items()})


def write(path: Path, profiled: bool) -> None:
    data = rows()
    table = _table(data)
    extra = {"row_group_size": GROUP_ROWS}
    if profiled:
        order = sorted(range(len(data)), key=lambda i: (data[i]["acn"], i))
        table = table.take(pa.array(order))
        extra |= {
            "max_rows_per_page": PAGE_ROWS,
            "sorting_columns": [pq.SortingColumn(table.schema.get_field_index("acn"))],
            "write_page_index": True,
        }

    def write_table(t, where, **k):
        if profiled:
            t = t.replace_schema_metadata({**t.schema.metadata, b"publicdata.profile": b"1"})
        pq.write_table(t, where, **{**k, **extra})

    writer.pq = SimpleNamespace(write_table=write_table)
    try:
        writer.write_parquet(SimpleNamespace(table=table), HEADER, path)
    finally:
        writer.pq = pq


def dataset():
    return SimpleNamespace(
        slug=SLUG,
        kind="table",
        query=False,
        example=None,
        chart={"where": ({"field": "current_name_indicator", "op": "eq", "value": True},)},
        rollup=DECLARED,
        fields=tuple(SimpleNamespace(name=n, type=t) for n, t in FIELDS.items()),
    )


def build_rollup(path: Path) -> tuple[rollup.Plan, bytes]:
    run, con = rollup.parquet_run(path)
    try:
        return rollup.make(dataset(), run, ROWS, rollup.parquet_header(path), SLUG, VERSION)
    finally:
        con.close()


# The questions the functions' tests ask, as count_rows and query_rows take them.
COUNTS = [
    ({"type": "APTY", "registration_date": {"min": "2024-01-01", "max": "2024-12-31"}}, []),
    (
        {
            "type": "APTY",
            "registration_date": {"min": "2024-01-01", "max": "2024-12-31"},
            "current_name_indicator": True,
        },
        [],
    ),
    ({"registration_date": {"min": "2024-01-01"}, "current_name_indicator": None}, ["type"]),
    ({}, ["status"]),
    ({"registration_date": None}, []),
    ({"type": ["APUB", "FNOS"], "registration_date": {"max": "1990-12-31"}}, ["type"]),
    ({"registration_date": {"min": "1999-01-01", "max": "2004-06-30"}}, ["current_name_indicator"]),
    ({"acn": "000000105"}, []),
]
ROW_QUERIES = [
    (
        {"type": "APTY", "registration_date": {"min": "2024-01-01", "max": "2024-12-31"}},
        ["acn", "company_name", "type", "registration_date"],
        20,
    ),
    ({"acn": "000000105"}, list(FIELDS), 50),
    ({"status": "DRGD"}, ["acn", "company_name", "status"], 20),
]


def _where(where: dict) -> str:
    out = []
    for k, w in where.items():
        if w is None:
            out.append(f'"{k}" IS NULL')
        elif isinstance(w, list):
            out.append(f'"{k}" IN (' + ", ".join(f"'{x}'" for x in w) + ")")
        elif isinstance(w, dict):
            out += [f"\"{k}\" >= '{w['min']}'"] if "min" in w else []
            out += [f"\"{k}\" <= '{w['max']}'"] if "max" in w else []
        elif isinstance(w, bool):
            out.append(f'"{k}" = {str(w).lower()}')
        else:
            out.append(f"\"{k}\" = '{w}'")
    return " WHERE " + " AND ".join(out) if out else ""


def _cell(v):
    return v.isoformat() if isinstance(v, dt.date) else int(v) if isinstance(v, bool) else v


def expected(path: Path) -> dict:
    """DuckDB's answers from the published file. Rows come in the order the profile file holds
    them, which the DuckDB SQL an answer cites reproduces: the sort, then the file's position."""
    import duckdb

    con = duckdb.connect()
    src = f"read_parquet('{path.as_posix()}', file_row_number = true)"
    counts = []
    for where, group in COUNTS:
        g = ", ".join(f'"{x}"' for x in group)
        sel = f"{g + ', ' if g else ''}COUNT(*)"
        # count_rows breaks ties in group order, and SQLite puts nulls first.
        by = ", ".join(f'"{x}" ASC NULLS FIRST' for x in group)
        tail = f" GROUP BY {g} ORDER BY COUNT(*) DESC, {by}" if g else ""
        got = con.execute(f"SELECT {sel} FROM {src}{_where(where)}{tail}").fetchall()
        groups = [
            {**dict(zip(group, map(_cell, r[:-1]), strict=True)), "count": r[-1]} for r in got
        ]
        total = con.execute(f"SELECT COUNT(*) FROM {src}{_where(where)}").fetchone()[0]
        counts.append({"where": where, "group_by": group, "groups": groups, "matched": total})
    rows_ = []
    for where, select, limit in ROW_QUERIES:
        cols = ", ".join(f'"{c}"' for c in select)
        q = f"SELECT {cols} FROM {src}{_where(where)} ORDER BY acn, file_row_number LIMIT {limit}"
        got = [dict(zip(select, map(_cell, r), strict=True)) for r in con.execute(q).fetchall()]
        total = con.execute(f"SELECT COUNT(*) FROM {src}{_where(where)}").fetchone()[0]
        rows_.append(
            {"where": where, "select": select, "limit": limit, "rows": got, "matched": total}
        )
    return {"slug": SLUG, "version": VERSION, "counts": counts, "rows": rows_}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def test_the_committed_company_fixture_is_what_the_writers_write(tmp_path):
    for committed, profiled in ((DATA, False), (PROFILED, True)):
        fresh = tmp_path / committed.name
        write(fresh, profiled)
        a, b = pq.ParquetFile(committed), pq.ParquetFile(fresh)
        assert a.metadata.num_row_groups == b.metadata.num_row_groups == ROWS // GROUP_ROWS
        assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
        assert a.read().equals(b.read())
    p, body = build_rollup(DATA)
    assert body == ROLLUP.read_bytes()
    assert EXPECTED.read_text(encoding="utf-8") == _json(expected(DATA))


def test_the_declared_cube_is_built_and_answers_the_type_and_year_count():
    p, _ = build_rollup(DATA)
    assert tuple(sorted(DECLARED[0])) in p.cubes
    first = expected(DATA)["counts"][0]
    assert first["matched"] > 0 and first["groups"] == [{"count": first["matched"]}]


if __name__ == "__main__":
    DIR.mkdir(parents=True, exist_ok=True)
    write(DATA, profiled=False)
    write(PROFILED, profiled=True)
    ROLLUP.write_bytes(build_rollup(DATA)[1])
    EXPECTED.write_text(_json(expected(DATA)), encoding="utf-8")
