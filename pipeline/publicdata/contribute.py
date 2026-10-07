"""The most-wanted datasets as issues a contributor can start on (`publicdata contribute sync`).

Issues are written only from the register and from catalogue records the site's API returns; a
vote is a count under a key and nothing more. Each issue carries its dataset's key in a hidden
marker, so a rerun finds it, and only issues the sync's own account opened are ever changed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import yaml

from . import REPO, SITE
from .register import CLOSED_LICENCES, OPEN_LICENCES, Dataset

UA = "publicdata-contribute (+https://publicdata.au/)"
LABELS = ("good first issue", "dataset")
AUTHOR = "github-actions[bot]"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MARK_RE = re.compile(r"^<!-- publicdata-contribute: ([a-z0-9][a-z0-9-]{0,63}) -->$", re.M)
GUIDE = f"{REPO}/blob/main/CONTRIBUTING.md#add-a-dataset"
# The catalogue API answers at most this many ids a call; past the top voted few hundred a record
# cannot reach the cap anyway.
IDS_PER_CALL = 50
MOST_VOTED = 200


@dataclass(frozen=True)
class Candidate:
    key: str
    title: str
    publisher: str
    votes: int
    portal_url: str
    licence: str
    evidence: str
    planned: str = ""
    # A register entry's key is its slug and it has a status; a catalogue record's key is its id.
    status: str = ""


@dataclass(frozen=True)
class Action:
    kind: str  # create, update, close
    key: str
    number: int = 0
    title: str = ""
    body: str = ""
    comment: str = ""
    reason: str = ""  # completed or not_planned, for a close


def marker(key: str) -> str:
    return f"<!-- publicdata-contribute: {key} -->"


def key_of(body: str | None) -> str:
    m = MARK_RE.search(body or "")
    return m.group(1) if m else ""


def eligible(d: Dataset) -> bool:
    """A backlog entry whose licence the register already holds to be open."""
    return (
        d.status == "backlog"
        and d.licence.id in OPEN_LICENCES
        and d.licence.id not in CLOSED_LICENCES
    )


def from_register(d: Dataset, votes: int) -> Candidate:
    return Candidate(
        key=d.slug,
        title=d.title,
        publisher=d.publisher.name,
        votes=votes,
        portal_url=d.source.portal or d.source.url,
        licence=d.licence.title,
        evidence=d.licence.evidence,
        planned=d.planned,
        status=d.status,
    )


def from_record(row: dict, votes: int) -> Candidate:
    return Candidate(
        key=row["id"],
        title=row["title"],
        publisher=row["publisher"],
        votes=votes,
        portal_url=row["url"],
        licence=row["licence"],
        evidence=row["url"],
    )


def candidates(
    datasets: list[Dataset], votes: dict[str, int], rows: dict[str, dict], threshold: int
) -> list[Candidate]:
    """Every backlog entry with an open licence, and every catalogue record with at least
    `threshold` votes that is open, downloadable and not yet in the register, most voted first."""
    out = [from_register(d, votes.get(d.slug, 0)) for d in datasets if eligible(d)]
    slugs = {d.slug for d in datasets}
    out += [
        from_record(r, votes[k])
        for k, r in rows.items()
        if k not in slugs
        and r.get("id") == k
        and r.get("state") == "votable"
        and votes.get(k, 0) >= threshold
    ]
    return sorted(out, key=lambda c: (-c.votes, c.key))


def resolve(key: str, register: dict[str, Dataset], rows: dict[str, dict]) -> tuple[str, str, str]:
    """(the key the dataset is known by now, its state, a link or reason). The state is `live`,
    `open` (still a task) or `gone` (no longer one)."""
    if key in register:
        d = register[key]
        if d.status == "live":
            return key, "live", f"{SITE}/d/{key}/"
        if d.status in ("backlog", "building") and d.licence.id not in CLOSED_LICENCES:
            return key, "open", ""
        return key, "gone", f"its register entry is now {d.status}"
    r = rows.get(key)
    if r is None:
        return key, "gone", "the portals' catalogue no longer lists it"
    if r.get("state") == "chosen":
        if r.get("vote") in register:
            return resolve(r["vote"], register, rows)
        return key, "gone", "it is planned under another register entry"
    if r.get("state") == "served":
        return key, "live", _url(r.get("page") or "") or SITE
    if r.get("state") == "votable":
        return key, "open", ""
    return key, "gone", r.get("reason") or "its licence is not open"


def plan(
    cands: list[Candidate],
    issues: list[dict],
    register: dict[str, Dataset],
    rows: dict[str, dict],
    votes: dict[str, int],
    cap: int,
) -> list[Action]:
    """What a run does to the sync's own issues. A key that ever had an issue never gets a second
    one; a maintainer reopens the old one instead."""
    acts: list[Action] = []
    ever: set[str] = set()
    keep: dict[str, dict] = {}
    for i in sorted(issues, key=lambda i: i["number"]):
        key, state, detail = resolve(i["key"], register, rows)
        ever |= {i["key"], key}
        if i["state"] != "open":
            continue
        if state == "live":
            acts.append(
                _close(i, key, "completed", f"This dataset is live at {detail}. Thank you.")
            )
        elif state == "gone":
            acts.append(_close(i, key, "not_planned", f"Closed because {_text(detail, 200)}."))
        elif key in keep:
            acts.append(
                _close(i, key, "not_planned", f"Closed as a duplicate of #{keep[key]['number']}.")
            )
        else:
            keep[key] = i
    by_key = {c.key: c for c in cands}
    rank = {c.key: n for n, c in enumerate(cands)}
    order = sorted(keep, key=lambda k: (rank.get(k, len(rank)), keep[k]["number"]))
    for k in order[cap:]:
        acts.append(
            _close(
                keep.pop(k),
                k,
                "not_planned",
                f"Closed to keep the number of open dataset tasks at {cap}. A maintainer can "
                "reopen it.",
            )
        )
    for k, i in keep.items():
        c = by_key.get(k) or _current(k, register, rows, votes)
        if c is None:
            continue
        title, body = render(c)
        if (i["title"], i["body"]) != (title, body):
            acts.append(Action("update", k, number=i["number"], title=title, body=body))
    room = cap - len(keep)
    for c in cands:
        if room <= 0:
            break
        if c.key in ever or not (c.status or _url(c.portal_url)):
            continue
        title, body = render(c)
        acts.append(Action("create", c.key, title=title, body=body))
        room -= 1
    return acts


def _close(issue: dict, key: str, reason: str, comment: str) -> Action:
    return Action("close", key, number=issue["number"], comment=comment, reason=reason)


def _current(key, register, rows, votes) -> Candidate | None:
    """The candidate an open issue describes when its dataset is no longer ranked, such as an
    entry now building."""
    if key in register:
        return from_register(register[key], votes.get(key, 0))
    if key in rows:
        return from_record(rows[key], votes.get(key, 0))
    return None


# Text from a portal or the register is shown as text: no markup, link, mention or reference.
_MD = re.compile(r"([\\`*_{}\[\]()<>#@|!~&])")


def _text(s: str, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", s or "")).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "..."
    return _MD.sub(r"\\\1", s)


def _url(u: str) -> str:
    u = (u or "").strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or re.search(r"[\s<>\"'`]", u):
        return ""
    return u


def _fence(text: str) -> str:
    n = max([3, *(len(m) + 1 for m in re.findall(r"`+", text))])
    return "`" * n


def render(c: Candidate) -> tuple[str, str]:
    """The issue's title and body."""
    title = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", f"Add {c.title}")).strip()
    if len(title) > 120:
        title = title[:117].rsplit(" ", 1)[0] + "..."
    portal = _url(c.portal_url)
    evidence = _url(c.evidence)
    votes = f"{c.votes} vote{'' if c.votes == 1 else 's'}"
    lines = [
        marker(c.key),
        "",
        f"**{_text(c.title, 200)}**, from {_text(c.publisher, 120)}, is on the publicdata.au "
        f"[backlog]({SITE}/backlog/) with {votes}.",
        "",
        "| | |",
        "|---|---|",
        f"| Publisher | {_text(c.publisher, 120)} |",
        f"| Portal page | {f'<{portal}>' if portal else 'not given'} |",
        f"| Licence | {_text(c.licence, 80)} |",
        f"| Licence evidence | {f'<{evidence}>' if evidence else 'not given'} |",
        f"| Votes | {c.votes} |",
        "",
        "## Starting point",
        "",
    ]
    if c.status:
        lines += [
            f"The register already holds an entry, [`register/{c.key}.yaml`]"
            f"({REPO}/blob/main/register/{c.key}.yaml), at status `{c.status}`."
            + (f" Its note: {_text(c.planned, 400)}" if c.planned else ""),
            "",
            "Finish the entry from step 3 of "
            f"[Add a dataset]({GUIDE}): fields, key and partitions, labels, a local build and the "
            "gate, then `status: live`.",
        ]
    else:
        stub = yaml.safe_dump(
            {
                "title": re.sub(r"\s+", " ", c.title).strip(),
                "status": "building",
                "publisher": {"name": re.sub(r"\s+", " ", c.publisher).strip()},
                "licence": {"evidence": evidence},
                "source": {"url": portal},
            },
            sort_keys=False,
            allow_unicode=True,
            width=1000,
        )
        fence = _fence(stub)
        lines += [
            "Read the licence on the portal page first; it must be open, as "
            f"[Add a dataset]({GUIDE}) step 1 says. A first entry starts from what the catalogue "
            "holds:",
            "",
            f"{fence}yaml",
            stub.rstrip("\n"),
            fence,
            "",
        ]
        if _ckan(portal):
            lines += [
                "`register draft` writes the full entry, with every field typed from the file:",
                "",
                "```",
                f"python -m publicdata register draft {portal}",
                "```",
                "",
            ]
        lines += [f"Then follow [Add a dataset]({GUIDE}) from step 3."]
    lines += [
        "",
        "Say `Closes #<this issue>` in the pull request. This issue is kept up to date each day "
        "from the register, the portals' catalogue and the vote counts, and closes when the "
        "dataset is live.",
        "",
    ]
    return title, "\n".join(lines)


