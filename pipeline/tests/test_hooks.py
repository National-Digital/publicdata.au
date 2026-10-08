import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from .conftest import ROOT, SLOW_TESTS

GIT = shutil.which("git")
RUFF = shutil.which("ruff")
NODE = shutil.which("node")
VENV_BIN = Path(sys.executable).parent


def _env(cwd, path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(PATH=os.pathsep.join(map(str, path)), HOME=str(cwd), GIT_CONFIG_NOSYSTEM="1")
    env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "1"
    return env


def _run(args, cwd, path, check=False):
    env = _env(cwd, path)
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=check)


def _bin(tmp_path, **tools) -> Path:
    """A directory holding git and the named tools, so a test decides what the hooks can find."""
    d = Path(tempfile.mkdtemp(prefix="bin", dir=tmp_path))
    for name, target in {"git": GIT, **tools}.items():
        (d / name).symlink_to(target)
    return d


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / "pipeline").mkdir(parents=True)
    shutil.copytree(ROOT / ".githooks", r / ".githooks")
    shutil.copy(ROOT / "pipeline" / "pyproject.toml", r / "pipeline" / "pyproject.toml")
    (r / "clients" / "python").mkdir(parents=True)
    shutil.copy(ROOT / "clients" / "python" / "pyproject.toml", r / "clients" / "python")
    path = [_bin(tmp_path)]
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.org"],
        ["config", "core.hooksPath", ".githooks"],
        ["add", "."],
        ["commit", "-q", "--no-verify", "-m", "chore: init"],
    ):
        _run([GIT, *args], r, path, check=True)
    return r


def _commit(repo, files: dict[str, str], path):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    return _run([GIT, "commit", "-m", "test: change"], repo, path)


needs_ruff = pytest.mark.skipif(RUFF is None, reason="ruff is not installed")


@needs_ruff
def test_pre_commit_stops_a_lint_error_and_names_the_file_and_rule(repo, tmp_path):
    out = _commit(repo, {"pipeline/publicdata/bad.py": "import os\n"}, [_bin(tmp_path, ruff=RUFF)])
    assert out.returncode != 0
    assert "publicdata/bad.py" in out.stdout + out.stderr
    assert "F401" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_stops_a_file_ruff_would_reformat(repo, tmp_path):
    out = _commit(
        repo, {"pipeline/publicdata/ugly.py": "x = {'a':1}\n"}, [_bin(tmp_path, ruff=RUFF)]
    )
    assert out.returncode != 0
    assert "pipeline/publicdata/ugly.py: not formatted" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_stops_a_lint_error_in_the_python_client(repo, tmp_path):
    out = _commit(repo, {"clients/python/src/bad.py": "import os\n"}, [_bin(tmp_path, ruff=RUFF)])
    assert out.returncode != 0
    assert "src/bad.py" in out.stdout + out.stderr
    assert "F401" in out.stdout + out.stderr


