"""Fail-closed checks over a finished dist/. Anything wrong here stops the deploy."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from html import escape as html_escape
from html import unescape as html_unescape
from pathlib import Path

from . import SITE, explorer, serialise, structured
from .ard import problems as ard_problems
from .register import OPEN_LICENCES, load
from .serialise.geo import geo_kind
from .serialise.profile import query_key

# Dated tables over the period threshold that were published whole before periods existed. Each
# is split by a change of its own, which rewrites how its new versions are laid out.
PERIOD_PENDING = {
    "au-eucalypt-records": "Parquet over 100 MB; to be split by year of the record's event date",
    "au-water-storage-levels": "over 5 million rows; to be split by year of the observation date",
}

# Titles and summaries quoted from a portal are the publisher's words and are not rewritten.
PORTAL_TEXT = re.compile(r"<!--portal-text-->.*?<!--/portal-text-->", re.S)


def _partition_values(out: Path, slug: str) -> list[str]:
    """The publisher's own values that a dataset's pages name as places and partitions, longest
    first, so the copy checks read only this site's words."""
    vals: set[str] = set()
    for idx in (out / "d" / slug / "v").glob("*/by/*/index.json"):
        for e in json.loads(idx.read_text(encoding="utf-8")).get("partitions", []):
            if isinstance(e.get("value"), str) and e["value"]:
                vals.add(html_escape(e["value"]))
                vals.add(e["value"])
    return sorted(vals, key=len, reverse=True)


def _without_publisher_values(text: str, page: Path, out: Path, cache: dict) -> str:
    parts = page.relative_to(out).parts
    if len(parts) < 2 or parts[0] != "d":
        return text
    if parts[1] not in cache:
        cache[parts[1]] = _partition_values(out, parts[1])
    for v in cache[parts[1]]:
        text = text.replace(v, "")
    return text


def _mcp_resources(out: Path) -> list[str]:
    """Every dataset page with a query console is an MCP resource whose fields are the page's own."""
    errors: list[str] = []
    listed_file = out / "mcp" / "resources.json"
    if not listed_file.exists():
        return errors
    listed = {r["uri"] for r in json.loads(listed_file.read_text(encoding="utf-8"))["resources"]}
    for page in sorted((out / "d").glob("*/index.html")) if (out / "d").exists() else []:
        slug = page.parent.name
        m = re.search(r'id="ds-data">(.*?)</script>', page.read_text(encoding="utf-8"), re.S)
        console = (json.loads(m.group(1)) if m else {}).get("console")
        uri = f"{SITE}/d/{slug}/fields.json"
        if not console:
            if uri in listed:
                errors.append(f"mcp/resources.json: {slug} is listed but has no query console")
            continue
        if uri not in listed:
            errors.append(f"mcp/resources.json: {slug} is not listed")
        f = page.parent / "fields.json"
        if not f.exists():
            errors.append(f"d/{slug}/fields.json: missing")
        elif json.loads(f.read_text(encoding="utf-8")).get("fields") != console["fields"]:
            errors.append(f"d/{slug}/fields.json: fields differ from the page's query console")
        listed.discard(uri)
    errors += [f"mcp/resources.json: {u} has no dataset page" for u in sorted(listed)]
    return errors


# The limits of the connector directory forms the listing is copied into.
LISTING_LIMITS = {"name": 100, "one_liner": 200, "description": 2000}


def _mcp_listing(out: Path) -> list[str]:
    f = out / "mcp" / "listing.json"
    if not f.exists():
        return []
    got = json.loads(f.read_text(encoding="utf-8"))
    errors = [f"mcp/listing.json: {k} is missing" for k in LISTING_LIMITS if not got.get(k)]
    errors += [
        f"mcp/listing.json: {k} is {len(got[k])} characters, over {n}"
        for k, n in LISTING_LIMITS.items()
        if got.get(k) and len(got[k]) > n
    ]
    if not 1 <= len(got.get("categories") or []) <= 5:
        errors.append("mcp/listing.json: one to five categories")
    if not 1 <= len(got.get("use_cases") or []) <= 3:
        errors.append("mcp/listing.json: one to three use cases")
    if not got.get("prerequisites"):
        errors.append("mcp/listing.json: prerequisites is missing")
    if not re.fullmatch(r"[a-z0-9-]+", got.get("slug") or ""):
        errors.append("mcp/listing.json: the slug is lowercase letters, numbers and hyphens")
    return errors


