"""The SQL the MCP server gives when it refuses a call over period parts answers the call.

The DuckDB SQL it gives for a version stored as period parts answers as the server would have: the
same rows in the same order, and the same groups. The server's engine (functions/_parquet.js) runs
under node over the committed part fixtures, and DuckDB runs its SQL over the same files.
"""

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import duckdb
import pytest

if TYPE_CHECKING:
    from publicdata.jsontypes import JSONObject

ROOT = Path(__file__).resolve().parents[2]
PARTS = Path(__file__).parent / "fixtures" / "parts"
SITE = "https://publicdata.au/"
CASES = [
    ("crashes-by-year", "rows", "lga=eq.Logan&order=day.desc&limit=7&offset=2"),
    ("crashes-by-year", "rows", "year=gte.2020&select=lga,year,ref&limit=40&offset=5"),
    ("crashes-by-year", "rows", "order=speed.desc,lga.asc&select=lga,speed,suppressed&limit=60"),
    ("crashes-by-year", "rows", "fatal=eq.true&year=in.(2018,2022)&limit=100"),
    ("crashes-by-year", "rows", "select=year,lga,speed&limit=50&offset=20"),
    ("crashes-by-quarter", "rows", "day=gte.2019-04-01&day=lt.2020-01-01&order=lga.asc&limit=30"),
    ("crashes-by-quarter", "rows", "limit=50&offset=40&select=day,seen,lga"),
    ("crashes-by-quarter", "rows", "day=is.null&select=lga,seen"),
    ("crashes-by-year", "aggregate", "group=lga&metric=count,sum.speed&year=gte.2019"),
    ("crashes-by-quarter", "aggregate", "group=year&metric=min.day,max.seen,count.fatal"),
    (
        "crashes-by-quarter",
        "aggregate",
        "group=lga,fatal&metric=count&order=count.desc,lga.asc,fatal.asc&limit=5",
    ),
]

SCRIPT = """
import { readFileSync } from 'node:fs';
import { BUDGET, openVersion, parquetAggregate, parquetRows } from '%(engine)s';
const root = %(root)s, cases = %(cases)s;
const DIST = {
  async get(key, o = {}) {
    let b;
    try { b = readFileSync(root + key.replace(/^d\\//, '')); } catch { return null; }
    const r = o.range || {};
    const s = r.suffix !== undefined ? Math.max(0, b.length - r.suffix) : r.offset || 0;
    const e = r.suffix !== undefined ? b.length : r.length !== undefined ? s + r.length : b.length;
    const u = b.subarray(s, e);
    return { size: b.length, etag: 'e', arrayBuffer: async () => u.buffer.slice(u.byteOffset, u.byteOffset + u.length), text: async () => u.toString('utf8') };
  },
  async head() { return { etag: 'e' }; },
};
const env = { DIST };
const out = [];
for (const [slug, op, qs] of cases) {
  const at = await openVersion(env, slug, '2026-04-01');
  const fn = op === 'rows' ? parquetRows : parquetAggregate;
  const url = `https://publicdata.au/d/${slug}/v/2026-04-01/manifest.json`;
  const got = await fn(env, at, new URLSearchParams(qs), url);
  let sql = null;
  try { await fn(env, at, new URLSearchParams(qs), url, { ...BUDGET, groups: 0, parts: 0 }); } catch (e) { sql = e.message.slice(e.message.indexOf('SELECT ')); }
  out.push({ rows: got.rows, sql });
}
console.log(JSON.stringify(out));
"""


class Answer(TypedDict):
    """What the engine gives for one case: its rows, and the SQL it gives when it refuses."""

    rows: list[JSONObject]
    sql: str | None


def _engine() -> list[Answer]:
    node = shutil.which("node")
    if not node or not (ROOT / "node_modules" / "hyparquet").is_dir():
        pytest.skip("node and the functions' packages (npm ci) are needed")
    code = SCRIPT % {
        "engine": (ROOT / "functions" / "_parquet.js").as_uri(),
        "root": json.dumps(str(PARTS) + "/"),
        "cases": json.dumps(CASES),
    }
    r = subprocess.run(
        [node, "--input-type=module", "-e", code], capture_output=True, text=True, check=True
    )
    got: list[Answer] = json.loads(r.stdout)
    return got


def _as_d1(v: object) -> object:
    """A DuckDB value as the engine gives it.

    A date or a time is ISO text, a boolean is 1 or 0, and an integer beyond 2**53 is its digits.
    """
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, dt.datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, int) and abs(v) > 2**53:
        return str(v)
    if isinstance(v, float):
        return round(v, 9)
    return v


def test_the_refusal_sql_over_parts_answers_as_the_engine_does() -> None:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    for (slug, _op, qs), got in zip(CASES, _engine(), strict=True):
        assert got["sql"], (slug, qs)
        sql = got["sql"].replace(SITE + "d/", str(PARTS) + "/")
        assert "https://" not in sql
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        want = [{n: _as_d1(v) for n, v in zip(names, row, strict=True)} for row in cur.fetchall()]
        rows = [{n: _as_d1(v) for n, v in r.items()} for r in got["rows"]]
        assert rows == want, (slug, qs, sql)
        assert rows, (slug, qs)
