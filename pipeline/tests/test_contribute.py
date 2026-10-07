import re

import pytest

from publicdata import contribute
from publicdata.register import Licence, Source

from .conftest import make_dataset

SITE = "https://publicdata.au"


class Response:
    def __init__(self, body, links=None):
        self.body, self.links = body, links or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeSite:
    """The site's votes and catalogue API."""

    def __init__(self, votes, rows):
        self.votes, self.rows, self.asked = votes, rows, []

    def get(self, url, params=None, timeout=None):
        if url == f"{SITE}/api/v1/votes":
            return Response(self.votes)
        assert url == f"{SITE}/api/v1/catalogue"
        ids = params["ids"].split(",")
        self.asked.append(ids)
        assert len(ids) <= 50
        return Response({"rows": [r for r in self.rows if r["id"] in ids or r.get("vote") in ids]})


class FakeGitHub:
    def __init__(self, issues=()):
        self.store = {i["number"]: dict(i) for i in issues}
        self.writes = []

    def issues(self):
        return [dict(i) for i in self.store.values()]

    def create(self, title, body):
        n = max(self.store, default=0) + 1
        self.store[n] = {
            "number": n,
            "state": "open",
            "title": title,
            "body": body,
            "key": contribute.key_of(body),
        }
        self.writes.append(("create", n))
        return n

    def update(self, number, title, body):
        self.store[number].update(title=title, body=body, key=contribute.key_of(body))
        self.writes.append(("update", number))

    def close(self, number, comment, reason):
        self.store[number].update(state="closed", reason=reason, comment=comment)
        self.writes.append(("close", number))

    def open(self):
        return sorted(i["key"] for i in self.store.values() if i["state"] == "open")


def row(id, state="votable", **kw):
    return {
        "id": id,
        "title": f"Title of {id}",
        "publisher": "Department of Examples",
        "url": f"https://data.qld.gov.au/dataset/{id}",
        "licence": "CC BY 4.0",
        "state": state,
        "vote": id if state == "votable" else kw.pop("vote", ""),
        **kw,
    }


def backlog(slug, status="backlog", licence="CC-BY-4.0", **kw):
    return make_dataset(
        [],
        slug=slug,
        title=f"Entry {slug}",
        status=status,
        licence=Licence(licence, f"https://data.gov.au/dataset/{slug}", "Attribution"),
        source=Source(adapter="none", url=f"https://data.gov.au/data/dataset/{slug}"),
        **kw,
    )


def run(datasets, site, gh, votes=1, cap=10, **kw):
    return contribute.sync(datasets, gh, site, votes, cap, log=lambda *_: None, **kw)


def test_a_voted_open_record_and_a_backlog_entry_each_get_one_issue():
    site = FakeSite({"qld-a": 3, "qld-b": 1, "nsw-fuel": 2}, [row("qld-a"), row("qld-b")])
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], site, gh, votes=2)
    assert gh.open() == ["nsw-fuel", "qld-a"]
    body = next(i["body"] for i in gh.store.values() if i["key"] == "qld-a")
    assert body.startswith("<!-- publicdata-contribute: qld-a -->\n")
    assert "<https://data.qld.gov.au/dataset/qld-a>" in body
    assert "| Votes | 3 |" in body
    assert "CONTRIBUTING.md#add-a-dataset" in body
    assert "python -m publicdata register draft https://data.qld.gov.au/dataset/qld-a" in body
    entry = next(i["body"] for i in gh.store.values() if i["key"] == "nsw-fuel")
    assert "register/nsw-fuel.yaml" in entry and "status `backlog`" in entry