OG_IMAGE = re.compile(r'<meta property="og:image" content="([^"]+)">')


def _png_size(p: Path) -> tuple[int, int]:
    head = p.read_bytes()[:24]
    return struct.unpack(">II", head[16:24]) if head[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)


def _manifest(out: Path) -> list[str]:
    f = out / "manifest.webmanifest"
    if not f.exists():
        return []
    m = json.loads(f.read_text(encoding="utf-8"))
    errors = [
        f"manifest.webmanifest: {k} is missing"
        for k in ("id", "name", "short_name", "start_url", "display")
        if not m.get(k)
    ]
    seen = set()
    for icon in m.get("icons", []) + [
        i for s in m.get("shortcuts", []) for i in s.get("icons", [])
    ]:
        p = out / icon["src"].lstrip("/")
        if not p.exists():
            errors.append(f"manifest.webmanifest: {icon['src']} does not exist")
            continue
        if icon.get("type") == "image/png":
            w, h = _png_size(p)
            if icon.get("sizes") != f"{w}x{h}":
                errors.append(
                    f"manifest.webmanifest: {icon['src']} is {w}x{h}, declared {icon.get('sizes')}"
                )
            seen.add((icon.get("sizes"), icon.get("purpose", "any")))
    for need in (("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")):
        if need not in seen:
            errors.append(f"manifest.webmanifest: no {need[1]} icon at {need[0]}")
    return errors


def _social_card(rel: str, page: str, out: Path) -> list[str]:
    """Every page but an embed carries a 1200x630 card that exists and matches its hash."""
    if rel.endswith("embed/index.html"):
        return []
    m = OG_IMAGE.search(page)
    if not m:
        return [f"{rel}: no og:image"]
    errors = [] if 'name="twitter:card"' in page else [f"{rel}: no twitter:card"]
    url, _, v = m.group(1).partition("?v=")
    p = out / url.removeprefix(SITE + "/")
    if not url.startswith(SITE + "/") or not p.exists():
        return [*errors, f"{rel}: og:image {url} is not in the build"]
    if _png_size(p) != (1200, 630):
        errors.append(f"{rel}: og:image {url} is not 1200x630")
    if v != hashlib.sha256(p.read_bytes()).hexdigest()[:12]:
        errors.append(f"{rel}: og:image {url} hash does not match the file")
    return errors


DS_DATA = re.compile(r'id="ds-data">(.*?)</script>', re.S)
QUERY_TILE = re.compile(
    r'Filter and count from a URL</b><p class="mono small">(.*?)</p>(?:<p>(.*?)</p>)?', re.S
)


def examples(out: Path, datasets: dict) -> tuple[list[str], list[str]]:
    """Each dataset page's first query and what its tile answers, marked by where the query came
    from: the register's example, or the build's pick for a reader to look over. A register
    example that answers nothing is an error, since a person chose it to be read."""
    report, errors = [], []
    for page in sorted((out / "d").glob("*/index.html")) if (out / "d").exists() else []:
        slug = page.parent.name
        ds = datasets.get(slug)
        text = page.read_text(encoding="utf-8")
        m = DS_DATA.search(text)
        console = (json.loads(m.group(1)) if m else {}).get("console")
        if ds is None or not console:
            continue
        tile = QUERY_TILE.search(text)
        query = html_unescape(tile.group(1)) if tile else ""
        answer = html_unescape(tile.group(2) or "") if tile else ""
        origin = "register" if ds.example else "rules"
        report.append(f"{origin:<8} {slug}: {query} -> {answer or '(no groups)'}")
        if ds.example and not answer:
            errors.append(f"{slug}: the register's example query answers no rows")
    return report, errors


