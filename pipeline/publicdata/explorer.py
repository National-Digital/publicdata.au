"""The in-browser explorer: Perspective over DuckDB-WASM, vendored from the npm lockfile.

Each dataset gets a first dashboard drawn from the same field hints as the query console.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE_MODULES = ROOT / "node_modules"
CACHE = NODE_MODULES / ".cache" / "publicdata"
# DuckDB-WASM 1.32.0 is DuckDB v1.4.3 and asks its extension repository for this path. Parquet
# is an extension in the WASM build, so the signed binary is served from this site.
DUCKDB_VERSION = "v1.4.3"
EXTENSIONS = {"parquet": "22765c8f7dc741cda2b571a66ac7bb355295d7d69a6c37e5315b265672984f55"}
EXTENSION_URL = "https://extensions.duckdb.org/{v}/wasm_eh/{name}.duckdb_extension.wasm"
COPY = (
    "@perspective-dev/client/dist/cdn/perspective.js",
    "@perspective-dev/client/dist/wasm/perspective-js.wasm",
    "@perspective-dev/server/dist/wasm/perspective-server.wasm",
    "@perspective-dev/server/dist/wasm/perspective-server.memory64.wasm",
    "@perspective-dev/viewer/dist/cdn/perspective-viewer.js",
    "@perspective-dev/viewer/dist/wasm/perspective-viewer.wasm",
    "@perspective-dev/viewer/dist/css/pro.css",
    "@perspective-dev/viewer/dist/css/pro-dark.css",
    "@perspective-dev/viewer-datagrid/dist/cdn/perspective-viewer-datagrid.js",
    "@perspective-dev/viewer-charts/dist/cdn/perspective-viewer-charts.js",
    "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js",
)
# Over the Pages per-file limit as it stands; the browser inflates it with DecompressionStream.
GZIP = ("@duckdb/duckdb-wasm/dist/duckdb-eh.wasm",)
LICENCES = (
    "@perspective-dev/client/LICENSE.md",
    "apache-arrow/LICENSE.txt",
)
# What the browser loads; the gate refuses an explorer page whose vendor tree lacks one.
REQUIRED = (
    "duckdb.js",
    *COPY,
    *(f"{rel}.gz" for rel in GZIP),
    *(f"duckdb-extensions/{DUCKDB_VERSION}/wasm_eh/{n}.duckdb_extension.wasm" for n in EXTENSIONS),
)
# The explorer holds the whole Parquet in the browser, so larger files get no explorer page.
MAX_PARQUET = 100 * 1024 * 1024
TABLE = "memory.records"
INT32 = 2**31


def _need_node_modules() -> None:
    if not (NODE_MODULES / "@perspective-dev" / "viewer" / "package.json").exists():
        msg = "The explorer needs its browser libraries: run `npm ci` at the repository root."
        raise SystemExit(msg)


def _extension(name: str, sha: str) -> bytes:
    p = CACHE / "extensions" / DUCKDB_VERSION / f"{name}.duckdb_extension.wasm"
    if not p.exists():
        req = urllib.request.Request(  # noqa: S310 - a fixed https URL, checked by its hash
            EXTENSION_URL.format(v=DUCKDB_VERSION, name=name),
            headers={"User-Agent": "publicdata.au build"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - a fixed https URL
            data = r.read()
        if hashlib.sha256(data).hexdigest() != sha:
            msg = f"DuckDB {name} extension does not match its pinned SHA-256"
            raise SystemExit(msg)
        p.parent.mkdir(parents=True, exist_ok=True)
        # A parallel build may fetch the same file, so each writes its own and swaps it in whole.
        fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=p.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    data = p.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        msg = f"cached DuckDB {name} extension does not match its pinned SHA-256"
        raise SystemExit(msg)
    return data


def _stage(dest: Path) -> None:
    for rel in COPY:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(NODE_MODULES / rel, dest / rel)
    for rel in GZIP:
        data = (NODE_MODULES / rel).read_bytes()
        (dest / f"{rel}.gz").write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    subprocess.run(
        [
            str(NODE_MODULES / ".bin" / "esbuild"),
            str(ROOT / "explorer" / "duckdb.js"),
            "--bundle",
            "--format=esm",
            "--minify",
            "--target=es2022",
            "--legal-comments=eof",
            f"--outfile={dest / 'duckdb.js'}",
            "--log-level=warning",
        ],
        check=True,
        cwd=ROOT,
    )
    for name, sha in EXTENSIONS.items():
        p = (
            dest
            / "duckdb-extensions"
            / DUCKDB_VERSION
            / "wasm_eh"
            / f"{name}.duckdb_extension.wasm"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_extension(name, sha))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    notice = [
        "Third-party libraries in this directory, as pinned in the site's package-lock.json.",
        "",
        *(
            f"{k.removeprefix('node_modules/')} {v['version']} ({v.get('license', 'see package')})"
            for k, v in sorted(lock.items())
            if k.startswith("node_modules/") and not v.get("dev")
        ),
        f"DuckDB {DUCKDB_VERSION} extensions: {', '.join(EXTENSIONS)} (MIT)",
        "",
    ]
    for rel in LICENCES:
        notice += [f"== {rel}", (NODE_MODULES / rel).read_text(encoding="utf-8"), ""]
    (dest / "LICENSES.txt").write_text("\n".join(notice), encoding="utf-8")


def vendor(out: Path) -> str:
    """Copy the explorer's libraries under a content-hashed path and return that path.

    The hash means a new library version never meets a browser's cached copy of the old one.
    """
    _need_node_modules()
    key = hashlib.sha256()
    for p in (ROOT / "package-lock.json", ROOT / "explorer" / "duckdb.js", Path(__file__)):
        key.update(p.read_bytes())
    staged = CACHE / "vendor" / key.hexdigest()[:16]
    if not (staged / ".done").exists():
        # Parallel builds stage the same key at once, so each stages into its own folder.
        staged.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=staged.name + ".", dir=staged.parent))
        _stage(tmp)
        (tmp / ".done").write_text("", encoding="utf-8")
        if staged.exists() and not (staged / ".done").exists():
            shutil.rmtree(staged, ignore_errors=True)
        try:
            tmp.rename(staged)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not (staged / ".done").exists():
                raise
    files = sorted(p for p in staged.rglob("*") if p.is_file() and p.name != ".done")
    h = hashlib.sha256()
    for p in files:
        h.update(
            str(p.relative_to(staged)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest()
        )
    rel = f"static/vendor/{h.hexdigest()[:12]}"
    for p in files:
        d = out / rel / p.relative_to(staged)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, d)
    return "/" + rel + "/"


YEAR = re.compile(r"(^|_)(year|yr)(_|$)")
CALENDAR = re.compile(r"(^|_)(month|day|day_of_week|weekday)(_|$)")


def labels(ds, console: dict) -> dict[str, str]:
    """The explorer's column names: each field's register label.

    Every menu, axis and legend then reads in words. The browser renames the columns as it loads
    the Parquet.
    """
    present = {e["name"] for e in console["fields"]}
    return {f.name: f.display for f in ds.fields if f.name in present}


def text_fields(console: dict) -> list[str]:
    """Year fields, which the browser loads as text.

    A chart then gives each year its own label instead of a numeric axis ("2.0K"), and a filter
    offers the years as a list.
    """
    return [
        e["name"] for e in console["fields"] if e["type"] == "integer" and YEAR.search(e["name"])
    ]


def yes_no_fields(console: dict) -> list[str]:
    """True-or-false fields, which the browser shows as Yes and No."""
    return [e["name"] for e in console["fields"] if e["type"] == "boolean"]


# A chart groups by a text field with this many values, and colours by one with fewer.
CATEGORY_MIN, CATEGORY_MAX = 2, 30
SPLIT_MIN, SPLIT_MAX = 3, 8


def categories(console: dict) -> list[str]:
    """Text fields with a short list of values, which a chart can group by."""
    return [
        e["name"]
        for e in console["fields"]
        if e["type"] == "string"
        and CATEGORY_MIN <= len(e.get("values") or ()) <= CATEGORY_MAX
        and not CALENDAR.search(e["name"])
    ]


def split_field(console: dict) -> str | None:
    """The field colour carries: severity where the dataset has it, else a short list."""
    fields = {e["name"]: e for e in console["fields"]}
    cats = categories(console)
    return next(
        (n for n in cats if "severity" in n and len(fields[n]["values"]) <= SPLIT_MAX), None
    ) or next((n for n in cats if SPLIT_MIN <= len(fields[n]["values"]) <= SPLIT_MAX), None)


# The query API's aggregates as Perspective names them.
PERSPECTIVE_AGG = {"sum": "sum", "avg": "avg", "min": "low", "max": "high"}


def defaults(ds, console: dict) -> dict:
    """The first dashboard, drawn from the same field hints as the query console.

    It holds a stacked bar of the main category that filters the other panels, the same split
    over time, a heatmap of category by year, a map where the dataset has coordinates, and the
    rows.
    """
    fields = {e["name"]: e for e in console["fields"]}
    lab = labels(ds, console)
    word = lambda n: lab.get(n, n).lower()  # noqa: E731
    metric = console["example"]["metric"]
    if metric == "count":
        what = getattr(ds, "row_label", "") or "Rows"
        # A count is a column of its own, so it must not share a field's label.
        measure = what if what not in lab.values() else f"{what} (rows)"
        expressions, aggregates = {measure: "1"}, {measure: "sum"}
    else:
        fn, name = metric.split(".", 1)
        measure = lab.get(name, name)
        what, expressions, aggregates = measure, {}, {measure: PERSPECTIVE_AGG[fn]}
    cats = categories(console)
    split = split_field(console)
    group = (console["example"]["group"] or [None])[0]
    if group == split or group not in cats:
        group = next((n for n in cats if n != split and "region" in n), None) or next(
            (n for n in cats if n != split), None
        )
    if group is None:
        group, split = split, None
    when = next(
        (e["name"] for e in console["fields"] if e["type"] in ("date", "datetime")), None
    ) or next(iter(text_fields(console)), None)
    base = {
        "table": TABLE,
        "columns": [measure],
        "expressions": expressions,
        "aggregates": aggregates,
    }
    col = lambda n: lab.get(n, n)  # noqa: E731
    by = f" and {word(split)}" if split else ""
    panels = {}
    if group:
        panels["by-group"] = {
            **base,
            "plugin": "X Bar",
            "title": f"{what} by {word(group)}{by}",
            "group_by": [col(group)],
            "split_by": [col(split)] if split else [],
            "sort": [[measure, "desc"]],
        }
    if when:
        panels["over-time"] = {
            **base,
            "plugin": "Y Area" if split else "Y Line",
            "title": f"{what} by {word(when)}{by}",
            "group_by": [col(when)],
            "split_by": [col(split)] if split else [],
        }
        if group:
            panels["heatmap"] = {
                **base,
                "plugin": "Heatmap",
                "title": f"{what} by {word(group)} and {word(when)}",
                "group_by": [col(when)],
                "split_by": [col(group)],
            }
    elif group and split:
        panels["heatmap"] = {
            **base,
            "plugin": "Heatmap",
            "title": f"{what} by {word(group)} and {word(split)}",
            "group_by": [col(group)],
            "split_by": [col(split)],
        }
    geo = getattr(ds, "geometry", None) or {}
    if geo.get("lon") in fields and geo.get("lat") in fields:
        colour = split or group
        panels["map"] = {
            "table": TABLE,
            "plugin": "Map Scatter",
            "title": f"Map{f' by {word(colour)}' if colour else ''}",
            "columns": [col(geo["lon"]), col(geo["lat"]), *([col(colour)] if colour else [])],
            # The map colours categories in row order; sorting them matches the other charts.
            "sort": [[col(colour), "asc"]] if colour else [],
        }
    panels["rows"] = {"table": TABLE, "plugin": "Datagrid", "title": "Rows"}

    tab = lambda *ids: {"type": "tab-layout", "tabs": [i for i in ids if i in panels]}  # noqa: E731

    def row(*items, sizes=None):
        keep = [
            (i, z)
            for i, z in zip(items, sizes or [1] * len(items), strict=True)
            if i and i.get("tabs") != []
        ]
        if len(keep) <= 1:
            return keep[0][0] if keep else None
        items, sizes = [i for i, _ in keep], [z for _, z in keep]
        return {
            "type": "split-layout",
            "orientation": "horizontal",
            "sizes": [z / sum(sizes) for z in sizes],
            "children": items,
        }

    top = row(tab("by-group"), tab("over-time"), sizes=[0.42, 0.58])
    if "map" in panels:
        bottom = row(tab("map"), tab("heatmap", "rows"))
    else:
        bottom = row(tab("heatmap"), tab("rows"))
    layout = (
        {
            "type": "split-layout",
            "orientation": "vertical",
            "sizes": [0.46, 0.54],
            "children": [top, bottom],
        }
        if top
        else bottom
    )
    ws = {"panels": panels, "layout": layout}
    if "by-group" in panels:
        ws["masters"] = ["by-group"]
    return ws


def int32_fields(console: dict) -> list[str]:
    """Integer fields whose whole range fits 32 bits.

    DuckDB reads Parquet int64 as BIGINT, which Perspective shows as a float, so the browser casts
    these back to INTEGER.
    """
    return [
        e["name"]
        for e in console["fields"]
        if e["type"] == "integer"
        and e.get("min") is not None
        and e["min"] >= -INT32
        and e["max"] < INT32
    ]