@needs_ruff
def test_each_python_tree_is_checked_with_its_own_settings(repo, tmp_path):
    # UP017 needs Python 3.11, which the pipeline targets and the client does not.
    code = "import datetime\n\nX = datetime.timezone.utc\n"
    path = [_bin(tmp_path, ruff=RUFF)]
    assert _commit(repo, {"clients/python/src/utc.py": code}, path).returncode == 0
    out = _commit(repo, {"pipeline/publicdata/utc.py": code}, path)
    assert out.returncode != 0
    assert "UP017" in out.stdout + out.stderr
    # ruff's defaults leave UP out, so this fails only under the client's own settings.
    _run([GIT, "reset", "-q", "--hard"], repo, path, check=True)
    old = "from typing import Optional\n\nX: Optional[int] = None\n"
    out = _commit(repo, {"clients/python/src/old.py": old}, path)
    assert out.returncode != 0
    assert "UP0" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_checks_the_staged_content(repo, tmp_path):
    path = [_bin(tmp_path, ruff=RUFF)]
    f = repo / "pipeline" / "publicdata" / "bad.py"
    f.parent.mkdir(parents=True)
    f.write_text("import os\n", encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    f.write_text("", encoding="utf-8")
    assert _run([GIT, "commit", "-m", "test: change"], repo, path).returncode != 0


@needs_ruff
def test_a_clean_commit_passes_with_one_sign_off(repo, tmp_path):
    path = [_bin(tmp_path, ruff=RUFF)]
    out = _commit(repo, {"pipeline/publicdata/good.py": "X = 1\n"}, path)
    assert out.returncode == 0, out.stdout + out.stderr
    _run([GIT, "commit", "-q", "-s", "--amend", "--no-edit"], repo, path, check=True)
    body = _run([GIT, "log", "-1", "--format=%B"], repo, path, check=True).stdout
    assert body.count("Signed-off-by: Test <test@example.org>") == 1


def test_pre_commit_runs_no_python_check_without_staged_python(repo, tmp_path):
    fake = tmp_path / "fake-ruff"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    files = {"register/x.yaml": "slug: x\n", "docs/x.md": "# X\n", "pipeline/x.py.txt": "x\n"}
    out = _commit(repo, files, [_bin(tmp_path, ruff=fake)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (tmp_path / "ran").exists()


def test_pre_commit_skips_when_ruff_is_missing(repo, tmp_path):
    out = _commit(repo, {"pipeline/publicdata/bad.py": "import os\n"}, [_bin(tmp_path)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ruff not found, lint skipped" in out.stdout + out.stderr


WORKFLOW_TOOLS = {t: shutil.which(t) for t in ("actionlint", "shellcheck", "zizmor")}
needs_workflow_tools = pytest.mark.skipif(
    None in WORKFLOW_TOOLS.values(), reason="actionlint, shellcheck or zizmor is not installed"
)
CI_PINS = dict(
    re.findall(
        r"^ +([A-Z]+)_VERSION: (\S+)$", (ROOT / ".github/workflows/ci.yml").read_text(), re.M
    )
)
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
WORKFLOW = """name: Check
on: pull_request
permissions:
  contents: read
{concurrency}jobs:
  check:
    name: Check
    runs-on: ubuntu-24.04
    steps:
      - uses: {uses}
        with:
          persist-credentials: false
      - run: {run}
"""


CONCURRENCY = "concurrency:\n  group: check-${{ github.ref }}\n  cancel-in-progress: true\n"


def _workflow(uses=CHECKOUT, run="echo ok", concurrency=CONCURRENCY) -> dict[str, str]:
    text = WORKFLOW.format(uses=uses, run=run, concurrency=concurrency)
    return {".github/workflows/check.yml": text}


@pytest.fixture
def wf_repo(repo) -> Path:
    """The fixture repository with CI's workflow and actionlint config committed."""
    (repo / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(ROOT / ".github" / "workflows" / "ci.yml", repo / ".github" / "workflows")
    shutil.copy(ROOT / ".github" / "actionlint.yaml", repo / ".github")
    _run([GIT, "add", "."], repo, [], check=True)
    _run([GIT, "commit", "-q", "--no-verify", "-m", "ci: workflows"], repo, [], check=True)
    return repo


def _wf_bin(tmp_path, **tools) -> Path:
    """_bin plus the utilities the workflow check calls, which a developer's PATH always has."""
    utils = {t: shutil.which(t) for t in ("sed", "grep", "head", "mktemp", "mkdir", "rm")}
    return _bin(tmp_path, **utils, **tools)


def _fake(tmp_path, name, text) -> Path:
    f = tmp_path / f"fake-{name}"
    f.write_text(f"#!/bin/sh\n{text}\n", encoding="utf-8")
    f.chmod(0o755)
    return f


@needs_workflow_tools
def test_pre_commit_passes_a_clean_workflow(wf_repo, tmp_path):
    out = _commit(wf_repo, _workflow(), [_wf_bin(tmp_path, **WORKFLOW_TOOLS)])
    assert out.returncode == 0, out.stdout + out.stderr


@needs_workflow_tools
def test_pre_commit_stops_an_unpinned_action_and_names_the_file_and_rule(wf_repo, tmp_path):
    out = _commit(
        wf_repo, _workflow(uses="actions/checkout@v7"), [_wf_bin(tmp_path, **WORKFLOW_TOOLS)]
    )
    assert out.returncode != 0
    assert ".github/workflows/check.yml" in out.stdout + out.stderr
    assert "unpinned-uses" in out.stdout + out.stderr


@needs_workflow_tools
def test_pre_commit_stops_a_template_injection_in_a_run_block(wf_repo, tmp_path):
    run = 'echo "${{ github.event.pull_request.title }}"'
    out = _commit(wf_repo, _workflow(run=run), [_wf_bin(tmp_path, **WORKFLOW_TOOLS)])
    assert out.returncode != 0
    assert "template-injection" in out.stdout + out.stderr
    assert '"github.event.pull_request.title" is potentially untrusted' in out.stdout + out.stderr


@needs_workflow_tools
def test_pre_commit_checks_the_staged_workflow(wf_repo, tmp_path):
    path = [_wf_bin(tmp_path, **WORKFLOW_TOOLS)]
    ((name, bad),) = _workflow(uses="actions/checkout@v7").items()
    (wf_repo / name).write_text(bad, encoding="utf-8")
    _run([GIT, "add", "."], wf_repo, path, check=True)
    (wf_repo / name).write_text(_workflow()[name], encoding="utf-8")
    out = _run([GIT, "commit", "-m", "test: change"], wf_repo, path)
    assert out.returncode != 0
    assert "unpinned-uses" in out.stdout + out.stderr


@needs_workflow_tools
def test_pre_commit_runs_zizmor_at_the_auditor_persona(wf_repo, tmp_path):
    # concurrency-limits is reported only at the pedantic persona and above.
    out = _commit(wf_repo, _workflow(concurrency=""), [_wf_bin(tmp_path, **WORKFLOW_TOOLS)])
    assert out.returncode != 0
    assert "concurrency-limits" in out.stdout + out.stderr


@needs_workflow_tools
def test_pre_commit_stops_a_workflow_zizmor_cannot_parse(wf_repo, tmp_path):
    bad = "name: Bad\non: pull_request\njobs:\n  x:\n    runs-on: ubuntu-24.04\n    steps: nope\n"
    files = {**_workflow(), ".github/workflows/bad.yml": bad}
    out = _commit(wf_repo, files, [_wf_bin(tmp_path, **WORKFLOW_TOOLS)])
    assert out.returncode != 0
    assert "failed to load file://.github/workflows/bad.yml" in out.stdout + out.stderr


@needs_workflow_tools
@needs_ruff
def test_a_workflow_failure_still_lets_the_python_check_report(wf_repo, tmp_path):
    files = {**_workflow(uses="actions/checkout@v7"), "pipeline/publicdata/bad.py": "import os\n"}
    out = _commit(wf_repo, files, [_wf_bin(tmp_path, ruff=RUFF, **WORKFLOW_TOOLS)])
    assert out.returncode != 0
    assert "unpinned-uses" in out.stdout + out.stderr
    assert "F401" in out.stdout + out.stderr


def test_pre_commit_runs_no_workflow_check_without_staged_workflows(wf_repo, tmp_path):
    fake = _fake(tmp_path, "lint", f"{shutil.which('touch')} {tmp_path / 'ran'}\nexit 1")
    files = {"register/x.yaml": "slug: x\n", "docs/github.md": "# X\n", "github/x.yml": "x: 1\n"}
    out = _commit(
        wf_repo, files, [_wf_bin(tmp_path, actionlint=fake, shellcheck=fake, zizmor=fake)]
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (tmp_path / "ran").exists()


def test_pre_commit_runs_no_workflow_check_for_templates_or_the_lint_fixture(wf_repo, tmp_path):
    files = {
        ".github/PULL_REQUEST_TEMPLATE.md": "## Why\n",
        ".github/CODEOWNERS": "* @x\n",
        ".github/ISSUE_TEMPLATE/bug.md": "# Bug\n",
        ".github/lint-fixtures/defects.yml": "on: push\n",
    }
    out = _commit(wf_repo, files, [_wf_bin(tmp_path)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "not found" not in out.stdout + out.stderr


@pytest.mark.parametrize("missing", ["actionlint", "shellcheck", "zizmor"])
def test_pre_commit_fails_when_a_workflow_tool_is_missing(wf_repo, tmp_path, missing):
    fake = _fake(tmp_path, "lint", "exit 0")
    tools = {t: fake for t in WORKFLOW_TOOLS if t != missing}
    out = _commit(wf_repo, _workflow(), [_wf_bin(tmp_path, **tools)])
    assert out.returncode != 0
    pin = CI_PINS[missing.upper()]
    assert f"{missing} not found; install {missing} {pin}" in out.stdout + out.stderr


def test_pre_commit_removes_its_copy_when_interrupted(wf_repo, tmp_path):
    seen = tmp_path / "tree"
    zizmor = _fake(
        tmp_path,
        "zizmor",
        f'[ "$1" = --version ] && exit 0\npwd > {seen}\nexec {shutil.which("sleep")} 30',
    )
    fake = _fake(tmp_path, "lint", "exit 0")
    path = [_wf_bin(tmp_path, actionlint=fake, shellcheck=fake, zizmor=zizmor)]
    ((name, text),) = _workflow().items()
    (wf_repo / name).write_text(text, encoding="utf-8")
    _run([GIT, "add", "."], wf_repo, path, check=True)
    hook = subprocess.Popen(
        [shutil.which("sh"), ".githooks/pre-commit"],
        cwd=wf_repo,
        env=_env(wf_repo, path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(200):
            if seen.exists() and seen.read_text().strip():
                break
            time.sleep(0.05)
        os.killpg(hook.pid, signal.SIGTERM)
        assert hook.wait(timeout=10) != 0
    finally:
        if hook.poll() is None:
            hook.kill()
    tree = Path(seen.read_text().strip())
    assert tree.name and not tree.exists()


def test_pre_commit_warns_when_a_tool_is_not_the_version_ci_pins(wf_repo, tmp_path):
    fake = _fake(tmp_path, "lint", "echo 0.0.1")
    out = _commit(
        wf_repo, _workflow(), [_wf_bin(tmp_path, actionlint=fake, shellcheck=fake, zizmor=fake)]
    )
    assert out.returncode == 0, out.stdout + out.stderr
    pin = CI_PINS["ZIZMOR"]
    assert f"warning: zizmor is 0.0.1 here and {pin} in CI" in out.stdout + out.stderr


def _push(repo, tmp_path, files: dict[str, str], path):
    remote = tmp_path / "remote.git"
    _run([GIT, "init", "-q", "--bare", str(remote)], tmp_path, path, check=True)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    _run([GIT, "commit", "-q", "--no-verify", "-m", "test: change"], repo, path, check=True)
    return _run([GIT, "push", str(remote), "HEAD:main"], repo, path)


SLOW_FAILURE = "import pytest\n\n\n@pytest.mark.slow\ndef test_slow():\n    assert False\n"


def test_pre_push_stops_a_failing_fast_test_and_names_it(repo, tmp_path):
    files = {"pipeline/tests/test_x.py": "def test_breaks():\n    assert False\n"}
    out = _push(repo, tmp_path, files, [_bin(tmp_path), VENV_BIN])
    assert out.returncode != 0
    assert "test_breaks" in out.stdout + out.stderr


def test_pre_push_passes_when_the_fast_tests_pass_and_leaves_slow_ones_out(repo, tmp_path):
    files = {
        "pipeline/tests/test_x.py": "def test_holds():\n    assert True\n",
        "pipeline/tests/test_y.py": SLOW_FAILURE,
    }
    out = _push(repo, tmp_path, files, [_bin(tmp_path), VENV_BIN])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "node not found, JavaScript tests skipped" in out.stdout + out.stderr


def test_pre_push_from_a_worktree_hides_the_repository_from_the_tests(repo, tmp_path):
    path = [_bin(tmp_path), VENV_BIN]
    tree = tmp_path / "tree"
    _run([GIT, "worktree", "add", "-q", "-b", "side", str(tree)], repo, path, check=True)
    files = {"pipeline/tests/test_x.py": "import os\n\n\ndef test_env():\n    assert 'GIT_DIR' not in os.environ\n"}  # fmt: skip
    out = _push(tree, tmp_path, files, path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_pre_push_skips_when_pytest_is_missing(repo, tmp_path):
    files = {"pipeline/tests/test_x.py": "def test_breaks():\n    assert False\n"}
    out = _push(repo, tmp_path, files, [_bin(tmp_path)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Python tests skipped" in out.stdout + out.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_pre_push_stops_a_failing_javascript_test(repo, tmp_path):
    files = {"functions/x.test.mjs": "import { test } from 'node:test';\ntest('x', () => { throw new Error('no'); });\n"}  # fmt: skip
    out = _push(repo, tmp_path, files, [_bin(tmp_path, node=NODE)])
    assert out.returncode != 0
    assert "tests failed" in out.stdout + out.stderr


def test_every_test_named_slow_exists():
    tests = Path(__file__).parent
    missing = [
        n
        for n in sorted(SLOW_TESTS)
        if f"def {n.split('::')[1]}(" not in (tests / n.split("::")[0]).read_text(encoding="utf-8")
    ]
    assert missing == []
