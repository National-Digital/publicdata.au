import re
from typing import TYPE_CHECKING, ClassVar, NotRequired, cast

import pytest
import requests

from publicdata import contribute
from publicdata.register import Licence, Source

from .conftest import make_dataset, present

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import TypedDict, Unpack

    from publicdata.contribute import Issue, Row
    from publicdata.register import Dataset, Status

    class Stored(Issue):
        reason: NotRequired[str]
        comment: NotRequired[str]

    class RowOptions(TypedDict, total=False):
        title: str
        publisher: str
        url: str
        vote: str
        page: str


SITE = "https://publicdata.au"


class Response:
    def __init__(self, body: object, links: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.body, self.links = body, links or {}

    def json(self) -> object:
        return self.body

    def raise_for_status(self) -> None:
        pass


def http(fake: object) -> requests.Session:
    # A stand-in that answers only the calls contribute makes of a session.
    return cast("requests.Session", fake)


class FakeSite:
    """The site's votes and catalogue API."""

    def __init__(self, votes: Mapping[str, int], rows: Iterable[Row]) -> None:
        self.votes, self.rows = votes, list(rows)
        self.asked: list[list[str]] = []

    def get(
        self, url: str, params: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> Response:
        if url == f"{SITE}/api/v1/votes":
            return Response(self.votes)
        assert url == f"{SITE}/api/v1/catalogue"
        ids = present(params)["ids"].split(",")
        self.asked.append(ids)
        assert len(ids) <= contribute.IDS_PER_CALL
        return Response({"rows": [r for r in self.rows if r["id"] in ids or r.get("vote") in ids]})


class FakeGitHub(contribute.GitHub):
    # The parent's session is never used: every call it would make is answered here.
    def __init__(self, issues: Iterable[Stored] = ()) -> None:
        self.store: dict[int, Stored] = {i["number"]: i.copy() for i in issues}
        self.writes: list[tuple[str, int]] = []

    def issues(self, state: str = "all") -> list[Issue]:
        return [i.copy() for i in self.store.values()]

    def create(self, title: str, body: str) -> int:
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

    def update(self, number: int, title: str, body: str) -> None:
        self.store[number].update({"title": title, "body": body, "key": contribute.key_of(body)})
        self.writes.append(("update", number))

    def close(self, number: int, comment: str, reason: str) -> None:
        self.store[number].update({"state": "closed", "reason": reason, "comment": comment})
        self.writes.append(("close", number))

    def open(self) -> list[str]:
        return sorted(i["key"] for i in self.store.values() if i["state"] == "open")


def row(key: str, state: str = "votable", **kw: Unpack[RowOptions]) -> Row:
    base: Row = {
        "id": key,
        "title": f"Title of {key}",
        "publisher": "Department of Examples",
        "url": f"https://data.qld.gov.au/dataset/{key}",
        "licence": "CC BY 4.0",
        "state": state,
        "vote": key if state == "votable" else kw.pop("vote", ""),
    }
    out: Row = {**base, **kw}
    return out


def backlog(
    slug: str, status: Status = "backlog", licence: str = "CC-BY-4.0", planned: str = ""
) -> Dataset:
    return make_dataset(
        [],
        slug=slug,
        title=f"Entry {slug}",
        status=status,
        licence=Licence(licence, f"https://data.gov.au/dataset/{slug}", "Attribution"),
        source=Source(adapter="none", url=f"https://data.gov.au/data/dataset/{slug}"),
        planned=planned,
    )


def run(
    datasets: list[Dataset], site: FakeSite, gh: FakeGitHub, votes: int = 1, cap: int = 10
) -> list[contribute.Action]:
    return contribute.sync(datasets, gh, http(site), votes, cap, log=lambda *_: None)


def test_a_voted_open_record_and_a_backlog_entry_each_get_one_issue() -> None:
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
    assert "register/nsw-fuel.yaml" in entry
    assert "status `backlog`" in entry


def test_the_github_client_labels_new_issues_and_reads_only_its_own_marked_issues() -> None:
    sent: list[tuple[str, str, object]] = []
    applied = 2

    class Http:
        headers: ClassVar[dict[str, str]] = {}

        def request(
            self,
            method: str,
            url: str,
            timeout: float | None = None,
            json: Mapping[str, object] | None = None,
        ) -> Response:
            sent.append((method, url, json))
            asked = (json or {}).get("labels", [])
            assert isinstance(asked, list)
            labels = [{"name": n} for n in asked[:applied]]
            return Response({"number": 9, "labels": labels})

        def get(
            self, url: str, params: Mapping[str, object] | None = None, timeout: float | None = None
        ) -> Response:
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

    gh = contribute.GitHub("o/r", http(Http()))
    assert [(i["number"], i["key"]) for i in gh.issues()] == [(1, "qld-a"), (4, "qld-z")]
    assert gh.create("Add x", "body") == 9
    assert sent[-1] == (
        "POST",
        "https://api.github.com/repos/o/r/issues",
        {"title": "Add x", "body": "body", "labels": ["good first issue", "dataset"]},
    )
    # A label GitHub dropped fails the change, so the run goes red.
    applied = 1
    with pytest.raises(contribute.LabelMissing, match="dataset"):
        gh.create("Add x", "body")
    gh.close(1, "done", "completed")
    assert [x[2] for x in sent[-2:]] == [
        {"state": "closed", "state_reason": "completed"},
        {"body": "done"},
    ]


def test_a_second_run_leaves_the_same_issues_and_creates_nothing() -> None:
    datasets = [backlog("nsw-fuel")]
    site = FakeSite({"qld-a": 3}, [row("qld-a")])
    gh = FakeGitHub()
    run(datasets, site, gh)
    before = {n: dict(i) for n, i in gh.store.items()}
    gh.writes.clear()
    assert run(datasets, site, gh) == []
    assert gh.writes == []
    assert gh.store == before


def test_a_new_vote_count_updates_the_existing_issue() -> None:
    datasets = [backlog("nsw-fuel")]
    gh = FakeGitHub()
    run(datasets, FakeSite({"qld-a": 1}, [row("qld-a")]), gh)
    gh.writes.clear()
    run(datasets, FakeSite({"qld-a": 4}, [row("qld-a")]), gh)
    assert gh.writes == [("update", 1)]
    assert "| Votes | 4 |" in gh.store[1]["body"]
    assert len(gh.store) == 2


@pytest.mark.parametrize("state", ["closed", "served", "chosen"])
def test_a_record_that_is_not_open_to_a_vote_gets_no_issue_whatever_its_votes(state: str) -> None:
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
def test_a_closed_licence_or_a_blocked_entry_gets_no_issue_whatever_its_votes(
    entry: Dataset,
) -> None:
    gh = FakeGitHub()
    run([entry], FakeSite({entry.slug: 500}, []), gh)
    assert gh.store == {}


def test_a_vote_key_the_catalogue_does_not_hold_never_reaches_an_issue() -> None:
    site = FakeSite({"qld-made-up": 9, "x" * 70: 9, "Bad Key": 9}, [])
    gh = FakeGitHub()
    run([], site, gh)
    assert gh.store == {}
    assert site.asked == [["qld-made-up"]]


def test_portal_text_is_shown_as_text() -> None:
    evil = row(
        "qld-a",
        title="Bins @octocat see #1 [click](https://evil.example) www.evil.example <img src=x> ```",
        publisher="Org\n## Heading | x",
    )
    gh = FakeGitHub()
    run([], FakeSite({"qld-a": 2}, [evil]), gh)
    (issue,) = gh.store.values()
    body = issue["body"]
    # GitHub links, mentions and marks up nothing inside a code block or a code span.
    fence = present(re.search(r"^(`{3,})yaml$", body, re.MULTILINE)).group(1)
    assert len(fence) > 3
    prose = re.sub(rf"^{fence}yaml$.*?^{fence}$", "", body, flags=re.MULTILINE | re.DOTALL)
    prose = re.sub(r"(`+) .*? \1", "", prose)
    for bad in ("@octocat", "evil.example", "<img", "#1", "## Heading", "Org"):
        assert bad not in prose
    assert "| Publisher | ` Org ## Heading \\| x ` |" in body
    assert issue["title"].startswith("Add Bins @octocat")
    assert "\n" not in issue["title"]
    # A record whose portal link is not a web address gets no issue.
    gh = FakeGitHub()
    unsafe: Row = {**evil, "url": "javascript:alert(1)"}
    run([], FakeSite({"qld-a": 2}, [unsafe]), gh)
    assert gh.store == {}


def test_a_marker_saved_with_windows_line_ends_is_still_found() -> None:
    assert contribute.key_of(contribute.marker("qld-a") + "\r\nedited\r\n") == "qld-a"
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({}, []), gh)
    gh.store[1]["body"] = gh.store[1]["body"].replace("\n", "\r\n")
    gh.writes.clear()
    run([backlog("nsw-fuel")], FakeSite({}, []), gh)
    assert gh.writes == []


def test_an_entry_whose_licence_is_no_longer_open_loses_its_issue() -> None:
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({}, []), gh)
    run([backlog("nsw-fuel", licence="other-closed")], FakeSite({}, []), gh)
    assert gh.store[1]["state"] == "closed"
    assert gh.store[1].get("reason") == "not_planned"


