import copy
import io
import socket
from contextlib import redirect_stderr, redirect_stdout

import pytest

from publicdata import tdqs


def _tools():
    return copy.deepcopy(tdqs.tools())


def _by_name(ts, name):
    return next(t for t in ts if t["name"] == name)


def _drop(t, sentence):
    t["description"] = " ".join(s for s in tdqs.sentences(t["description"]) if s != sentence)


def _run(ts):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = tdqs.check(ts)
    return rc, out.getvalue(), err.getvalue()


def test_the_current_tool_definitions_pass_offline(monkeypatch):
    def refuse(*a, **k):
        msg = "the check opened a connection"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert tdqs.problems(tdqs.tools())[0] == []
    assert tdqs.main(["check"]) == 0


def test_two_runs_print_the_same_bytes(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    ts = _tools()
    _by_name(ts, "query_rows")["inputSchema"]["properties"]["limit"]["description"] = ""
    assert _run(ts) == _run(copy.deepcopy(ts))
    assert _run(_tools()) == _run(_tools())


def test_a_parameter_without_a_description_names_the_tool_and_parameter(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    ts = _tools()
    del _by_name(ts, "query_rows")["inputSchema"]["properties"]["limit"]["description"]
    rc, _, err = _run(ts)
    assert rc == 1
    assert "query_rows: parameters: parameter limit needs a description" in err


@pytest.mark.parametrize(
    "quality,sentence",
    [
        ("limits", "Each call costs one of the 60 queries each address may make in 10 seconds."),
        ("returns", "Up to 50 matches come back in one answer, with no paging."),
        ("usage", "If nothing matches, try fewer or broader words, then search_catalogue."),
    ],
)
def test_removing_the_sentence_for_a_quality_fails_that_quality(quality, sentence):
    ts = _tools()
    t = _by_name(ts, "search_datasets")
    assert sentence in tdqs.sentences(t["description"])
    _drop(t, sentence)
    errors = tdqs.problems(ts)[0]
    assert [e.split(":")[:2] for e in errors] == [["search_datasets", f" {quality}"]]


RETURN_SENTENCES = {
    "search_datasets": ["Up to 50 matches come back in one answer, with no paging."],
    "get_dataset": ["The whole history comes back in one answer, with no paging."],
    "list_fields": ["The answer describes the newest version, in one page."],
    "list_partitions": ["Every value comes back in one answer, with no paging."],
    "query_rows": [
        "One page holds up to limit rows, and offset steps through the rest until next_offset "
        "is null."
    ],
    "count_rows": [
        "Groups come back largest first, and truncated means raise limit or narrow where."
    ],
    "diff_versions": [
        "One answer holds the counts of rows added, removed, changed and unchanged, the keys of "
        "the rows added, removed and changed, and up to ten changed rows with their old and new "
        "values."
    ],
    "search_catalogue": ["A page holds 20 records, those that can take a vote first."],
    "list_backlog": [
        "Votes on catalogue records that no entry has claimed yet come back apart, under the vote "
        "key search_catalogue uses.",
        "The whole backlog comes back in one answer, with no paging, and the call costs nothing "
        "against the rate limit.",
    ],
    "upvote_dataset": ["The answer gives the dataset's vote total after this call."],
}


def test_every_tool_has_its_return_sentence_listed():
    assert set(RETURN_SENTENCES) == {t["name"] for t in tdqs.tools()}


@pytest.mark.parametrize("name", sorted(RETURN_SENTENCES))
def test_removing_the_return_sentence_fails_returns_for_every_tool(name):
    ts = _tools()
    t = _by_name(ts, name)
    for sentence in RETURN_SENTENCES[name]:
        assert sentence in tdqs.sentences(t["description"])
        _drop(t, sentence)
    assert any(e.startswith(f"{name}: returns:") for e in tdqs.problems(ts)[0])


def test_removing_the_purpose_sentence_fails_purpose():
    ts = _tools()
    t = _by_name(ts, "get_dataset")
    _drop(t, tdqs.sentences(t["description"])[0])
    assert any(e.startswith("get_dataset: purpose:") for e in tdqs.problems(ts)[0])


@pytest.mark.parametrize(
    "quality,change",
    [
        (
            "purpose",
            lambda t: t.update(title="query rows.") or t["annotations"].update(title="query rows."),
        ),
        ("parameters", lambda t: t["inputSchema"].pop("additionalProperties")),
        ("parameters", lambda t: t["inputSchema"]["properties"]["offset"].pop("minimum")),
        ("parameters", lambda t: t["inputSchema"]["required"].append("missing")),
        ("returns", lambda t: t.update(outputSchema={})),
        ("annotations", lambda t: t["annotations"].pop("idempotentHint")),
        ("annotations", lambda t: t["annotations"].update(destructiveHint=True)),
        ("annotations", lambda t: t["annotations"].update(title="Something else")),
        ("length", lambda t: t.update(description=t["description"] + " More." * 100)),
        ("length", lambda t: t.update(description=t["description"] + " And" + " it" * 40 + ".")),
    ],
)
def test_each_quality_fails_on_its_own(quality, change):
    ts = _tools()
    change(_by_name(ts, "query_rows"))
    errors = tdqs.problems(ts)[0]
    assert errors and all(e.startswith(f"query_rows: {quality}:") for e in errors), errors


def test_a_read_only_tool_that_says_it_writes_fails():
    ts = _tools()
    t = _by_name(ts, "upvote_dataset")
    t["annotations"]["readOnlyHint"] = True
    assert any(
        e.startswith("upvote_dataset: annotations:") and "opens with 'Add'" in e
        for e in tdqs.problems(ts)[0]
    )


def test_an_inconsistent_name_fails_for_the_tool_and_a_duplicate_fails_the_set():
    ts = _tools()
    t = _by_name(ts, "diff_versions")
    t["name"] = "diffVersions"
    errors = tdqs.problems(ts)[0]
    assert "diffVersions: naming: name 'diffVersions' should be lowercase" in errors[0]

    ts = _tools()
    ts.append(copy.deepcopy(_by_name(ts, "count_rows")))
    errors = tdqs.problems(ts)[0]
    assert "tool set: naming: count_rows is used by 2 tools" in errors
    assert (
        "tool set: disambiguation: count_rows and count_rows open with near the same purpose"
        in errors
    )


def test_similar_tools_must_name_each_other():
    ts = _tools()
    q, c = _by_name(ts, "query_rows"), _by_name(ts, "count_rows")
    for t, other in ((q, "count_rows"), (c, "query_rows")):
        t["description"] = " ".join(s for s in tdqs.sentences(t["description"]) if other not in s)
    errors = tdqs.problems(ts)[0]
    assert (
        "tool set: disambiguation: query_rows and count_rows have similar purposes and neither "
        "names the other; say when to use each"
    ) in errors


def test_too_many_tools_fails_the_set():
    ts = _tools()
    base = _by_name(ts, "list_backlog")
    for i in range(tdqs.MAX_TOOLS):
        extra = copy.deepcopy(base)
        extra["name"] = f"list_backlog_{'x' * (i + 1)}"
        extra["title"] = f"Backlog {i}"
        extra["annotations"]["title"] = extra["title"]
        extra["description"] = (
            f"Return entry number {i} of a long list of things. "
            + base["description"].split(". ", 1)[1]
        )
        ts.append(extra)
    assert any(e.startswith("tool set: tool count:") for e in tdqs.problems(ts)[0])


def test_the_step_summary_carries_the_report(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert _run(_tools())[0] == 0
    assert summary.read_text().startswith("### Tool definitions\n\n```\nsearch_datasets    pass\n")