def test_the_github_client_labels_new_issues_and_reads_only_its_own_marked_issues():
    sent = []

    class Http:
        headers = {}

        def request(self, method, url, timeout=None, json=None):
            sent.append((method, url, json))
            return Response({"number": 9})

        def get(self, url, params=None, timeout=None):
            sent.append(("GET", url, params))
            mine = {"login": contribute.AUTHOR}
            if params is None:
                return Response(
                    [
                        {
                            "number": 4,
                            "state": "closed",
                            "title": "t",
                            "user": mine,
                            "body": contribute.marker("qld-z"),
                        }
                    ]
                )
            return Response(
                [
                    {"number": 1, "state": "open", "title": "t", "user": mine, "body": contribute.marker("qld-a") + "\nx"},
                    {"number": 2, "state": "open", "title": "t", "user": {"login": "someone"}, "body": contribute.marker("qld-b")},
                    {"number": 3, "state": "open", "title": "t", "user": mine, "body": contribute.marker("qld-c"), "pull_request": {}},
                    {"number": 5, "state": "open", "title": "t", "user": mine, "body": "no marker"},
                    {"number": 6, "state": "open", "title": "t", "user": mine, "body": "quoted <!-- publicdata-contribute: qld-d --> inline"},
                ],
                links={"next": {"url": "https://api.github.com/repos/o/r/issues?page=2"}},
            )  # fmt: skip

    gh = contribute.GitHub("o/r", Http())
    assert [(i["number"], i["key"]) for i in gh.issues()] == [(1, "qld-a"), (4, "qld-z")]
    assert gh.create("Add x", "body") == 9
    assert sent[-1] == (
        "POST",
        "https://api.github.com/repos/o/r/issues",
        {"title": "Add x", "body": "body", "labels": ["good first issue", "dataset"]},
    )
    gh.close(1, "done", "completed")
    assert sent[-1][2] == {"state": "closed", "state_reason": "completed"}


def test_a_second_run_leaves_the_same_issues_and_creates_nothing():
    datasets = [backlog("nsw-fuel")]
    site = FakeSite({"qld-a": 3}, [row("qld-a")])
    gh = FakeGitHub()
    run(datasets, site, gh)
    before = {n: dict(i) for n, i in gh.store.items()}
    gh.writes.clear()
    assert run(datasets, site, gh) == []
    assert gh.writes == [] and gh.store == before


def test_a_new_vote_count_updates_the_existing_issue():
    datasets = [backlog("nsw-fuel")]
    gh = FakeGitHub()
    run(datasets, FakeSite({"qld-a": 1}, [row("qld-a")]), gh)
    gh.writes.clear()
    run(datasets, FakeSite({"qld-a": 4}, [row("qld-a")]), gh)
    assert gh.writes == [("update", 1)]
    assert "| Votes | 4 |" in gh.store[1]["body"] and len(gh.store) == 2


@pytest.mark.parametrize("state", ["closed", "served", "chosen"])
def test_a_record_that_is_not_open_to_a_vote_gets_no_issue_whatever_its_votes(state):
    site = FakeSite({"qld-a": 500}, [row("qld-a", state=state, vote="nope")])
    gh = FakeGitHub()
    run([], site, gh)
    assert gh.store == {}


@pytest.mark.parametrize(
    "entry",
    [
        backlog("x-nd", licence="CC-BY-ND-4.0"),
        backlog("x-nc", licence="CC-BY-NC-4.0"),
        backlog("x-blocked", status="blocked"),
        backlog("x-assessing", status="assessing"),
        backlog("x-unknown", licence="other-closed"),
    ],
)
def test_a_closed_licence_or_a_blocked_entry_gets_no_issue_whatever_its_votes(entry):
    gh = FakeGitHub()
    run([entry], FakeSite({entry.slug: 500}, []), gh)
    assert gh.store == {}


def test_a_vote_key_the_catalogue_does_not_hold_never_reaches_an_issue():
    site = FakeSite({"qld-made-up": 9, "x" * 70: 9, "Bad Key": 9}, [])
    gh = FakeGitHub()
    run([], site, gh)
    assert gh.store == {}
    assert site.asked == [["qld-made-up"]]


