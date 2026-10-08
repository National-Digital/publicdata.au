"""Build-time figures for the pages, drawn as inline SVG.

The figures are rows per year and rows per map cell, read from a version's data.parquet. A figure
is a count of the rows in the file it sits beside, worked out in the build. Nothing else is added
and the files are untouched.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from pathlib import Path

from PIL import Image

from .explorer import YEAR, split_field
from .provenance import long_date
from .records import connect
from .register import WHERE_OPS

STEP = 0.05
LON0, LON1, LAT0, LAT1 = 112.0, 154.5, -44.5, -9.0
_STATES = json.loads((Path(__file__).parent / "au_states.json").read_text("utf-8"))
STATES: dict[str, str] = _STATES["states"]
MAP_RGB = (255, 139, 90)
OUTLINES = "State outlines from the ABS Australian Statistical Geography Standard 2021, CC BY 4.0."
PALETTE = 8
# Name, longitude, latitude, which side the label sits, and whether the national map shows it.
CITIES = (
    ("Sydney", 151.21, -33.87, "start", True),
    ("Melbourne", 144.96, -37.81, "end", True),
    ("Brisbane", 153.02, -27.47, "start", True),
    ("Perth", 115.86, -31.95, "end", True),
    ("Adelaide", 138.60, -34.93, "end", True),
    ("Hobart", 147.33, -42.88, "start", True),
    ("Darwin", 130.84, -12.46, "start", True),
    ("Canberra", 149.13, -35.28, "start", True),
    ("Cairns", 145.77, -16.92, "start", True),
    ("Townsville", 146.82, -19.26, "start", False),
    ("Newcastle", 151.78, -32.93, "start", False),
    ("Geelong", 144.36, -38.15, "end", False),
    ("Launceston", 147.14, -41.43, "start", False),
    ("Alice Springs", 133.88, -23.70, "start", False),
    ("Mount Isa", 139.49, -20.73, "end", False),
    ("Broken Hill", 141.47, -31.96, "end", False),
    ("Mount Gambier", 140.78, -37.83, "end", False),
    ("Port Augusta", 137.77, -32.49, "end", False),
    ("Kalgoorlie", 121.47, -30.75, "start", False),
    ("Broome", 122.24, -17.96, "start", False),
)


def fmt(n: float) -> str:
    """A figure as the pages print it.

    Whole numbers carry separators, and a fraction keeps two places under ten and one under a
    thousand, so an average reads 4.35 or 19.3, not 4 or 19.
    """
    if float(n).is_integer():
        return f"{int(n):,}"
    if abs(n) < 10:
        return f"{n:.2f}".rstrip("0").rstrip(".")
    return f"{n:,.1f}" if abs(n) < 1000 else f"{n:,.0f}"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def cutoff(m) -> str:
    """The last day the version's rows can run to.

    That is the publisher's as-at date, else the day the publisher released the version or the
    file was fetched, whichever is earlier.
    """
    return m.as_at or min(m.fetched_at[:10], m.version)


def year_field(ds) -> tuple[str, str] | None:
    """The field that gives a row its year: the register's chart year, else an integer year
    field, else a date field. None when the register turns the chart off. A text chart year is a
    financial year written 2018-19."""
    chart = getattr(ds, "chart", None) or {}
    if chart.get("off"):
        return None
    if chart.get("year"):
        f = ds.field(chart["year"])
        kind = {"integer": "integer", "string": "financial"}.get(f.type, "date")
        return f.name, kind
    # A school grade (year_1) or an age rounded to a year is not the row's year.
    years = [
        f.name
        for f in ds.fields
        if f.type == "integer"
        and YEAR.search(f.name)
        and not re.search(r"(year|yr)_\d|_to_year", f.name)
    ]
    years.sort(key=lambda n: n.startswith("reporting"))
    if years:
        return years[0], "integer"
    for f in ds.fields:
        if f.type in ("date", "datetime"):
            return f.name, "date"
    return None


# A financial year with both its years, such as 2011-12, 2008/09, 2011-2012 or FY201213.
FY_PAIR = re.compile(r"(?<!\d)(\d{4})\s*[-/]?\s*(\d{4}|\d{2})(?!\d)")
# One written by the year it ends, as Australia names them: FY2010 is July 2009 to June 2010.
FY_END = re.compile(r"^FY\s*(\d{4})$", re.I)
FY_SHORT = re.compile(r"^FY\s*(\d{2})\s*-\s*(\d{2})$", re.I)


def financial_start(text) -> int | None:
    """The calendar year a financial year starts in, or None when the text is not one."""
    t = re.sub(r"[\u2013\u2014]", "-", str(text)).strip()
    if m := FY_END.match(t):
        return int(m.group(1)) - 1
    if m := FY_SHORT.match(t):
        a, b = int(m.group(1)), int(m.group(2))
        return 2000 + a if (a + 1) % 100 == b else None
    pairs = FY_PAIR.findall(t)
    if len(pairs) != 1:
        return None
    a, b = int(pairs[0][0]), pairs[0][1]
    return a if int(b) == (a + 1 if len(b) == 4 else (a + 1) % 100) else None


def series(
    db: Path,
    yf: str,
    kind: str,
    split: str | None,
    metric: str,
    until: str,
    where: dict | None = None,
) -> dict:
    """Rows (or the summed count field) per year, split by a category, kept to the register's
    chart condition when it has one. A year that ends after the cut-off is left out and named,
    so a chart never falls away at a part year. A financial year is drawn under the year it
    starts in and named as the publisher writes it."""
    y = {"integer": _q(yf), "financial": _q(yf)}.get(
        kind, f"TRY_CAST(substr({_q(yf)}, 1, 4) AS INTEGER)"
    )
    cols = f"{y}, {_q(split)}" if split else y
    con = connect(db)
    try:
        cond, params = _conditions(where, con)
        agg = _agg(metric, con)
        rows = con.execute(
            f"SELECT {cols}, {agg} FROM records WHERE {y} IS NOT NULL{cond} GROUP BY {'1, 2' if split else '1'}",
            params,
        ).fetchall()
        first, last = (
            con.execute(
                f"SELECT MIN(substr({_q(yf)}, 1, 10)), MAX(substr({_q(yf)}, 1, 10)) FROM records"
                f" WHERE {_q(yf)} IS NOT NULL{cond}",
                params,
            ).fetchone()
            if kind == "date"
            else (None, None)
        )
    finally:
        con.close()
    values: dict[int, dict[str, float]] = {}
    names: dict[int, str] = {}
    for r in rows:
        year = financial_start(r[0]) if kind == "financial" else int(r[0])
        if year is None or year < 1800 or year > 2200:
            continue
        if kind == "financial":
            names[year] = min(names.get(year, str(r[0])), str(r[0]))
        key = str(r[1]) if split else ""
        values.setdefault(year, {})[key] = values.get(year, {}).get(key, 0) + (r[-1] or 0)
    years = sorted(values)
    # Dated rows that stop short of the cut-off end the last full year sooner.
    end = min(until, last) if last else until
    # Dated rows that begin after January leave their first year short too.
    late = int(first[:4]) if first and first[5:] > "01-31" else None
    ends = (lambda y: f"{y + 1}-06-30") if kind == "financial" else (lambda y: f"{y}-12-31")
    full = [y for y in years if ends(y) <= end and y != late]
    partial = [y for y in years if y not in full]
    cats = sorted({k for v in values.values() for k in v}) if split else [""]
    if len(cats) > PALETTE:
        keep = sorted(cats, key=lambda c: -sum(v.get(c, 0) for v in values.values()))[: PALETTE - 1]
        other = [c for c in cats if c not in keep]
        for v in values.values():
            v["Other"] = sum(v.pop(c, 0) for c in other)
        cats = [*sorted(keep), "Other"]
    return {
        "years": full,
        "partial": partial,
        # The newest day in a date field, which says where a part year stops.
        "last": last or "",
        "first": first or "",
        "categories": cats,
        "values": {y: values[y] for y in full},
        "names": {y: names.get(y, str(y)) for y in years},
        # An average, lowest or highest per year does not add up across the years.
        "additive": metric == "count" or metric.startswith("sum."),
    }


def _columns(db: Path) -> set[str]:
    con = connect(db)
    try:
        return set(con.columns())
    finally:
        con.close()


def _agg(metric: str, con) -> str:
    if metric == "count":
        return "COUNT(*)"
    fn, name = metric.split(".", 1)
    return con.agg(fn, name)


AGG_WORDS = {"avg": "Average", "min": "Lowest", "max": "Highest"}


# A register label that already says how its figure is split or kept reads as a whole phrase,
# so nothing is added to it and a chart does not borrow it.
PHRASE = re.compile(r"\b(by|each|per|newest|first)\b")


def measure(ds, metric: str, label: str = "") -> str:
    """What a figure counts or sums, in words.

    That is the register's label for it, else the row label for a count and the field's label for
    a sum.
    """
    if label:
        return label
    if metric == "count":
        return ds.row_label or "Rows"
    fn, name = metric.split(".", 1)
    word = ds.field(name).display
    return word if fn == "sum" else f"{AGG_WORDS[fn]} {word[0].lower()}{word[1:]}"


def basis(ds, metric: str) -> str:
    """How the figure is worked out from the rows, for the caption."""
    if metric == "count":
        return "a count of rows"
    fn, name = metric.split(".", 1)
    word = ds.field(name).display.lower()
    return {"sum": "a sum", "avg": "an average", "min": "the lowest", "max": "the highest"}[
        fn
    ] + f" of the {word} column"


def _conditions(where, con) -> tuple[str, list]:
    """One or more (field, op, value) conditions as SQL, each ANDed on.

    The values are given as the connection's columns compare them.
    """
    if not where:
        return "", []
    conds = [where] if isinstance(where, dict) else list(where)
    sql, params = "", []
    for w in conds:
        if w["op"] not in ("=", "!=", ">", "<", ">=", "<="):
            raise ValueError(w["op"])
        sql += f" AND {_q(w['field'])} {w['op']} ?"
        # The records view holds a boolean as 1 or 0, as SQLite does.
        params.append(con.param(w["field"], {"true": 1, "false": 0}.get(w["value"], w["value"])))
    return sql, params


def _nice(top: float, lines: int = 5) -> tuple[float, float]:
    """A tick step from 1, 2, 5 times a power of ten, and the axis top it makes."""
    if top <= 0:
        return 1, 1
    raw = top / lines
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 5, 10) if s * mag >= raw)
    return step, math.ceil(top / step) * step


def _tick(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if v >= 1000:
        return f"{v / 1000:g}k"
    return f"{v:g}"


def _name(s: dict, y: int) -> str:
    return s.get("names", {}).get(y, str(y))


def _summary(s: dict, totals: dict, peak: int) -> str:
    years = s["years"]
    top, last = _name(s, peak), _name(s, years[-1])
    if s.get("additive", True):
        return f", {fmt(sum(totals.values()))} in all, the highest {fmt(totals[peak])} in {top}."
    if peak == years[-1]:
        return f", the highest {fmt(totals[peak])} in {top}, the latest year."
    return f", the highest {fmt(totals[peak])} in {top} and the latest {fmt(totals[years[-1]])} in {last}."


def stacked_svg(s: dict, label: str, w: int = 680, h: int = 300) -> str:
    """Bars per year, stacked by category, with a legend in HTML beside the drawing."""
    years, cats = s["years"], s["categories"]
    if not years:
        return ""
    totals = {y: sum(s["values"][y].values()) for y in years}
    peak = max(years, key=lambda y: totals[y])
    label = (
        f"{label} {len(years)} years"
        + (f" in {len(cats)} series" if len(cats) > 1 else "")
        + _summary(s, totals, peak)
    )
    step, top = _nice(max(totals.values()))
    pl, pr, pt, pb = 46, 8, 14, 30
    cw = (w - pl - pr) / len(years)
    ih = h - pt - pb
    out = []
    g = 0.0
    while g <= top:
        yy = pt + ih - ih * g / top
        out.append(
            f'<line x1="{pl}" x2="{w - pr}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
            f'<text x="{pl - 6}" y="{yy + 4:.1f}" text-anchor="end" class="tick">{_tick(g)}</text>'
        )
        g += step
    every = max(1, math.ceil(len(years) / 8))
    gap = 3 if cw > 8 else 1
    for i, y in enumerate(years):
        x = pl + i * cw + gap / 2
        acc = 0.0
        for k, c in enumerate(cats):
            n = s["values"][y].get(c, 0)
            if not n:
                continue
            hh = ih * n / top
            y0 = pt + ih - ih * (acc + n) / top
            title = f"{_name(s, y)} {c}: {fmt(n)}" if c else f"{_name(s, y)}: {fmt(n)}"
            out.append(
                f'<rect x="{x:.1f}" y="{y0:.1f}" width="{cw - gap:.1f}" height="{hh:.1f}" fill="var(--s{k + 1})"><title>{_esc(title)}</title></rect>'
            )
            acc += n
        if (len(years) - 1 - i) % every == 0:
            out.append(
                f'<text x="{x + (cw - gap) / 2:.1f}" y="{h - 10}" text-anchor="middle" class="tick">{_name(s, y)}</text>'
            )
    svg = (
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{_esc(label)}" class="chart">'
        + "".join(out)
        + "</svg>"
    )
    if cats != [""]:
        svg += (
            '<div class="legend">'
            + "".join(
                f'<span><i style="background:var(--s{k + 1})"></i>{_esc(c)}</span>'
                for k, c in enumerate(cats)
            )
            + "</div>"
        )
    return svg


def spark_svg(s: dict, w: int = 220, h: int = 56) -> str:
    """A small line of the yearly totals for a card."""
    years = s["years"]
    if len(years) < 2:
        return ""
    vals = [sum(s["values"][y].values()) for y in years]
    mn, mx = min(vals), max(vals)
    rng = (mx - mn) or 1
    pts = []
    for i, v in enumerate(vals):
        x = 2 + (w - 4) * i / (len(vals) - 1)
        y = 4 + (h - 10) * (1 - (v - mn) / rng)
        pts.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (
        f"M{pts[0][0]:.1f},{h} "
        + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts)
        + f" L{pts[-1][0]:.1f},{h} Z"
    )
    ex, ey = pts[-1]
    peak = years[vals.index(mx)]
    alt = (
        f"{fmt(sum(vals))} over {len(years)} years, {year_span(s)}, the highest {fmt(mx)} in {_name(s, peak)}."
        if s.get("additive", True)
        else f"{len(years)} years, {year_span(s)}"
        + _summary(s, dict(zip(years, vals, strict=True)), peak)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" class="spark" role="img" aria-label="{_esc(alt)}">'
        f'<path d="{area}" fill="var(--primary)" fill-opacity=".12"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--primary)" stroke-width="1.6" stroke-linejoin="round"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.6" fill="var(--primary)"/></svg>'
    )


def hbars_svg(rows: list[tuple[str, float]], label: str, w: int = 420) -> str:
    """Horizontal bars for a short list of category totals."""
    if not rows:
        return ""
    mx = max(v for _, v in rows) or 1
    rh = 24
    h = 8 + rh * len(rows)
    out = []
    for i, (k, v) in enumerate(rows):
        y = 4 + i * rh
        bw = (w - 200) * v / mx
        name = k if len(k) <= 22 else k[:21] + "…"
        out.append(
            f'<text x="118" y="{y + rh * 0.68:.1f}" text-anchor="end" class="tick">{_esc(name)}</text>'
            f'<rect x="124" y="{y + 2:.1f}" width="{bw:.1f}" height="{rh - 6:.1f}" fill="var(--primary)"><title>{_esc(k)}: {fmt(v)}</title></rect>'
            f'<text x="{128 + bw:.1f}" y="{y + rh * 0.68:.1f}" class="tick">{fmt(v)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" class="chart" role="img" aria-label="{_esc(label.rstrip(".") + ": " + ", ".join(f"{k} {fmt(v)}" for k, v in rows) + ".")}">'
        + "".join(out)
        + "</svg>"
    )


def cells(db: Path, lon: str, lat: str, where=None) -> dict[tuple[int, int], float]:
    """Rows per cell of STEP degrees, counted in SQL.

    The rows are kept to those the conditions match when any are given: a (field, op, value)
    tuple, or one or more {field, op, value} dicts.
    """
    if isinstance(where, tuple):
        where = {"field": where[0], "op": where[1], "value": where[2]}
    con = connect(db)
    try:
        cond, params = _conditions(where, con)
        rows = con.execute(
            f"SELECT CAST(trunc(({_q(lon)} - {LON0}) / {STEP}) AS INTEGER), CAST(trunc(({LAT1} - {_q(lat)}) / {STEP}) AS INTEGER), COUNT(*)"
            f" FROM records WHERE {_q(lon)} BETWEEN {LON0} AND {LON1} AND {_q(lat)} BETWEEN {LAT0} AND {LAT1}{cond}"
            " GROUP BY 1, 2",
            params,
        ).fetchall()
    finally:
        con.close()
    return {(int(i), int(j)): float(n or 0) for i, j, n in rows if n}


def _raster(cells_: dict, box: tuple[int, int, int, int], floor: float = 0.35) -> bytes:
    """The cells as a PNG with the brand colour and an alpha that grows with the count.

    Pixels are cells, so the browser scales it without smoothing.
    """
    i0, j0, i1, j1 = box
    w, h = i1 - i0, j1 - j0
    mx = max(cells_.values()) or 1
    buf = bytearray(w * h * 4)
    r, g, b = MAP_RGB
    for (i, j), n in cells_.items():
        if i0 <= i < i1 and j0 <= j < j1:
            at = ((j - j0) * w + (i - i0)) * 4
            a = floor + (1 - floor) * math.log1p(n) / math.log1p(mx)
            buf[at : at + 4] = bytes((r, g, b, int(255 * a)))
    img = Image.frombytes("RGBA", (w, h), bytes(buf))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _png(out: Path, data: bytes) -> str:
    """The PNG written once under maps/, named by its content.

    That way a page links a file that never changes, and the SVG over it stays vector.
    """
    rel = f"maps/{hashlib.sha256(data).hexdigest()[:16]}.png"
    path = out / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return "/" + rel


def _join(names) -> str:
    names = list(names)
    if not names:
        return ""
    return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]


def map_alt(cells_: dict, what: str, gaps: dict[str, str] | None = None) -> str:
    """What the map draws, in words worked out from the cells.

    That is the rows, the cells, the fullest cell and any state hatched.
    """
    total = round(sum(cells_.values()))
    text = (
        f"{fmt(total)} {what.lower()} drawn in {fmt(len(cells_))} cells of {STEP:g} degrees, "
        f"the fullest with {fmt(max(cells_.values()))}."
    )
    if gaps:
        text += f" {_join(gaps)} hatched: {next(iter(gaps.values()))}."
    return text


def _cell(lon: float, lat: float) -> tuple[float, float]:
    return (lon - LON0) / STEP, (LAT1 - lat) / STEP


def map_html(
    cells_: dict,
    what: str,
    out: Path,
    gaps: dict[str, str] | None = None,
    box=None,
    floor: float = 0.35,
    bare: bool = False,
) -> str:
    """A map of the cells, as a PNG under maps/ with a vector SVG over it.

    The PNG sits in an img whose alt says what it draws, and the SVG carries the state outlines,
    the city names and any state hatched. gaps names the states drawn hatched, with the reason
    under the name, so a state without data never reads as a state without rows. A bare map is
    the cells alone, no outlines and no city names, with only a gap still hatched.
    """
    if not cells_:
        return ""
    nx, ny = int((LON1 - LON0) / STEP) + 1, int((LAT1 - LAT0) / STEP) + 1
    if box is None:
        xs = [i for i, _ in cells_]
        ys = [j for _, j in cells_]
        pad = 6
        box = (
            max(0, min(xs) - pad),
            max(0, min(ys) - pad),
            min(nx, max(xs) + pad + 1),
            min(ny, max(ys) + pad + 1),
        )
    i0, j0, i1, j1 = box
    w, h = i1 - i0, j1 - j0
    fs = w / 45
    img = (
        f'<img class="mapimg" src="{_png(out, _raster(cells_, box, floor))}"'
        f' alt="{_esc(map_alt(cells_, what, gaps))}" width="{w}" height="{h}" loading="lazy" decoding="async">'
    )
    out = [f'<svg viewBox="{i0} {j0} {w} {h}" class="map" aria-hidden="true" focusable="false">']
    if gaps:
        out.append(
            '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            '<line x1="0" y1="0" x2="0" y2="6" stroke="var(--band-muted)" stroke-opacity=".35" stroke-width="1"/></pattern></defs>'
        )
    if not bare:
        out += [
            f'<path class="state" vector-effect="non-scaling-stroke" d="{d}"/>'
            for n, d in STATES.items()
            if n not in (gaps or {})
        ]
    out += [
        f'<path class="nodata" vector-effect="non-scaling-stroke" d="{STATES[n]}"/>'
        for n in (gaps or {})
        if n in STATES
    ]
    out.append(f'<g style="font-size:{fs:.1f}px">')
    for name, lon, lat, anchor, major in CITIES:
        x, y = _cell(lon, lat)
        if bare or (gaps and not major):
            continue
        span = len(name) * fs * 0.55
        left, right = (x, x + span) if anchor == "start" else (x - span, x)
        if not (i0 + fs < left and right < i1 and j0 + fs < y < j1 - fs):
            continue
        dx = fs * 0.4 if anchor == "start" else -fs * 0.4
        out.append(
            f'<text x="{x + dx:.1f}" y="{y + fs * 0.35:.1f}" text-anchor="{anchor}" class="mlabel">{name}</text>'
        )
    for name, why in (gaps or {}).items():
        lon, lat = GAP_LABEL.get(name, (None, None))
        if lon is None:
            continue
        x, y = _cell(lon, lat)
        out.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" class="nlabel">{_esc(name)}</text>'
            f'<text x="{x:.1f}" y="{y + fs * 1.2:.1f}" text-anchor="middle" class="nlabel small">{_esc(why)}</text>'
        )
    out.append("</g></svg>")
    # An overlay with nothing but its empty label group is left out.
    svg = "".join(out) if len(out) > 3 else ""
    return f'<div class="mapbox" style="--ar:{w}/{h}">{img}{svg}</div>'


GAP_LABEL = {
    "Western Australia": (122.5, -26.0),
    "Northern Territory": (133.5, -19.0),
    "South Australia": (135.5, -29.5),
    "Queensland": (144.5, -23.0),
    "New South Wales": (146.5, -32.0),
    "Victoria": (143.5, -36.8),
    "Tasmania": (146.6, -42.0),
    "Australian Capital Territory": (149.0, -35.5),
}


def merge(parts: list[dict]) -> dict:
    total: dict[tuple[int, int], float] = {}
    for p in parts:
        for k, v in p.items():
            total[k] = total.get(k, 0) + v
    return total


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


VERBS = {
    "=": "is",
    "!=": "is not",
    ">": "is over",
    "<": "is under",
    ">=": "is at least",
    "<=": "is at most",
}


def where_words(ds) -> str:
    """The chart conditions as a caption clause, blank when there are none."""
    conds = (ds.chart or {}).get("where") or ()
    if not conds:
        return ""
    parts = [f"{ds.field(w['field']).display.lower()} {VERBS[w['op']]} {w['value']}" for w in conds]
    return f" Rows where {' and '.join(parts)} are drawn."


def year_span(s: dict) -> str:
    if not s["years"]:
        return ""
    a, b = s["years"][0], s["years"][-1]
    return f"{_name(s, a)} to {_name(s, b)}" if a != b else _name(s, a)


def dataset_figures(ds, m, console: dict | None, db: Path, out: Path, within=None) -> dict:
    """Everything a dataset or version page draws for one version.

    That is the yearly chart, its caption, a sparkline for a card and a map when the rows have
    coordinates. within is a {field, op, value} condition a place page adds, so its figures draw
    its rows alone.
    """
    chart = ds.chart or {}
    metric = chart.get("metric") or (console["example"]["metric"] if console else "count")
    yf = year_field(ds)
    # The example's words fit the chart only when the chart keeps the rows the example keeps,
    # which it does when the example filters on nothing but the year it draws.
    ex = console["example"] if console else {}
    kept = all(f["field"] == (yf[0] if yf else "") for f in ex.get("filters", ()))
    kept = kept and not PHRASE.search(ex.get("label", ""))
    label = chart.get("label") or (
        ex.get("label", "") if console and not chart.get("metric") and kept else ""
    )
    what = measure(ds, metric, label)
    fig: dict = {
        "what": what,
        "basis": basis(ds, metric),
        "chart": "",
        "chart_caption": "",
        "spark": "",
        "spark_caption": "",
        "map": "",
        "map_caption": "",
        "years": "",
    }
    until = cutoff(m)
    if chart.get("split") is not None:
        split = chart["split"] or None
    else:
        # Stacked bars add up, so an average or an extreme is drawn as one series.
        split = (
            split_field(console) if console and metric.split(".")[0] in ("count", "sum") else None
        )
    # An older version can lack a column the register names, and then draws no chart.
    named = {yf[0]} if yf else set()
    named |= {w["field"] for w in chart.get("where", ())} | ({split} if split else set())
    named |= {metric.split(".", 1)[1]} if metric != "count" else set()
    if yf and db.exists() and named <= _columns(db):
        conds = [*chart.get("where", ()), *([within] if within else [])]
        s = series(db, yf[0], yf[1], split, metric, until, conds)
        # One bar is no trend, so a table of one year draws no chart.
        if len(s["years"]) >= 2:
            by = f" by {ds.field(split).display.lower()}" if split else ""
            per = "per financial year" if yf[1] == "financial" else "per year"
            span = year_span(s)
            left = where_words(ds)
            head = [y for y in s["partial"] if s["first"] and y == int(s["first"][:4])]
            if head and head[0] < s["years"][0]:
                left += (
                    f" {head[0]} is not drawn because the rows begin on {long_date(s['first'])}."
                )
            tail = [y for y in s["partial"] if y not in head or y > s["years"][-1]]
            if tail:
                part = " and ".join(_name(s, y) for y in tail)
                verb = "is" if len(tail) == 1 else "are"
                if m.as_at:
                    why = f"the file runs to {long_date(m.as_at)}"
                elif s["last"]:
                    why = f"the rows run to {long_date(min(s['last'], until))}"
                else:
                    why = f"the year was not over when the publisher released this version on {long_date(until)}"
                left += f" {part} {verb} not drawn because {why}."
            fig.update(
                series=s,
                years=span,
                chart=stacked_svg(s, f"{what} {per}{by}, {span}"),
                chart_caption=f"{what} {per}{by}, {span}.{left}",
                spark=spark_svg(s),
                spark_caption=f"{what.lower()} {per}, {span}.{left}",
            )
    if ds.geometry and ds.geometry.get("lon") and db.exists():
        c = cells(db, ds.geometry["lon"], ds.geometry["lat"], within)
        if c:
            fig["cells"] = c
            rows = ds.row_label or "Rows"
            fig["map"] = map_html(c, rows, out)
            fig["map_caption"] = f"{rows} counted in cells of {STEP:g} degrees. {OUTLINES}"
    return fig


def national_map(
    parts: list[tuple[str, dict]], what: str, why: str, out: Path, covered: set[str] | None = None
) -> str:
    """The home page map: the cells of every located dataset overlaid.

    Each state that publishes none is drawn hatched and named. covered names the states whose
    datasets have located rows; a state whose rows all miss the drawing's condition is covered,
    not a gap.
    """
    covered = {state for state, _ in parts} | (covered or set())
    gaps = {n: why for n in STATES if n not in covered}
    nx, ny = int((LON1 - LON0) / STEP) + 1, int((LAT1 - LAT0) / STEP) + 1
    # Fatal crashes are sparse, so a single one must still show at the size of the hero.
    # The hero is the data alone: no outlines and no city names, only a gap hatched.
    return map_html(
        merge([c for _, c in parts]),
        what,
        out,
        gaps=gaps,
        box=(0, 50, nx + 90, ny),
        floor=0.7,
        bare=True,
    )


def example_rows(
    db: Path, console: dict, limit: int = 8, key: tuple[str, ...] = ()
) -> list[tuple[str, float]]:
    """The query console's first aggregate, answered from the rows the API loads.

    key is the dataset's key, which data.sqlite indexes.
    """
    ex = console["example"]
    group = (ex.get("group") or [None])[0]
    if not group or not db.exists():
        return []
    types = {e["name"]: e["type"] for e in console["fields"]}
    where, params = [], []
    for f in ex.get("filters", []):
        where.append(f"{_q(f['field'])} {WHERE_OPS[f['op']]} ?")
        # The API takes true and false for a boolean, which the records hold as 1 and 0.
        v = f["value"]
        params.append(
            {"true": 1, "false": 0}.get(v, v) if types.get(f["field"]) == "boolean" else v
        )
    # Equal totals come in the order SQLite gives them: ascending when its key index hands it the
    # groups in order, which it does for a key field whose earlier key fields are all pinned to
    # one value, else descending.
    pinned = {f["field"] for f in ex.get("filters", []) if f["op"] == "eq"}
    indexed = any(k == group and set(key[:i]) <= pinned for i, k in enumerate(key))
    con = connect(db)
    try:
        params = [
            con.param(f["field"], v) for f, v in zip(ex.get("filters", []), params, strict=True)
        ]
        agg = _agg(ex["metric"], con)
        rows = con.execute(
            f"SELECT {_q(group)}, {agg} FROM records"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + f" GROUP BY 1 ORDER BY 2 DESC, 1 {'ASC' if indexed else 'DESC'} LIMIT {int(limit)}",
            params,
        ).fetchall()
    finally:
        con.close()
    return [(str(k) if k is not None else "blank", _num(v)) for k, v in rows]


def _num(v) -> int | float:
    v = v or 0
    return int(v) if float(v).is_integer() else float(v)


def newest(con, field: str):
    """A field's newest value, which is its largest.

    When some values start with a digit, only those count, so a text period such as
    "bef 30 Jun 2018" never outranks "2024/25".
    """
    f = _q(field)
    top = con.execute(
        f"SELECT MAX({f}) FROM records WHERE CAST({f} AS TEXT) GLOB '[0-9]*'"
    ).fetchone()[0]
    return top if top is not None else con.execute(f"SELECT MAX({f}) FROM records").fetchone()[0]


def sample_rows(
    db: Path,
    fields: list[str],
    n: int = 3,
    where=None,
    nulls: bool = False,
    order: tuple = (),
    spread: str = "",
) -> dict:
    """Some rows of some fields, for a preview table.

    The rows are kept to those the conditions match: in the Parquet's order, or by the
    (field, descending) order terms first, with the spread field's values taking turns. A
    condition on newest stands for the field's newest value. A null cell is "" unless nulls asks
    for None, which a page shows as null.
    """
    if not db.exists() or not fields:
        return {"fields": [], "rows": []}
    con = connect(db)
    try:
        have = set(con.columns())
        cols = [f for f in fields if f in have]
        if not cols:
            return {"fields": [], "rows": []}
        conds = []
        for w in [where] if isinstance(where, dict) else list(where or ()):
            if w["value"] == "newest":
                w = {**w, "value": newest(con, w["field"])}
            conds.append(w)
        cond, params = _conditions(conds, con)
        terms = [(_q(f), " DESC" if desc else "") for f, desc in order if f in have]
        by = ", ".join([*(f + d for f, d in terms), "rowid"])
        if spread in have:
            # Each value's rows are numbered in the order, then the values take turns.
            rows = con.execute(
                f"SELECT {', '.join('r.' + _q(c) for c in cols)} FROM records r JOIN ("
                f"SELECT rowid AS id, ROW_NUMBER() OVER"
                f" (PARTITION BY {_q(spread)} ORDER BY {by}) AS rn FROM records WHERE 1=1{cond}"
                f") w ON r.rowid = w.id WHERE w.rn <= {int(n)} ORDER BY w.rn, "
                + ", ".join([*(f"r.{f}{d}" for f, d in terms), "r.rowid"])
                + f" LIMIT {int(n)}",
                params,
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT {', '.join(_q(c) for c in cols)} FROM records WHERE 1=1{cond}"
                f" ORDER BY {by} LIMIT {int(n)}",
                params,
            ).fetchall()
    finally:
        con.close()
    blank = None if nulls else ""
    return {
        "fields": cols,
        "rows": [[blank if v is None else _preview_cell(v) for v in r] for r in rows],
    }


# A sample cell longer than this is cut, so one row of a long note stays one line.
CELL_MAX = 120


def _preview_cell(v) -> str:
    """A value as a preview table shows it.

    A float loses its binary noise and long text is cut with an ellipsis.
    """
    if isinstance(v, float):
        return format(v, ".12g")
    text = str(v)
    if "<" in text and ">" in text:
        text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= CELL_MAX else text[: CELL_MAX - 1].rstrip() + "…"
