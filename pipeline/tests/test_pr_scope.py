"""Which pull requests skip the checks that test code: those that only add store data."""

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
import yaml

from .conftest import ROOT

if TYPE_CHECKING:
    from pathlib import Path

ACTION = ROOT / ".github" / "actions" / "pr-scope" / "action.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "bash"
SKIPS = "needs.scope.outputs.code != 'false'"
# The jobs a data-only pull request skips, by workflow.
GATED = {
    "ci.yml": {"pipeline", "tdqs", "determinism", "accessibility", "functions", "workflows"},
    "clients.yml": {"python", "r"},
    "codeql.yml": {"analyze"},
}
# The required checks that read the data or the pull request itself, which every one must pass.
DATA_CHECKS = {
    "Register validates",
    "deploy",
    "Check title",
    "Check sign-off",
    "Secret scan (gitleaks)",
    "Review dependency changes",
}

# (action, path, text or destination): write, rm or mv.
Op = tuple[str, str, str]
EMPTY = "{}\n"


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(repo.parent), GIT_CONFIG_NOSYSTEM="1")
    subprocess.run([GIT, *args], cwd=repo, env=env, check=True, capture_output=True)


def _write(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)


def _commit(repo: Path, ops: list[Op], message: str) -> None:
    for action, path, arg in ops:
        if action == "write":
            _write(repo, path, arg)
        elif action == "rm":
            _git(repo, "rm", "-q", path)
        else:
            (repo / arg).parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "mv", path, arg)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "Test")
    _git(r, "config", "user.email", "test@example.org")
    _commit(
        r,
        [
            ("write", "store/a/2026-01-01/manifest.json", EMPTY),
            ("write", "store/hubs.json", EMPTY),
            ("write", "pipeline/x.py", "x = 1\n"),
        ],
        "base",
    )
    _git(r, "checkout", "-q", "-b", "pr")
    return r


def _scope(repo: Path, ops: list[Op], *, event: str = "pull_request", merge: bool = True) -> str:
    """The action's output for a pull request making these changes, read at GitHub's test merge."""
    _commit(repo, ops, "pull request")
    if merge:
        # main moves on after the branch, so the test merge's first parent holds code the PR lacks.
        _git(repo, "checkout", "-q", "main")
        _commit(repo, [("write", "functions/y.mjs", "export {};\n")], "main moves on")
        _git(repo, "merge", "-q", "--no-ff", "-m", "test merge", "pr")
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    script = next(s["run"] for s in steps if s.get("id") == "scope")
    out = repo.parent / "output"
    out.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(EVENT=event, GITHUB_OUTPUT=str(out), HOME=str(repo.parent), GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    lines = out.read_text().splitlines()
    assert len(lines) == 1, lines
    return lines[0].removeprefix("code=")


@pytest.mark.parametrize(
    "ops",
    [
        [("write", "store/a/2026-02-01/manifest.json", EMPTY)],
        [
            ("write", "store/b/2026-02-01/manifest.json", EMPTY),
            ("write", "store/b/2026-02-01/changes.json", EMPTY),
        ],
        [("write", "store/hubs.json", '{"hubs": 1}\n')],
    ],
    ids=["a new version", "a new dataset with a change log", "the hub record"],
)
def test_a_pull_request_of_new_store_data_alone_skips_the_code_checks(
    repo: Path, ops: list[Op]
) -> None:
    assert _scope(repo, ops) == "false"


@pytest.mark.parametrize(
    "ops",
    [
        [("write", "store/a/2026-01-01/manifest.json", '{"edited": 1}\n')],
        [("rm", "store/a/2026-01-01/manifest.json", "")],
        [
            ("write", "store/a/2026-02-01/manifest.json", EMPTY),
            ("write", "pipeline/x.py", "x = 2\n"),
        ],
        [("mv", "pipeline/x.py", "store/c/2026-02-01/manifest.json")],
        [("write", "store/a/2026-02-01/notes.md", "notes\n")],
        [("write", ".github/workflows/ci.yml", "name: CI\n")],
    ],
    ids=[
        "an edited version",
        "a deleted version",
        "code beside data",
        "code moved into the store",
        "another file in a version",
        "a workflow",
    ],
)
def test_any_other_change_runs_the_code_checks(repo: Path, ops: list[Op]) -> None:
    assert _scope(repo, ops) == "true"


def test_a_push_runs_the_code_checks(repo: Path) -> None:
    assert (
        _scope(repo, [("write", "store/a/2026-02-01/manifest.json", EMPTY)], event="push") == "true"
    )


def test_a_commit_that_is_not_a_test_merge_runs_the_code_checks(repo: Path) -> None:
    assert (
        _scope(repo, [("write", "store/a/2026-02-01/manifest.json", EMPTY)], merge=False) == "true"
    )


def _needs(job: dict[str, object]) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return [str(n) for n in needs] if isinstance(needs, list) else []


def test_only_the_code_checks_wait_on_the_scope_and_a_failed_scope_skips_none() -> None:
    gated: dict[str, set[str]] = {}
    for f in sorted(WORKFLOWS.glob("*.yml")):
        jobs = yaml.safe_load(f.read_text(encoding="utf-8"))["jobs"]
        for name, job in jobs.items():
            if "scope" not in _needs(job):
                continue
            gated.setdefault(f.name, set()).add(name)
            assert str(job.get("name", "")) not in DATA_CHECKS, f"{f.name}: {name}"
            # needs alone would skip the job when the scope fails, which reports success.
            assert "!cancelled()" in str(job.get("if", "")), f"{f.name}: {name}"
            steps = [str(s.get("if", "")) for s in job["steps"]]
            assert SKIPS in str(job.get("if", "")) or all(SKIPS in s for s in steps), (
                f"{f.name}: {name}"
            )
    assert gated == GATED