def _ckan(url: str) -> bool:
    from .register_draft import DraftError, portal_for

    try:
        portal_for(url)
    except DraftError:
        return False
    return bool(url)


# Network: the site's public API and GitHub's REST API, both through a session a test can replace.


def session(token: str = "") -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def read_votes(http: requests.Session, site: str = SITE) -> dict[str, int]:
    r = http.get(f"{site}/api/v1/votes", timeout=60)
    r.raise_for_status()
    return {k: int(v) for k, v in r.json().items() if KEY_RE.match(k) and int(v) > 0}


def read_records(http: requests.Session, ids: list[str], site: str = SITE) -> dict[str, dict]:
    """The catalogue rows for these record ids, by id. A chosen record is also found under the
    slug its votes go to."""
    out: dict[str, dict] = {}
    for n in range(0, len(ids), IDS_PER_CALL):
        part = ids[n : n + IDS_PER_CALL]
        r = http.get(f"{site}/api/v1/catalogue", params={"ids": ",".join(part)}, timeout=60)
        r.raise_for_status()
        for row in r.json()["rows"]:
            out[row["id"]] = row
    return out


class GitHub:
    """The few calls the sync makes. Issues it did not open, and pull requests, are left out."""

    def __init__(self, repo: str, http: requests.Session, author: str = AUTHOR):
        self.base = f"https://api.github.com/repos/{repo}"
        self.http = http
        self.author = author
        self.http.headers["Accept"] = "application/vnd.github+json"

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"{self.base}{path}", timeout=60, **kw)
        r.raise_for_status()
        return r

    def issues(self) -> list[dict]:
        out, url = [], f"{self.base}/issues"
        # Every issue is read and filtered here: a server-side filter that matched nothing would
        # make every dataset look new.
        params = {"state": "all", "per_page": 100}
        while url:
            r = self.http.get(url, params=params, timeout=60)
            r.raise_for_status()
            for i in r.json():
                key = key_of(i.get("body"))
                if (
                    key
                    and "pull_request" not in i
                    and (i.get("user") or {}).get("login") == self.author
                ):
                    out.append(
                        {
                            "number": i["number"],
                            "state": i["state"],
                            "title": i["title"],
                            "body": i.get("body") or "",
                            "key": key,
                        }
                    )
            url, params = r.links.get("next", {}).get("url"), None
        return out

    def create(self, title: str, body: str) -> int:
        r = self._call(
            "POST", "/issues", json={"title": title, "body": body, "labels": list(LABELS)}
        )
        return r.json()["number"]

    def update(self, number: int, title: str, body: str) -> None:
        self._call("PATCH", f"/issues/{number}", json={"title": title, "body": body})

    def close(self, number: int, comment: str, reason: str) -> None:
        self._call("POST", f"/issues/{number}/comments", json={"body": comment})
        self._call("PATCH", f"/issues/{number}", json={"state": "closed", "state_reason": reason})