def test_an_entry_merged_before_the_catalogue_catches_up_takes_over_the_records_issue() -> None:
    url = "https://data.gov.au/data/dataset/qld-entry"
    site = FakeSite({"gov-a": 2}, [row("gov-a", url=url)])
    gh = FakeGitHub()
    run([], site, gh)
    # The register has the entry; the deployed catalogue still lists the record as votable.
    run([backlog("qld-entry")], site, gh)
    assert list(gh.store) == [1]
    assert gh.open() == ["qld-entry"]
    # With no issue yet, the record and the entry make one issue, not two.
    gh = FakeGitHub()
    run([backlog("qld-entry")], site, gh)
    assert gh.open() == ["qld-entry"]


def test_one_refused_change_does_not_stop_the_others() -> None:
    class Refusing(FakeGitHub):
        def update(self, number: int, title: str, body: str) -> None:
            msg = "403 locked"
            raise requests.HTTPError(msg)

    gh = Refusing()
    run([backlog("a-entry")], FakeSite({}, []), gh)
    with pytest.raises(contribute.SyncError, match="a-entry"):
        run([backlog("a-entry", planned="Changed."), backlog("b-entry")], FakeSite({}, []), gh)
    assert gh.open() == ["a-entry", "b-entry"]


def test_no_run_leaves_more_issues_open_than_the_cap() -> None:
    rows = [row(f"qld-{c}") for c in "abcd"]
    votes = {"qld-a": 9, "qld-b": 7, "qld-c": 5, "qld-d": 3}
    gh = FakeGitHub()
    run([], FakeSite(votes, rows), gh, cap=2)
    assert gh.open() == ["qld-a", "qld-b"]
    # A lower cap closes the least wanted; a closed issue is not opened again.
    run([], FakeSite(votes, rows), gh, cap=1)
    assert gh.open() == ["qld-a"]
    assert gh.store[2].get("reason") == "not_planned"
    run([], FakeSite(votes, rows), gh, cap=3)
    assert gh.open() == ["qld-a", "qld-c", "qld-d"]
    run([], FakeSite(votes, rows), gh, cap=0)
    assert gh.open() == []


