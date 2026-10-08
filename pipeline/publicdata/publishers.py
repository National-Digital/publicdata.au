"""Jurisdictions and publishers: the two tiers above a dataset.

Every organisation in the catalogue belongs to one publisher. `register/publishers/*.yaml` names
the ones that need more than their portal gives them: a jurisdiction other than the portal's,
one agency listed on two portals, a short name or a website. Every other organisation is its own
publisher, named as its portal names it, in its portal's jurisdiction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from .register import Dataset, RegisterError

if TYPE_CHECKING:
    from pathlib import Path

# Register code, URL segment, long name.
JURISDICTIONS = (
    ("Cth", "cth", "Australian Government"),
    ("NSW", "nsw", "New South Wales"),
    ("Vic", "vic", "Victoria"),
    ("Qld", "qld", "Queensland"),
    ("WA", "wa", "Western Australia"),
    ("SA", "sa", "South Australia"),
    ("Tas", "tas", "Tasmania"),
    ("ACT", "act", "Australian Capital Territory"),
    ("NT", "nt", "Northern Territory"),
)
JUR_SEGMENT = {code: seg for code, seg, _ in JURISDICTIONS}
JUR_NAME = {code: name for code, _, name in JURISDICTIONS}
PORTAL_JUR = {seg: code for code, seg, _ in JURISDICTIONS}
LEVELS = ("federal", "state", "local", "other")
LOCAL_RE = re.compile(
    r"\b(council|shire|city of|town of|municipal|municipality|borough|regional council)\b",
    re.IGNORECASE,
)
# Words portals append to an organisation's name that are not part of it.
NOISE_RE = re.compile(r"\s*('s data hub|\bopen data hub\b|\bopen data\b)\s*$", re.IGNORECASE)
LISTED_KINDS = ("dataset", "dataflow")
OTHER_RE = re.compile(
    r"\b(universit\w*|school of|college|institute of technology|pty\.? ?ltd|limited|ltd|sip register|foundation|association"
    r"|incorporated|inc\.|data network|research infrastructure|observing system|data discovery)\b",
    re.IGNORECASE,
)
# One aggregator republishes other bodies' records as "Government of X - Agency".
PREFIX_RE = re.compile(
    r"^(?P<local>Local )?Government of (?P<gov>the Commonwealth of Australia|Victoria|New South Wales|"
    r"NSW|Queensland|South Australia|Western Australia|Tasmania|the Northern Territory|"
    r"the Australian Capital Territory|The ACT) - (?P<name>.+)$"
    r"|^Australian Government - (?P<cth>.+)$"
    r"|^(?P<gov2>Victoria State|Northern Territory|NSW|Queensland|ACT|Tasmanian|South Australian|"
    r"Western Australian) Government - (?P<name2>.+)$"
)
PREFIX_JUR = {
    "NSW": "NSW",
    "The ACT": "ACT",
    "Victoria State": "Vic",
    "Northern Territory": "NT",
    "ACT": "ACT",
    "Tasmanian": "Tas",
    "South Australian": "SA",
    "Western Australian": "WA",
    "the Commonwealth of Australia": "Cth",
    "Victoria": "Vic",
    "New South Wales": "NSW",
    "Queensland": "Qld",
    "South Australia": "SA",
    "Western Australia": "WA",
    "Tasmania": "Tas",
    "the Northern Territory": "NT",
    "the Australian Capital Territory": "ACT",
}
PLACE_JUR = (
    (re.compile(r"\b(Tasmanian?|Hobart|Launceston|the LIST)\b", re.IGNORECASE), "Tas"),
    (re.compile(r"\b(NSW|New South Wales|Sydney)\b"), "NSW"),
    (re.compile(r"\b(Victorian?|Melbourne|Geelong|Ballarat|Bendigo)\b"), "Vic"),
    (re.compile(r"\b(Queensland|Brisbane|Gold Coast|Moreton Bay|Townsville|Ipswich)\b"), "Qld"),
    (re.compile(r"\b(South Australian?|Adelaide)\b"), "SA"),
    (re.compile(r"\b(Western Australian?|Perth)\b"), "WA"),
    (re.compile(r"\b(Northern Territory|Darwin)\b"), "NT"),
    (re.compile(r"\b(ACT|Canberra|Australian Capital Territory)\b"), "ACT"),
)
COUNCIL_FORM_RE = re.compile(
    r"\b(city of|town of|shire|municipal|borough|city council|regional council|rural city|town council)\b",
    re.IGNORECASE,
)
COUNCIL_WORDS = re.compile(
    r"\b(city of|town of|shire of|municipality of|council|shire|city|regional|rural|municipal|"
    r"borough|town|open data|data hub|'s)\b",
    re.IGNORECASE,
)


SLUG_MAX = 80


def slugify(text: str) -> str:
    s = re.sub("[\u2019']", "", text.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > SLUG_MAX:
        s = s[: SLUG_MAX + 1].rsplit("-", 1)[0]
    return s or "unnamed"


def clean_title(title: str) -> str:
    return NOISE_RE.sub("", re.sub(r"\s+", " ", title or "").strip()).strip() or title.strip()


@dataclass
class Publisher:
    slug: str
    name: str
    jurisdiction: str
    level: str
    short: str = ""
    url: str = ""
    orgs: list[str] = field(default_factory=list)
    curated: bool = False

    @property
    def path(self) -> str:
        return f"/{JUR_SEGMENT[self.jurisdiction]}/{self.slug}/"

    @property
    def label(self) -> str:
        return self.short or self.name


def load_curated(folder: Path) -> list[Publisher]:  # noqa: C901 - one check per curated field
    """One file per government, each a list of publishers."""
    raw = []
    for p in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        raw += [
            (p.name, i, e)
            for i, e in enumerate(yaml.safe_load(p.read_text(encoding="utf-8")) or [])
        ]
    out, seen_slug, seen_org = [], set(), {}
    for fname, i, e in raw:
        ctx = f"publishers/{fname}[{i}]"
        for k in ("slug", "name", "jurisdiction"):
            if not e.get(k):
                msg = f"{ctx}: {k} is required"
                raise RegisterError(msg)
        jur = str(e["jurisdiction"])
        if jur not in JUR_SEGMENT:
            msg = f"{ctx}: jurisdiction '{jur}' is not one of {list(JUR_SEGMENT)}"
            raise RegisterError(msg)
        slug = str(e["slug"])
        if slug != slugify(slug):
            msg = f"{ctx}: slug '{slug}' must be lower case words and hyphens"
            raise RegisterError(msg)
        if (jur, slug) in seen_slug:
            msg = f"{ctx}: {jur}/{slug} appears twice"
            raise RegisterError(msg)
        seen_slug.add((jur, slug))
        level = str(e.get("level") or ("federal" if jur == "Cth" else "state"))
        if level not in LEVELS:
            msg = f"{ctx}: level '{level}' is not one of {LEVELS}"
            raise RegisterError(msg)
        orgs = [str(o) for o in e.get("orgs") or []]
        for o in orgs:
            if not re.match(r"^[a-z]+:[^\s]+$", o):
                msg = f"{ctx}: org '{o}' must be portal:organisation"
                raise RegisterError(msg)
            if o in seen_org:
                msg = f"{ctx}: org '{o}' is already under {seen_org[o]}"
                raise RegisterError(msg)
            seen_org[o] = slug
        out.append(
            Publisher(
                slug=slug,
                name=str(e["name"]),
                jurisdiction=jur,
                level=level,
                short=str(e.get("short") or ""),
                url=str(e.get("url") or ""),
                orgs=orgs,
                curated=True,
            )
        )
    return out


def org_key(rec: dict) -> str:
    return f"{rec['portal']}:{rec['org'] or 'unknown'}"


def resolve(records: list[dict], curated: list[Publisher], portal_jur: dict[str, str]):
    """Returns (publishers by (jurisdiction, slug), publisher for each org key)."""
    pubs: dict[tuple[str, str], Publisher] = {(p.jurisdiction, p.slug): p for p in curated}
    by_org: dict[str, Publisher] = {o: p for p in curated for o in p.orgs}
    titles: dict[str, str] = {}
    for r in records:
        titles.setdefault(org_key(r), r.get("org_title") or r.get("org") or "Unknown")
    for key in sorted(titles):
        if key in by_org:
            continue
        portal = key.split(":", 1)[0]
        jur = PORTAL_JUR[portal_jur[portal]]
        name = clean_title(titles[key])
        slug = slugify(name)
        p = pubs.get((jur, slug))
        if p is None:
            level = (
                "local"
                if LOCAL_RE.search(name)
                else "other"
                if OTHER_RE.search(name)
                else ("federal" if jur == "Cth" else "state")
            )
            p = pubs[(jur, slug)] = Publisher(slug=slug, name=name, jurisdiction=jur, level=level)
        p.orgs.append(key)
        by_org[key] = p
    return pubs, by_org


def for_dataset(
    ds: Dataset,
    pubs: dict[tuple[str, str], Publisher],
    by_org: dict[str, Publisher],
    records_by_name: dict[tuple[str, str], dict],
    portal_by_host: dict[str, str],
) -> Publisher:
    """The publisher page a register dataset sits under.

    It is found through the dataset's catalogue record when the catalogue holds it, then by the
    publisher's name, and otherwise the dataset gets a page of its own.
    """
    host = re.sub(r"^https?://(www\.)?", "", ds.source.portal or ds.source.url).split("/")[0]
    portal = portal_by_host.get(host)
    rec = records_by_name.get((portal, ds.source.package)) if portal else None
    if rec and org_key(rec) in by_org:
        return by_org[org_key(rec)]
    jur = ds.publisher.jurisdiction if ds.publisher.jurisdiction in JUR_SEGMENT else "Cth"
    want = {ds.publisher.name.lower(), ds.publisher.short.lower()}
    for (j, _), p in sorted(pubs.items()):
        if j == jur and (p.name.lower() in want or (p.short and p.short.lower() in want)):
            return p
    slug = slugify(ds.publisher.name)
    p = pubs.get((jur, slug))
    if p is None:
        p = pubs[(jur, slug)] = Publisher(
            slug=slug,
            name=ds.publisher.name,
            jurisdiction=jur,
            level="federal" if jur == "Cth" else "state",
            short=ds.publisher.short if ds.publisher.short != ds.publisher.name else "",
            url=ds.publisher.url,
        )
    return p


def _council_base(name: str) -> str:
    return re.sub(r"\s+", " ", COUNCIL_WORDS.sub(" ", name)).strip(" -").lower()


def suggest(records: list[dict], curated: list[Publisher], lgas: dict[str, str]) -> list[dict]:  # noqa: C901, PLR0912, PLR0915 - one rule per way an organisation is matched
    """Proposed curation for the organisations that the portal cannot place.

    These are the organisations data.gov.au and the Infrastructure catalogue list. The proposal
    gives the jurisdiction, the level, a cleaned name and a merge with the same body on a state
    portal. `lgas` maps a council area's name to its jurisdiction. Output is for review, never
    applied automatically.
    """
    claimed = {o for p in curated for o in p.orgs}
    titles: dict[str, str] = {}
    for r in records:
        if r["kind"] in LISTED_KINDS:
            titles.setdefault(org_key(r), r.get("org_title") or r["org"])
    state_orgs = {}
    for key, t in titles.items():
        if key.split(":")[0] not in ("gov", "infra", "abs"):
            state_orgs.setdefault(re.sub(r"[^a-z]", "", clean_title(t).lower()), key)
    lga_by_base = {}
    for n, jur in lgas.items():
        base = re.sub(r"\s*\((nsw|vic\.|qld|sa|wa|tas\.|nt|act)\)$", "", n.lower())
        lga_by_base.setdefault(base, set()).add(jur)
    out = []
    for key, raw in sorted(titles.items()):
        portal = key.split(":")[0]
        if portal not in ("gov", "infra") or key in claimed:
            continue
        name, jur, level, why = clean_title(raw), "Cth", "federal", []
        m = PREFIX_RE.match(name)
        if m:
            name = m["name"] or m["cth"] or m["name2"]
            gov = m["gov"] or m["gov2"]
            jur = PREFIX_JUR[gov] if gov else "Cth"
            level = "local" if m["local"] else ("federal" if jur == "Cth" else "state")
            why.append("prefix")
        if OTHER_RE.search(name):
            level = "other"
            why.append("research or other")
        if jur == "Cth" and "CSIRO" not in name:
            for rx, j in PLACE_JUR:
                if rx.search(name):
                    jur, why = j, [*why, "place name"]
                    if level == "federal":
                        level = "state"
                    break
        hit = lga_by_base.get(_council_base(name)) if LOCAL_RE.search(name) else None
        if not hit and name.lower() in lga_by_base:
            hit = lga_by_base[name.lower()]  # a council that names itself by its area alone
        if hit and len(hit) == 1:
            jur, level, why = next(iter(hit)), "local", [*why, "council area"]
        elif hit:
            level, why = "local", [*why, f"council area in {sorted(hit)}"]
        elif COUNCIL_FORM_RE.search(name):
            level, why = "local", [*why, "council name"]
        if jur != "Cth" and level == "federal":
            level = "state"
        # A shared name only merges within one government: "Department of Human Services" is a
        # federal and a state department.
        merge = state_orgs.get(re.sub(r"[^a-z]", "", name.lower()))
        if merge and PORTAL_JUR[merge.split(":")[0]] != jur:
            why.append(f"same name as {merge}, not merged")
            merge = None
        elif merge:
            why.append(f"same body as {merge}")
        if jur == "Cth" and level == "federal" and name == raw.strip() and not merge:
            continue
        out.append(
            {
                "org": key,
                "title": raw,
                "name": name,
                "jurisdiction": jur,
                "level": level,
                "merge": merge or "",
                "why": why,
            }
        )
    return out