def gather(
    datasets: list[Dataset], gh, http: requests.Session, site: str = SITE
) -> tuple[dict, dict, list[dict]]:
    """Votes, the catalogue rows the run needs, and the sync's issues."""
    votes = read_votes(http, site)
    issues = gh.issues()
    slugs = {d.slug for d in datasets}
    voted = sorted((k for k in votes if k not in slugs), key=lambda k: (-votes[k], k))[:MOST_VOTED]
    ids = sorted(set(voted) | {i["key"] for i in issues if i["key"] not in slugs})
    return votes, read_records(http, ids, site), issues


def sync(
    datasets: list[Dataset],
    gh,
    http: requests.Session,
    threshold: int,
    cap: int,
    dry_run: bool = False,
    site: str = SITE,
    log=print,
) -> list[Action]:
    votes, rows, issues = gather(datasets, gh, http, site)
    register = {d.slug: d for d in datasets}
    acts = plan(candidates(datasets, votes, rows, threshold), issues, register, rows, votes, cap)
    for a in acts:
        what = f"#{a.number}" if a.number else repr(a.title)
        log(f"contribute: {'would ' if dry_run else ''}{a.kind} {what} ({a.key})")
        if dry_run:
            if a.kind != "close":
                log(a.body)
            continue
        if a.kind == "create":
            log(f"contribute: opened #{gh.create(a.title, a.body)}")
        elif a.kind == "update":
            gh.update(a.number, a.title, a.body)
        else:
            gh.close(a.number, a.comment, a.reason)
    return acts


def open_tasks(issues: list[dict]) -> dict[str, int]:
    """Each dataset key with an open sync issue, for the pages' notice."""
    out: dict[str, int] = {}
    for i in sorted(issues, key=lambda i: i["number"]):
        if i["state"] == "open":
            out.setdefault(i["key"], i["number"])
    return dict(sorted(out.items()))