def periods_needed(out: Path, datasets: dict) -> list[str]:
    """A table whose rows carry their own date is split by period once its newest version is over
    100 MB of Parquet or 5 million rows, and a split table's grain is the largest that keeps every
    part at or under 100 MB. Sizes come from the catalogue and each newest manifest."""
    from .periods import PART_MAX, ROWS_MAX, grain_problem

    errors: list[str] = []
    cat = out / "catalog.json"
    newest = out / "latest.json"
    if not cat.exists() or not newest.exists():
        return errors
    sizes = {
        r["identifier"]: next(
            (
                int(d.get("byteSize") or 0)
                for d in r.get("distribution", [])
                if d.get("format") == "parquet"
            ),
            0,
        )
        for r in json.loads(cat.read_text(encoding="utf-8")).get("dataset", [])
    }
    for slug, version in json.loads(newest.read_text(encoding="utf-8")).items():
        ds = datasets.get(slug)
        man = out / "d" / slug / "v" / version / "manifest.json"
        if ds is None or ds.kind != "table" or not man.exists():
            continue
        m = json.loads(man.read_text(encoding="utf-8"))
        if m.get("period"):
            got = {r["period"]: r["files"]["parquet"]["bytes"] for r in m.get("parts", [])}
            grain = m["period"]["grain"]
            if why := grain_problem(got, grain):
                errors.append(f"{slug}/{version}: period grain {grain}: {why}")
        # A period applies from the next fetch, so the register entry is what is asked for.
        if ds.period is not None:
            continue
        dated = any(f.type in ("date", "datetime") for f in ds.fields)
        layer = (ds.geometry or {}).get("kind") in ("polygon", "line")
        big = sizes.get(slug, 0) > PART_MAX or int(m.get("rows", 0)) > ROWS_MAX
        if dated and big and not layer and slug not in PERIOD_PENDING:
            errors.append(
                f"{slug}/{version}: {m.get('rows')} rows and {sizes.get(slug, 0)} bytes of Parquet "
                "need a period; give the register entry period: {field, grain}"
            )
    return errors


def query_explained(out: Path, datasets: dict) -> list[str]:
    """A table dataset's page either runs the query console or says why the query API does not
    serve it, so a dataset never leaves the API unremarked."""
    errors = []
    for page in sorted((out / "d").glob("*/index.html")) if (out / "d").exists() else []:
        ds = datasets.get(page.parent.name)
        if ds is None or ds.kind != "table":
            continue
        text = page.read_text(encoding="utf-8")
        m = re.search(r'id="ds-data">(.*?)</script>', text, re.S)
        if m and not json.loads(m.group(1)).get("console") and "data-no-query" not in text:
            errors.append(
                f"d/{ds.slug}/index.html: not in the query API and the page says no reason"
            )
    return errors


def fetch_sequence(out: Path) -> list[str]:
    """Each fetch of a rolling source or a feed is compared with the fetch before it. One
    compared with an older fetch was made while the one between waited unmerged, so its change
    log and a feed's history skip a state."""
    errors = []
    for idx in sorted((out / "d").glob("*/changes/index.json")) if (out / "d").exists() else []:
        fetches = json.loads(idx.read_text(encoding="utf-8")).get("fetches", [])
        for a, b in zip(fetches, fetches[1:], strict=False):
            if b.get("from") != a["fetch"]:
                errors.append(
                    f"{idx.parts[-3]}: the fetch of {b['fetch']} was compared with {b.get('from')}, "
                    f"not {a['fetch']}; remove store/{idx.parts[-3]}/{b['fetch']}/ and fetch again"
                )
    return errors


def check(out: Path, register_dir: Path, absent: list[str] = (), site: bool = True) -> list[str]:
    return checked(out, register_dir, absent, site)[0]


def ard_errors(out: Path) -> list[str]:
    """The discovery manifest and every catalogue it links on this site must pass the ARD rules
    Lighthouse audits, and each linked catalogue must exist."""
    doc = json.loads((out / ".well-known/ard.json").read_text(encoding="utf-8"))
    errors = [f"ard: {p}" for p in ard_problems(doc)]
    for e in doc.get("entries", []):
        url = e.get("url", "")
        if e.get("type") != "application/ai-catalog+json" or not url.startswith(SITE + "/"):
            continue
        f = out / url.removeprefix(SITE + "/")
        if not f.exists():
            errors.append(f"ard: {e['identifier']} links {url}, which the build did not write")
            continue
        nested = json.loads(f.read_text(encoding="utf-8"))
        errors += [f"ard: {p}" for p in ard_problems(nested, top=False, where=url)]
    return errors


