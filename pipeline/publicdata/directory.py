"""The directory: a page for every government and every publisher, built from the catalogue.

A catalogue record never gets a page of its own. It is listed on its publisher's page, linked to
the portal, until it is serialised here and has a dataset page.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from . import REPO, SITE
from .catalogue import BY_CODE
from .provenance import OPERATOR_ORG
from .publishers import (
    JUR_NAME,
    JUR_SEGMENT,
    JURISDICTIONS,
    LISTED_KINDS,
    PORTAL_JUR,
    Publisher,
    for_dataset,
    org_key,
    resolve,
)

if TYPE_CHECKING:
    from .register import Dataset

HOST = SITE.replace("https://", "")
# A publisher page with fewer listed datasets than this, and nothing served, is kept out of the
# index: a page that lists one or two portal links adds nothing a search engine needs.
INDEX_MIN = 3
SHOWN = 200
LEVEL_TITLE = {
    "federal": "Agencies",
    "state": "Departments and agencies",
    "local": "Councils",
    "other": "Universities, research bodies and other organisations",
}
KIND_NOTE = {
    "geoscience": "exploration and survey reports",
    "map": "maps",
    "chart": "charts",
    "filter": "filtered views",
    "href": "links to other sites",
    "file": "files",
    "story": "stories",
}


def fmt(n: int) -> str:
    return f"{n:,}"


# The adjective a heading gains when a department's name does not say which government it serves.
JUR_ADJ = {
    "Cth": "Australian Government",
    "NSW": "NSW",
    "Vic": "Victorian",
    "Qld": "Queensland",
    "WA": "WA",
    "SA": "SA",
    "Tas": "Tasmanian",
    "ACT": "ACT",
    "NT": "NT",
}


def display(p: Publisher) -> str:
    name = f"{p.name} ({p.short})" if p.short and p.short != p.name else p.name
    if p.level not in ("federal", "state"):
        return name
    words = re.compile(
        {
            "Cth": r"\bAustralian?\b|\bNational\b|\bCommonwealth\b",
            "NSW": r"\bNSW\b|New South Wales",
            "Vic": r"\bVictorian?\b",
            "Qld": r"\bQueensland\b",
            "WA": r"\bWA\b|Western Australian?",
            "SA": r"\bSA\b|South Australian?",
            "Tas": r"\bTasmanian?\b",
            "ACT": r"\bACT\b|Canberra|Australian Capital Territory",
            "NT": r"\bNT\b|Northern Territory",
        }[p.jurisdiction]
    )
    return name if words.search(name) else f"{JUR_ADJ[p.jurisdiction]} {name}"


def join(items: list[str]) -> str:
    return (
        items[0]
        if len(items) == 1
        else ", ".join(items[:-1]) + " and " + items[-1]
        if items
        else ""
    )


def count(n: int, one: str, many: str | None = None) -> str:
    return f"{fmt(n)} {one if n == 1 else (many or one + 's')}"


@dataclass
class Directory:
    pubs: dict[tuple[str, str], Publisher]
    ds_pub: dict[str, Publisher]
    records: dict[str, list[dict]] = field(default_factory=dict)  # by publisher path
    served: dict[str, str] = field(default_factory=dict)  # record id -> path of its page here
    chosen: dict[str, str] = field(default_factory=dict)  # record id -> register slug not built yet
    as_at: str = ""
    unread: dict[str, dict] = field(default_factory=dict)  # portal code -> harvest stats
    jur_rows: list[dict] = field(default_factory=list)  # filled by render, for the home page
    tasks: dict[str, int] = field(default_factory=dict)  # vote key -> open contributor issue

    def task_url(self, key: str) -> str:
        """The open contributor issue for a register slug or record id, or ''."""
        n = self.tasks.get(key) or self.tasks.get(self.chosen.get(key, ""))
        if not n:
            n = next((v for k, v in self.tasks.items() if self.chosen.get(k) == key), 0)
        return f"{REPO}/issues/{int(n)}" if n else ""

    def notes(self, jurisdiction: str | None = None) -> list[str]:
        """A sentence for each portal the latest harvest could not read."""
        out = []
        for code, st in sorted(self.unread.items()):
            portal = BY_CODE.get(code)
            if not portal or (jurisdiction and PORTAL_JUR[portal.jurisdiction] != jurisdiction):
                continue
            host = portal.host.removeprefix("www.")
            then = st.get("carried_from")
            out.append(
                f"The {host} catalogue could not be read on {_long(self.as_at)}, so "
                + (
                    f"its datasets are listed as they stood on {_long(then)}."
                    if then
                    else "its datasets are not listed yet."
                )
            )
        return out

    @property
    def votable(self) -> int:
        return sum(
            1
            for rs in self.records.values()
            for r in rs
            if r["kind"] in LISTED_KINDS
            and r["open"]
            and r["downloadable"]
            and r["id"] not in self.served
        )

    @property
    def listed(self) -> int:
        return sum(1 for rs in self.records.values() for r in rs if r["kind"] in LISTED_KINDS)

    def jur_path(self, code: str) -> str:
        return f"/{JUR_SEGMENT[code]}/"


def plan(
    datasets: list[Dataset],
    records: list[dict],
    curated: list[Publisher],
    as_at: str,
    stats: dict | None = None,
):
    portal_jur = {p.code: p.jurisdiction for p in BY_CODE.values()}
    pubs, by_org = resolve(records, curated, portal_jur)
    by_name = {(r["portal"], r["name"]): r for r in records}
    portal_by_host = {p.host.removeprefix("www."): p.code for p in BY_CODE.values()}
    d = Directory(
        pubs=pubs,
        ds_pub={},
        as_at=as_at,
        unread={k: v for k, v in (stats or {}).items() if isinstance(v, dict) and v.get("error")},
    )
    by_id = {r["id"]: r for r in records}
    by_url = {_bare(r["url"]): r for r in records if r.get("url")}
    for ds in datasets:
        d.ds_pub[ds.slug] = for_dataset(ds, pubs, by_org, by_name, portal_by_host)
        host = (ds.source.portal or "").split("://")[-1].removeprefix("www.").split("/")[0]
        rec = by_name.get((portal_by_host.get(host), ds.source.package))
        if ds.status in ("live", "building"):
            # A record named by its id in the source URL; a bare URL is too loose to mark served.
            rec = rec or find(ds.source.url, by_name, by_id, {}, portal_by_host)
        if rec and ds.status in ("live", "building"):
            d.served.setdefault(
                rec["id"], f"/c/{ds.collection}/" if ds.collection else f"/d/{ds.slug}/"
            )
        elif ds.status in ("backlog", "assessing", "blocked"):
            rec = rec or find(ds.source.url, by_name, by_id, by_url, portal_by_host)
            if rec:
                d.chosen.setdefault(rec["id"], ds.slug)
    for r in records:
        d.records.setdefault(by_org[org_key(r)].path, []).append(r)
    return d


def _bare(url: str) -> str:
    u = urlparse(url.strip())
    return (u.hostname or "").removeprefix("www.") + u.path.rstrip("/")


# Hosts people browse a portal on, mapped to the host its records are listed under.
PORTAL_HOSTS = {
    "dataexplorer.abs.gov.au": "data.api.abs.gov.au",
    "explore.data.abs.gov.au": "data.api.abs.gov.au",
}


def locate(url: str) -> tuple[str, str, str] | None:  # noqa: PLR0911 - one return per way a record is placed
    """What a pasted portal URL names.

    The answer is (host, "name", package name), (host, "id", record id) or (host, "url", bare
    url). functions/_catalogue.js does the same and the tests hold them together.
    """
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    host = PORTAL_HOSTS.get(host, host)
    m = re.search(r"/datasets?/([^/?#]+)", u.path)
    if m:
        return host, "name", m.group(1).lower()
    m = re.search(r"(?:^|/)([a-z0-9]{4}-[a-z0-9]{4})(?:/|$)", u.path)
    if m:
        return host, "source", m.group(1)
    df = parse_qs(u.query).get("df[id]")
    if df:
        return host, "source", df[0]
    m = re.search(r"/data/[^/,]+,([^/,]+),", u.path)
    if m:
        return host, "source", m.group(1)
    return host, "url", _bare(url)


def find(url, by_name, by_id, by_url, portal_by_host) -> dict | None:
    hit = locate(url or "")
    if not hit:
        return None

    host, kind, value = hit
    code = portal_by_host.get(host)
    rec = None
    if kind == "name" and code:
        rec = by_name.get((code, value)) or by_id.get(record_id(code, value))
    elif kind == "source" and code:
        rec = by_id.get(record_id(code, value))
    return rec or by_url.get(_bare(url))


def search_rows(d: Directory) -> list[dict]:
    """One row per listed catalogue record for the search index in D1.

    `vote` is the key a vote goes under: the record's id, or the register slug when the record is
    already chosen.
    """
    pub_by_path = {p.path: p for p in d.pubs.values()}
    out = []
    for path, recs in d.records.items():
        pub = pub_by_path[path]
        for r in recs:
            if r["kind"] not in LISTED_KINDS:
                continue
            if r["id"] in d.served:
                state, vote, note = "served", "", d.served[r["id"]]
            elif r["id"] in d.chosen:
                state, vote, note = "chosen", d.chosen[r["id"]], ""
            elif r["open"] and r["downloadable"]:
                state, vote, note = "votable", r["id"], ""
            else:
                state, vote, note = "closed", "", _reason(r)
            out.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "summary": r["summary"]
                    if r["summary"] not in ("No notes provided", r["title"])
                    else "",
                    "publisher": pub.name,
                    "publisher_path": path,
                    "jur": JUR_SEGMENT[pub.jurisdiction],
                    "portal": r["portal"],
                    "host": BY_CODE[r["portal"]].host.removeprefix("www."),
                    "name": (r.get("name") or "").lower(),
                    "url": r["url"],
                    "licence": licence_name(r["licence"]),
                    "formats": ", ".join(r["formats"][:6]),
                    "modified": r["modified"] or "",
                    "state": state,
                    "vote": vote,
                    "note": note,
                }
            )
    return sorted(out, key=lambda x: x["id"])


LICENCE_NAMES = {
    "CC0-1.0": "CC0 (public domain)",
    "PDM": "Public domain",
    "PDDL-1.0": "Public domain (PDDL)",
    "ODC-BY-1.0": "ODC Attribution",
    "ODBL-1.0": "ODbL",
    "other-open": "Open, on the portal's own terms",
    "other-at": "Open with attribution, on the portal's own terms",
}


def licence_name(lic: str | None) -> str:
    """A licence id as people read it: CC-BY-3.0-AU is CC BY 3.0 AU."""
    if not lic:
        return "none stated"
    if lic in LICENCE_NAMES:
        return LICENCE_NAMES[lic]
    m = re.fullmatch(r"CC-(BY(?:-(?:SA|NC|ND))*)-(\d\.\d)(-AU)?", lic)
    if m:
        return f"CC {m.group(1)} {m.group(2)}{' AU' if m.group(3) else ''}"
    return lic


def _reason(r: dict) -> str:
    if r["open"] is None:
        return "no licence stated"
    if not r["open"]:
        return "licence not open"
    return "no download"


def _row(r: dict, served: dict[str, str], chosen: dict[str, str], task: str = "") -> dict:
    fmts = r["formats"]
    return {
        "id": r["id"],
        "title": r["title"],
        "url": r["url"],
        "summary": r["summary"] if r["summary"] != "No notes provided" else "",
        "formats": ", ".join(fmts[:SHOWN_FORMATS])
        + (f" +{len(fmts) - SHOWN_FORMATS}" if len(fmts) > SHOWN_FORMATS else ""),
        "licence": licence_name(r["licence"]),
        "modified": r["modified"],
        "served": served.get(r["id"], ""),
        "candidate": bool(r["open"] and r["downloadable"]),
        "vote": chosen.get(r["id"], r["id"]),
        "reason": _reason(r),
        "portal": BY_CODE[r["portal"]].host.removeprefix("www."),
        "task": "" if r["id"] in served else task,
    }


def _stats(recs: list[dict], live: int) -> dict:
    listed = [r for r in recs if r["kind"] in LISTED_KINDS]
    return {
        "records": len(recs),
        "listed_n": len(listed),
        "listed": fmt(len(listed)),
        "candidates_n": sum(1 for r in listed if r["open"] and r["downloadable"]),
        "candidates": fmt(sum(1 for r in listed if r["open"] and r["downloadable"])),
        "live": live,
    }


def _other_kinds(recs: list[dict], portals: list[str]) -> str:
    kinds = Counter(r["kind"] for r in recs if r["kind"] not in LISTED_KINDS)
    if not kinds:
        return ""
    parts = [f"{fmt(n)} {KIND_NOTE.get(k, k)}" for k, n in kinds.most_common()]
    where = " and ".join(portals)
    return f"The catalogue on {where} also holds {', '.join(parts)} from this publisher, which are counted but not listed here."


def shard(record_id: str) -> str:
    """The vote endpoint reads one small file to check an id, named by portal and first character."""
    portal, rest = record_id.split("-", 1)
    return f"{portal}-{rest[0]}"


def render(d: Directory, page, write, live_rows: dict[str, dict], breadcrumbs) -> list[str]:  # noqa: C901, PLR0912, PLR0915 - the directory's pages in the order they are written
    """Writes the browse page, one page per government and one per publisher, and their JSON.

    Returns the URLs that belong in the sitemap.
    """
    urls: list[str] = []
    # Before the first harvest there is no catalogue: pages show what is served, make no claim
    # about the portals and stay out of the index.
    have = any(d.records.values())
    pub_by_path = {p.path: p for p in d.pubs.values()}
    live_by_pub: dict[str, list[dict]] = defaultdict(list)
    for slug, row in live_rows.items():
        p = d.ds_pub[slug]
        live_by_pub[p.path].append({**row, "publisher_path": p.path})
    as_at_long = _long(d.as_at)
    pub_index = []
    jur_pubs: dict[str, list[dict]] = defaultdict(list)
    for (jur, _slug), p in sorted(d.pubs.items(), key=lambda kv: kv[1].name.lower()):
        recs = sorted(d.records.get(p.path, []), key=lambda r: r["title"].lower())
        recs.sort(key=lambda r: r["modified"] or "", reverse=True)
        live = live_by_pub.get(p.path, [])
        if not recs and not live:
            continue
        portals = sorted({BY_CODE[r["portal"]].host.removeprefix("www.") for r in recs})
        st = _stats(recs, len([x for x in live if x["latest"]]))
        listed = [r for r in recs if r["kind"] in LISTED_KINDS]
        rows = [_row(r, d.served, d.chosen, d.task_url(r["id"])) for r in listed]
        # A record with an open contributor issue is listed even past the newest SHOWN.
        rows = rows[:SHOWN] + [r for r in rows[SHOWN:] if r["task"]]
        index = have and (st["listed_n"] >= INDEX_MIN or st["live"] > 0 or len(recs) >= INDEX_MIN)
        entry = {
            "slug": p.slug,
            "path": p.path,
            "url": SITE + p.path,
            "name": p.name,
            "short": p.short,
            "jurisdiction": jur,
            "level": p.level,
            "portals": portals,
            "orgs": sorted(p.orgs),
            "listed": st["listed_n"],
            "candidates": st["candidates_n"],
            "live": st["live"],
            "listed_fmt": st["listed"],
            "candidates_fmt": st["candidates"],
        }
        pub_index.append(entry)
        jur_pubs[jur].append(entry)
        heading = f"{display(p)} open data"
        holds = (
            count(st["listed_n"], "dataset")
            if st["listed_n"] or not recs
            else count(
                len(recs),
                KIND_NOTE.get(recs[0]["kind"], "record"),
                KIND_NOTE.get(recs[0]["kind"], "records"),
            )
        )
        n, c = st["listed_n"], st["candidates_n"]
        intro = (
            f"It lists {holds} on {join(portals) or 'no portal we read'}."
            + (
                f" {fmt(c)} of them {'has' if c == 1 else 'have'} an open licence and a file or API we can read."
                if n
                else ""
            )
            + (
                f" {count(st['live'], 'table')} from its releases {'is' if st['live'] == 1 else 'are'} served here in every format with version history."
                if st["live"]
                else ""
            )
        )
        if not have:
            intro = (
                "It publishes the datasets below, served here in every format with version history."
            )
        intro = f"{_who(p, jur)} {intro}{_listing_note(listed, live)}"
        crumbs = [
            ("Datasets", SITE + "/"),
            (JUR_NAME[jur], SITE + d.jur_path(jur)),
            (p.label, SITE + p.path),
        ]
        jsonld = {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": heading,
            "url": SITE + p.path,
            "about": {
                "@type": "GovernmentOrganization",
                "name": p.name,
                **({"alternateName": p.short} if p.short else {}),
                **({"url": p.url} if p.url else {}),
                "areaServed": {"@type": "AdministrativeArea", "name": JUR_NAME[jur]},
            },
            "isPartOf": {"@type": "WebSite", "name": HOST, "url": SITE + "/"},
            "provider": OPERATOR_ORG,
        }
        md = "\n".join(
            [
                "---",
                f"title: {heading}",
                f"resource: {SITE}{p.path}",
                f"jurisdiction: {JUR_NAME[jur]}",
                f"catalogue_read: {d.as_at}",
                "not_endorsed: true",
                "---",
                "",
                f"# {heading}",
                "",
                intro,
                "",
                *(
                    [
                        "## Served here",
                        "",
                        *[f"- [{x['title']}]({SITE}/d/{x['slug']}/): {x['summary']}" for x in live],
                        "",
                    ]
                    if live
                    else []
                ),
                *(
                    [
                        "## On the portals",
                        "",
                        *[
                            f"- [{r['title']}]({r['url']}) ({r['formats'] or 'no files'}; {r['licence']}; updated {r['modified'] or 'unknown'})"
                            + (f" Open as a contributor task: {r['task']}" if r["task"] else "")
                            for r in rows
                        ],
                        "",
                        f"Full list: {SITE}{p.path}catalogue.json",
                        "",
                    ]
                    if rows
                    else []
                ),
            ]
        )
        page(
            p.path.strip("/") + "/index.html",
            "publisher.html",
            md,
            title=f"{heading}: {holds} | {HOST}" if have else f"{heading} | {HOST}",
            description=(
                f"Every dataset {p.name} lists on {join(portals) or 'its portal'}, with licence, formats and "
                f"update date. {st['candidates']} are open with a download, {st['live']} are served here as CSV, "
                "Excel, JSON and Parquet."
                if have
                else f"Datasets from {p.name} served here as CSV, Excel, JSON and Parquet, with version history."
            ),
            nav="browse",
            noindex=not index,
            catalogue=have,
            heading=heading,
            intro=intro,
            pub=p,
            jur={"name": JUR_NAME[jur]},
            jur_path=d.jur_path(jur),
            stats={**st, "shown": min(len(rows), SHOWN)},
            portals=portals,
            live=live,
            rows=rows,
            other_kinds=_other_kinds(recs, portals),
            as_at_long=as_at_long,
            jsonld=json.dumps(jsonld, ensure_ascii=False),
            extra_jsonld=[json.dumps(breadcrumbs(crumbs), ensure_ascii=False)],
        )
        write(
            p.path.strip("/") + "/catalogue.json",
            json.dumps(
                {
                    "publisher": {
                        k: entry[k]
                        for k in (
                            "name",
                            "short",
                            "jurisdiction",
                            "level",
                            "url",
                            "portals",
                            "orgs",
                        )
                    },
                    "catalogue_read": d.as_at,
                    "other_kinds": dict(
                        Counter(r["kind"] for r in recs if r["kind"] not in LISTED_KINDS)
                    ),
                    "records": [
                        {
                            **{
                                k: r[k]
                                for k in (
                                    "id",
                                    "title",
                                    "url",
                                    "licence",
                                    "open",
                                    "formats",
                                    "downloadable",
                                    "modified",
                                )
                            },
                            "served_at": SITE + d.served[r["id"]] if r["id"] in d.served else None,
                            "vote": d.chosen.get(r["id"], r["id"])
                            if r["open"] and r["downloadable"] and r["id"] not in d.served
                            else None,
                        }
                        for r in listed
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
        )
        if index:
            urls.append(SITE + p.path)

    jur_rows = []
    for code, seg, jname in JURISDICTIONS:
        entries = jur_pubs.get(code, [])
        if not entries:
            continue
        recs = [r for e in entries for r in d.records.get(e["path"], [])]
        live = [x for e in entries for x in live_by_pub.get(e["path"], [])]
        st = _stats(recs, len([x for x in live if x["latest"]]))
        portals = sorted({pt for e in entries for pt in e["portals"]})
        groups = []
        most = max((e["listed"] for e in entries), default=0)
        for level in ("federal", "state", "local", "other"):
            ps = sorted(
                (e for e in entries if e["level"] == level),
                key=lambda e: (-e["listed"], e["name"].lower()),
            )
            for e in ps:
                e["bar"] = _bar(e["listed"], most)
            if ps:
                groups.append({"title": LEVEL_TITLE[level], "publishers": ps})
        heading = f"{jname} open data" if code != "Cth" else "Australian Government open data"
        intro = (
            f"{count(len(entries), 'publisher')} in {jname if code != 'Cth' else 'the Australian Government'} list "
            f"{count(st['listed_n'], 'dataset')} on {join(portals)}. {st['candidates']} have an open licence and a file or API "
            f"we can read. Each publisher's page lists its datasets with their licence and formats, and a vote on one "
            "asks us to serialise it."
        )
        if not have:
            intro = (
                f"The datasets served here from {jname if code != 'Cth' else 'the Australian Government'}, by publisher. "
                "A list of every dataset on the portals is being gathered and will appear on this page."
            )
        jsonld = {
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": heading,
            "url": SITE + f"/{seg}/",
            "about": {"@type": "AdministrativeArea", "name": jname},
            "isPartOf": {"@type": "WebSite", "name": HOST, "url": SITE + "/"},
        }
        md = "\n".join(
            [
                "---",
                f"title: {heading}",
                f"resource: {SITE}/{seg}/",
                f"catalogue_read: {d.as_at}",
                "---",
                "",
                f"# {heading}",
                "",
                intro,
                "",
                *[
                    line
                    for g in groups
                    for line in (
                        f"## {g['title']}",
                        "",
                        *[
                            (
                                f"- [{e['name']}]({e['url']}): {count(e['listed'], 'dataset')} listed, {fmt(e['candidates'])} open with a download, {fmt(e['live'])} served here"
                                if have
                                else f"- [{e['name']}]({e['url']}): {fmt(e['live'])} served here"
                            )
                            for e in g["publishers"]
                        ],
                        "",
                    )
                ],
            ]
        )
        page(
            f"{seg}/index.html",
            "jurisdiction.html",
            md,
            title=(
                f"{heading}: {count(st['listed_n'], 'dataset')} from {count(len(entries), 'publisher')} | {HOST}"
                if have
                else f"{heading} | {HOST}"
            ),
            description=(
                f"Every open dataset {jname if code != 'Cth' else 'Australian Government'} agencies list on "
                f"{join(portals)}, by publisher, with licence and formats. {st['live']} are served here as CSV, Excel, JSON and Parquet."
                if have
                else f"Datasets from {jname if code != 'Cth' else 'the Australian Government'} served here as CSV, Excel, JSON and Parquet, by publisher."
            ),
            nav="browse",
            notes=d.notes(code),
            catalogue=have,
            noindex=not have,
            heading=heading,
            intro=intro,
            jur={"name": jname},
            stats={**st, "publishers": fmt(len(entries))},
            groups=groups,
            live=live,
            as_at_long=as_at_long,
            jsonld=json.dumps(jsonld, ensure_ascii=False),
            extra_jsonld=[
                json.dumps(
                    breadcrumbs(
                        [
                            ("Datasets", SITE + "/"),
                            ("By government", SITE + "/browse/"),
                            (jname, SITE + f"/{seg}/"),
                        ]
                    ),
                    ensure_ascii=False,
                )
            ],
        )
        if have:
            urls.append(SITE + f"/{seg}/")
        jur_rows.append(
            {
                "code": code,
                "path": f"/{seg}/",
                "name": jname,
                "portals": portals,
                "publishers": fmt(len(entries)),
                "publishers_n": len(entries),
                "listed": st["listed"],
                "listed_n": st["listed_n"],
                "candidates": st["candidates"],
                "live": st["live"],
            }
        )
    top = max((j["listed_n"] for j in jur_rows), default=0)
    for j in jur_rows:
        j["bar"] = _bar(j["listed_n"], top)
    d.jur_rows = jur_rows

    total = _stats([r for rs in d.records.values() for r in rs], sum(j["live"] for j in jur_rows))
    heading = "Australian government open data by government"
    if have:
        intro = (
            f"Every dataset the Australian Government, the states and territories and their councils list on their open-data "
            f"portals: {total['listed']} datasets from {fmt(len(pub_index))} publishers. {total['candidates']} have an open licence and a file "
            "or API we can read. Pick a government to see its publishers."
        )
    else:
        intro = (
            "The datasets served here, by the government that publishes them. A list of every dataset on "
            "Australia's government portals is being gathered and will appear here."
        )
    page(
        "browse/index.html",
        "browse.html",
        "\n".join(
            [
                "---",
                f"title: {heading}",
                f"resource: {SITE}/browse/",
                f"catalogue_read: {d.as_at}",
                "---",
                "",
                f"# {heading}",
                "",
                intro,
                "",
                *[
                    (
                        f"- [{j['name']}]({SITE}{j['path']}): {count(j['publishers_n'], 'publisher')}, {j['listed']} datasets listed, {j['candidates']} open with a download"
                        if have
                        else f"- [{j['name']}]({SITE}{j['path']}): {j['live']} served here"
                    )
                    for j in jur_rows
                ],
                "",
            ]
        ),
        title=(
            f"Australian government open data: {total['listed']} datasets by government and publisher | {HOST}"
            if have
            else f"Australian government open data by government | {HOST}"
        ),
        description=(
            f"{total['listed']} datasets listed on Australia's government open-data portals, by government and publisher, with licences and formats."
            if have
            else "Australian government datasets served here as CSV, Excel, JSON and Parquet, by the government that publishes them."
        ),
        nav="browse",
        notes=d.notes(),
        catalogue=have,
        noindex=not have,
        heading=heading,
        intro=intro,
        jurisdictions=jur_rows,
        as_at_long=as_at_long,
        jsonld=json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": heading,
                "url": SITE + "/browse/",
            },
            ensure_ascii=False,
        ),
        extra_jsonld=[
            json.dumps(
                breadcrumbs([("Datasets", SITE + "/"), ("By government", SITE + "/browse/")]),
                ensure_ascii=False,
            )
        ],
    )
    if have:
        urls.insert(0, SITE + "/browse/")
    votable: dict[str, dict] = defaultdict(dict)
    for path, recs in d.records.items():
        pub = pub_by_path.get(path)
        for r in recs:
            if (
                r["kind"] in LISTED_KINDS
                and r["open"]
                and r["downloadable"]
                and r["id"] not in d.served
                and r["id"] not in d.chosen
            ):
                votable[shard(r["id"])][r["id"]] = [
                    r["title"],
                    path,
                    r["url"],
                    pub.name if pub else "",
                ]
    for key, ids in sorted(votable.items()):
        write(
            f"catalogue/votable/{key}.json",
            json.dumps(ids, ensure_ascii=False, sort_keys=True) + "\n",
        )
    write("catalogue/aliases.json", json.dumps(d.chosen, sort_keys=True) + "\n")
    write(
        "catalogue/publishers.json",
        json.dumps(
            {"catalogue_read": d.as_at, "publishers": pub_index}, ensure_ascii=False, indent=1
        )
        + "\n",
    )
    return urls


LEVEL_NOUN = {
    "federal": "an Australian Government agency",
    "state": "a {jur} government body",
    "local": "a council in {jur}",
    "other": "an organisation in {jur}",
}


def _who(p: Publisher, jur: str) -> str:
    """The first sentence of a publisher page: what kind of body it is and where."""
    noun = LEVEL_NOUN[p.level].format(jur=JUR_NAME[jur])
    site = f", at {p.url.split('/')[2]}" if p.url and "://" in p.url else ""
    return f"{p.name} is {noun}{site}."


def _listing_note(rows: list[dict], live: list[dict]) -> str:
    """What the listing holds.

    That is the topics served here, else the file types the portal lists and when a listing last
    changed.
    """
    # Only a dataset with a built version is served; a new entry waits for its first fetch.
    topics = sorted({t for x in live if x.get("live", True) for t in x.get("topics", [])})
    if topics:
        return f" Its datasets served here cover {join(topics)}."
    fmts = Counter(f for r in rows for f in (r.get("formats") or []))
    newest = max((r.get("modified") or "" for r in rows), default="")
    parts = []
    if fmts:
        top = [f for f, _ in fmts.most_common(3)]
        parts.append(f"Most of its files are {join(top)}")
    if newest:
        parts.append(f"the newest listing changed on {_long(newest)}")
    return f" {', and '.join(parts)}." if parts else ""


def _bar(n: int, top: int) -> int:
    """A bar length on a log scale, so a small government still shows beside the Commonwealth."""
    if n <= 0 or top <= 1:
        return 0
    return max(4, round(100 * math.log10(n + 1) / math.log10(top + 1)))


def _long(iso: str) -> str:
    if not iso:
        return "never"

    x = dt.date.fromisoformat(iso[:10])
    return f"{x.day} {x.strftime('%B %Y')}"