def test_portal_text_is_shown_as_text():
    evil = row(
        "qld-a",
        title="Bins @octocat see #1 [click](https://evil.example) <img src=x> ```",
        publisher="Org\n## Heading",
    )
    gh = FakeGitHub()
    run([], FakeSite({"qld-a": 2}, [evil]), gh)
    (issue,) = gh.store.values()
    body = issue["body"]
    # Inside a code block nothing is a mention or a link; outside it everything is escaped.
    fence = re.search(r"^(`{3,})yaml$", body, re.M).group(1)
    assert len(fence) > 3
    body = re.sub(rf"^{fence}yaml$.*?^{fence}$", "", body, flags=re.M | re.S)
    assert "@octocat" not in body.replace("\\@octocat", "")
    assert "](https://evil.example)" not in body and "<img" not in body.replace("\\<img", "")
    assert "#1" not in body.replace("\\#1", "")
    assert "\n## Heading" not in body and "javascript:" not in body
    assert issue["title"].startswith("Add Bins @octocat") and "\n" not in issue["title"]
    # A record whose portal link is not a web address gets no issue.
    gh = FakeGitHub()
    run([], FakeSite({"qld-a": 2}, [{**evil, "url": "javascript:alert(1)"}]), gh)
    assert gh.store == {}


def test_no_run_leaves_more_issues_open_than_the_cap():
    rows = [row(f"qld-{c}") for c in "abcd"]
    votes = {"qld-a": 9, "qld-b": 7, "qld-c": 5, "qld-d": 3}
    gh = FakeGitHub()
    run([], FakeSite(votes, rows), gh, cap=2)
    assert gh.open() == ["qld-a", "qld-b"]
    # A lower cap closes the least wanted; a closed issue is not opened again.
    run([], FakeSite(votes, rows), gh, cap=1)
    assert gh.open() == ["qld-a"]
    assert gh.store[2]["reason"] == "not_planned"
    run([], FakeSite(votes, rows), gh, cap=3)
    assert gh.open() == ["qld-a", "qld-c", "qld-d"]
    run([], FakeSite(votes, rows), gh, cap=0)
    assert gh.open() == []


def test_an_issue_closes_when_its_dataset_goes_live():
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({"qld-a": 2}, [row("qld-a")]), gh)
    live = [backlog("nsw-fuel", status="live")]
    run(live, FakeSite({"qld-a": 2}, [row("qld-a", state="served", page=f"{SITE}/d/qld-a/")]), gh)
    assert gh.open() == []
    assert {i["reason"] for i in gh.store.values()} == {"completed"}
    comments = {i["key"]: i["comment"] for i in gh.store.values()}
    assert f"{SITE}/d/nsw-fuel/" in comments["nsw-fuel"]
    assert f"{SITE}/d/qld-a/" in comments["qld-a"]


def test_a_record_claimed_by_a_register_entry_keeps_its_one_issue():
    gh = FakeGitHub()
    run([], FakeSite({"qld-a": 2}, [row("qld-a")]), gh)
    claimed = [backlog("qld-entry")]
    site = FakeSite({"qld-entry": 2}, [row("qld-a", state="chosen", vote="qld-entry")])
    run(claimed, site, gh)
    assert gh.open() == ["qld-entry"] and list(gh.store) == [1]
    assert gh.store[1]["body"].startswith(contribute.marker("qld-entry"))
    gh.writes.clear()
    run(claimed, site, gh)
    assert gh.writes == []


def test_an_entry_that_leaves_the_backlog_closes_its_issue_as_not_planned():
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({}, []), gh)
    run([backlog("nsw-fuel", status="blocked")], FakeSite({}, []), gh)
    assert gh.store[1]["state"] == "closed" and gh.store[1]["reason"] == "not_planned"
    assert "blocked" in gh.store[1]["comment"]


def test_a_dry_run_changes_nothing():
    gh = FakeGitHub()
    printed = []
    acts = contribute.sync(
        [backlog("nsw-fuel")], gh, FakeSite({}, []), 1, 10, dry_run=True, log=printed.append
    )
    assert [a.kind for a in acts] == ["create"] and gh.writes == []
    assert any("would create" in p for p in printed)


def test_open_tasks_map_each_key_to_its_open_issue():
    issues = [
        {"number": 3, "state": "open", "key": "qld-a"},
        {"number": 1, "state": "closed", "key": "qld-b"},
        {"number": 2, "state": "open", "key": "nsw-fuel"},
    ]
    assert contribute.open_tasks(issues) == {"nsw-fuel": 2, "qld-a": 3}