def checked(
    out: Path, register_dir: Path, absent: list[str] = (), site: bool = True
) -> tuple[list[str], list[str]]:
    """The gate's errors, and the list of every dataset page's first query. absent lists files a cached build left out because an earlier build published them; the
    deploy's push refuses to go ahead unless R2 holds every one. Without site, only the dated
    versions are checked, as a deploy shard builds them without pages."""
    errors: list[str] = []
    absent = set(absent)
    datasets = {d.slug: d for d in load(register_dir)}
    for req in (
        ()
        if not site
        else (
            "index.html",
            "catalog.json",
            "places.json",
            "backlog.json",
            "llms.txt",
            "llms-full.txt",
            "health.json",
            "robots.txt",
            "sitemap.xml",
            ".well-known/ard.json",
            ".well-known/ai-catalog.json",
            ".well-known/api-catalog",
            "skills/publicdata-au/SKILL.md",
            ".well-known/security.txt",
            "_headers",
            "_routes.json",
            "latest.json",
            "current.json",
            "withheld.json",
            "openapi.json",
            "mcp/resources.json",
            "mcp/server-card",
            "mcp/listing.json",
            "manifest.webmanifest",
            "favicon.svg",
            "favicon.ico",
            "apple-touch-icon.png",
            "og/site.png",
        )
    ):
        if not (out / req).exists():
            errors.append(f"missing {req}")
    if site and (out / ".well-known/ard.json").exists():
        errors += ard_errors(out)
    ddir = out / "d"
    for vman in sorted(ddir.glob("*/v/*/manifest.json")) if ddir.exists() else []:
        slug = vman.parts[-4]
        version = vman.parts[-2]
        ds = datasets.get(slug)
        if ds is None:
            errors.append(f"{slug}: published but not in the register")
            continue
        m = json.loads(vman.read_text(encoding="utf-8"))
        lic = (m.get("licence") or {}).get("id", "")
        if ds.licence.id not in OPEN_LICENCES:
            errors.append(f"{slug}: register licence {ds.licence.id} is not open")
        expected = ds.licence.portal_id or ds.licence.id
        if lic and lic.replace("_", "-").upper() != expected.replace("_", "-").upper():
            errors.append(
                f"{slug}/{version}: portal licence '{lic}' differs from register '{expected}'"
            )
        if ds.status not in ("live", "building"):
            errors.append(f"{slug}: status {ds.status} but versions are published")
        vdir = vman.parent
        rel = vdir.relative_to(out).as_posix()

        def have(name: str, vdir=vdir, rel=rel) -> bool:
            return (vdir / name).exists() or f"{rel}/{name}" in absent

        if ds.kind == "database":
            need = ("data.duckdb", "schema.json", "schema.sql") + tuple(
                f"tables/{t.name}.parquet" for t in ds.tables
            )
        elif m.get("whole", True) is False:
            # Written as parts alone: the DuckDB file over them, and each part this version wrote.
            need = ("data.duckdb", "schema.json", "schema.sql")
        else:
            rows = int(m.get("rows", 0))
            gone = None
            if serialise.capped(m):
                gone = m.get("formats_left_out")
                measured = m.get("measured_bytes")
                if not isinstance(gone, dict) or not isinstance(measured, dict):
                    errors.append(f"{slug}/{version}: manifest lacks its format record")
                    continue
                if not set(gone) <= set(serialise.cappable(geo_kind(ds))):
                    errors.append(
                        f"{slug}/{version}: formats_left_out names a format no cap covers"
                    )
                for f in serialise.MEASURED:
                    if f"data.{f}" not in measured:
                        errors.append(f"{slug}/{version}: measured_bytes lacks data.{f}")
                for name, n in measured.items():
                    if (vdir / name).is_file() and (vdir / name).stat().st_size != n:
                        errors.append(f"{slug}/{version}: measured_bytes disagrees with {name}")
                for x in (*gone, "arrow"):
                    if have(f"data.{x}"):
                        errors.append(
                            f"{slug}/{version}: data.{x} is published though the caps leave it out"
                        )
                vpage = vdir / "index.html"
                if site and vpage.exists():
                    text = vpage.read_text(encoding="utf-8")
                    for x, why in gone.items():
                        if html_escape(why) not in text:
                            errors.append(
                                f"{slug}/{version}: the version page does not say why data.{x} is not there"
                            )
            fmts = serialise.formats_for(rows, geo_kind(ds), gone)
            need = (
                *(f"data.{x}" for x in fmts),
                "schema.json",
                "schema.sql",
                "data.csv-metadata.json",
            )
        for part in (*m.get("parts", []), *(m.get("history") or {}).get("parts", [])):
            if part["tree"] == version:
                need = (*need, *(f["path"] for f in part["files"].values()))
            elif not (ddir / slug / "v" / part["tree"] / "manifest.json").exists():
                errors.append(
                    f"{slug}/{version}: part {part['period']} is in {part['tree']}, which is not published"
                )
        for f in need:
            if not have(f):
                errors.append(f"{slug}/{version}: missing {f}")
        if ds.kind != "database" and m.get("whole", True):
            q = query_key(slug, version)
            if not (out / q).exists() and q not in absent:
                errors.append(f"{slug}/{version}: missing its query copy {q}")
        if m.get("unknown_upstream_columns"):
            errors.append(
                f"{slug}/{version}: unknown upstream columns held: {m['unknown_upstream_columns']}"
            )
        if m.get("unknown_upstream_tables"):
            errors.append(
                f"{slug}/{version}: unknown upstream tables held: {m['unknown_upstream_tables']}"
            )
        # A licence with a condition states it on the dataset page, its Markdown twin, the
        # version page, the data package and the catalogue, so no one reaches the files
        # without seeing it.
        if ds.licence.condition:
            for rel_page in (
                ddir / slug / "index.html",
                ddir / slug / "index.md",
                ddir / slug / "datapackage.json",
                vdir / "index.html",
                vdir / "index.md",
                out / "catalog.json",
            ):
                if rel_page.exists() and ds.licence.condition[:60] not in rel_page.read_text(
                    encoding="utf-8"
                ):
                    errors.append(
                        f"{rel_page.relative_to(out).as_posix()}: the licence condition is not stated"
                    )
        # A cached version's data.json was checked by the build that published it.
        if (vdir / "data.json").exists():
            with (vdir / "data.json").open(encoding="utf-8") as f:
                head = f.read(4000)
            for needle in ('"attribution"', '"licence"', '"sha256"', '"not_endorsed"'):
                if needle not in head:
                    errors.append(f"{slug}/{version}: data.json header lacks {needle}")
    if not site:
        return errors, []
    for page in sorted(ddir.glob("*/explore/index.html")) if ddir.exists() else []:
        rel = page.relative_to(out)
        m = re.search(r'id="ex-data">(.*?)</script>', page.read_text(encoding="utf-8"), re.S)
        data = json.loads(m.group(1)) if m else None
        if not data:
            errors.append(f"{rel}: no explorer data")
            continue
        vdir = out / data["vendor"].strip("/")
        errors += [f"{rel}: vendor lacks {f}" for f in explorer.REQUIRED if not (vdir / f).exists()]
        errors += [
            f"{rel}: missing {v['parquet']}"
            for v in data["versions"]
            if not (out / v["parquet"].lstrip("/")).exists()
            and v["parquet"].lstrip("/") not in absent
        ]
        if not (page.parent.parent / "embed" / "index.html").exists():
            errors.append(f"{rel}: no embed page beside it")
        # The API saves a dashboard only for a version listed here.
        listed = page.parent / "versions.json"
        if not listed.exists() or json.loads(listed.read_text(encoding="utf-8")).get(
            "versions"
        ) != [v["version"] for v in data["versions"]]:
            errors.append(f"{rel}: explore/versions.json missing or out of step")
    report, wrong = examples(out, datasets)
    errors += wrong
    errors += periods_needed(out, datasets)
    errors += fetch_sequence(out)
    errors += query_explained(out, datasets)
    errors += _mcp_resources(out)
    errors += _mcp_listing(out)
    errors += _manifest(out)
    pages = []
    cache: dict[str, list[str]] = {}
    for html in sorted(out.rglob("*.html")):
        page = html.read_text(encoding="utf-8")
        pages.append((str(html.relative_to(out)), page))
        errors += structured.check_page(page, pages[-1][0])
        errors += _social_card(pages[-1][0], page, out)
        text = _without_publisher_values(PORTAL_TEXT.sub("", page), html, out, cache)
        if (
            "not endorsed" not in text
            and "has not endorsed" not in text
            and "no government agency" not in text.lower()
        ):
            errors.append(f"{html.relative_to(out)}: no not-endorsed statement")
        if "—" in text:
            errors.append(f"{html.relative_to(out)}: em-dash")
    errors += structured.duplicate_names(pages)
    return errors, report


def main(out: Path, register_dir: Path, absent: list[str] = (), site: bool = True) -> int:
    errors, report = checked(out, register_dir, absent, site)
    if site:
        picked = sum(1 for line in report if line.startswith("rules"))
        print(f"examples: {len(report)} dataset pages, {picked} picked by the rules")
        for line in report:
            print(f"  {line}")
    for e in errors:
        print(f"GATE: {e}", file=sys.stderr)
    print(f"gate: {'FAIL' if errors else 'PASS'} ({len(errors)} problems)")
    return 1 if errors else 0