def test_an_issue_closes_when_its_dataset_goes_live() -> None:
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({"qld-a": 2}, [row("qld-a")]), gh)
    live = [backlog("nsw-fuel", status="live")]
    run(live, FakeSite({"qld-a": 2}, [row("qld-a", state="served", page=f"{SITE}/d/qld-a/")]), gh)
    assert gh.open() == []
    assert {i.get("reason") for i in gh.store.values()} == {"completed"}
    comments = {i["key"]: i.get("comment") for i in gh.store.values()}
    assert f"{SITE}/d/nsw-fuel/" in present(comments["nsw-fuel"])
    assert f"{SITE}/d/qld-a/" in present(comments["qld-a"])


def test_a_record_claimed_by_a_register_entry_keeps_its_one_issue() -> None:
    gh = FakeGitHub()
    run([], FakeSite({"qld-a": 2}, [row("qld-a")]), gh)
    claimed = [backlog("qld-entry")]
    site = FakeSite({"qld-entry": 2}, [row("qld-a", state="chosen", vote="qld-entry")])
    run(claimed, site, gh)
    assert gh.open() == ["qld-entry"]
    assert list(gh.store) == [1]
    assert gh.store[1]["body"].startswith(contribute.marker("qld-entry"))
    gh.writes.clear()
    run(claimed, site, gh)
    assert gh.writes == []


def test_an_entry_that_leaves_the_backlog_closes_its_issue_as_not_planned() -> None:
    gh = FakeGitHub()
    run([backlog("nsw-fuel")], FakeSite({}, []), gh)
    run([backlog("nsw-fuel", status="blocked")], FakeSite({}, []), gh)
    assert gh.store[1]["state"] == "closed"
    assert gh.store[1].get("reason") == "not_planned"
    assert "blocked" in present(gh.store[1].get("comment"))


def test_a_dry_run_changes_nothing() -> None:
    gh = FakeGitHub()
    printed: list[str] = []
    acts = contribute.sync(
        [backlog("nsw-fuel")], gh, http(FakeSite({}, [])), 1, 10, dry_run=True, log=printed.append
    )
    assert [a.kind for a in acts] == ["create"]
    assert gh.writes == []
    assert any("would create" in p for p in printed)


def test_open_tasks_map_each_key_to_its_open_issue() -> None:
    issues: list[Issue] = [
        {"number": 3, "state": "open", "title": "", "body": "", "key": "qld-a"},
        {"number": 1, "state": "closed", "title": "", "body": "", "key": "qld-b"},
        {"number": 2, "state": "open", "title": "", "body": "", "key": "nsw-fuel"},
    ]
    assert contribute.open_tasks(issues) == {"nsw-fuel": 2, "qld-a": 3}
