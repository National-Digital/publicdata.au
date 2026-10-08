import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .conftest import ROOT, SLOW_TESTS

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# Every test here needs git; a missing one fails at its first call.
GIT = shutil.which("git") or "git"
RUFF = shutil.which("ruff")
NODE = shutil.which("node")
VENV_BIN = Path(sys.executable).parent


def _run(
    args: Sequence[str | Path], cwd: Path, path: Iterable[Path], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(PATH=os.pathsep.join(map(str, path)), HOME=str(cwd), GIT_CONFIG_NOSYSTEM="1")
    env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "1"
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=check)


def _bin(tmp_path: Path, **tools: str | Path | None) -> Path:
    """A directory holding git and the named tools, so a test decides what the hooks can find."""
    d = Path(tempfile.mkdtemp(prefix="bin", dir=tmp_path))
    for name, target in {"git": GIT, **tools}.items():
        if target is None:
            pytest.fail(f"{name} is not installed")
        (d / name).symlink_to(target)
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "pipeline").mkdir(parents=True)
    shutil.copytree(ROOT / ".githooks", r / ".githooks")
    shutil.copy(ROOT / "pipeline" / "pyproject.toml", r / "pipeline" / "pyproject.toml")
    (r / "clients" / "python").mkdir(parents=True)
    shutil.copy(ROOT / "clients" / "python" / "pyproject.toml", r / "clients" / "python")
    # Each tree is a package, as in the repository, so a sample file meets INP001 and D104.
    (r / "pipeline" / "publicdata").mkdir()
    (r / "pipeline" / "publicdata" / "__init__.py").write_text('"""Pipeline."""\n')
    client = r / "clients" / "python" / "src" / "publicdata_au"
    client.mkdir(parents=True)
    (client / "__init__.py").write_text('"""Client."""\n')
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


def _commit(
    repo: Path, files: dict[str, str], path: Iterable[Path]
) -> subprocess.CompletedProcess[str]:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    return _run([GIT, "commit", "-m", "test: change"], repo, path)


needs_ruff = pytest.mark.skipif(RUFF is None, reason="ruff is not installed")


@needs_ruff
def test_pre_commit_stops_a_lint_error_and_names_the_file_and_rule(
    repo: Path, tmp_path: Path
) -> None:
    out = _commit(repo, {"pipeline/publicdata/bad.py": "import os\n"}, [_bin(tmp_path, ruff=RUFF)])
    assert out.returncode != 0
    assert "publicdata/bad.py" in out.stdout + out.stderr
    assert "F401" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_stops_a_file_ruff_would_reformat(repo: Path, tmp_path: Path) -> None:
    out = _commit(
        repo, {"pipeline/publicdata/ugly.py": "x = {'a':1}\n"}, [_bin(tmp_path, ruff=RUFF)]
    )
    assert out.returncode != 0
    assert "pipeline/publicdata/ugly.py: not formatted" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_stops_a_lint_error_in_the_python_client(repo: Path, tmp_path: Path) -> None:
    out = _commit(repo, {"clients/python/src/bad.py": "import os\n"}, [_bin(tmp_path, ruff=RUFF)])
    assert out.returncode != 0
    assert "src/bad.py" in out.stdout + out.stderr
    assert "F401" in out.stdout + out.stderr


@needs_ruff
def test_each_python_tree_is_checked_with_its_own_settings(repo: Path, tmp_path: Path) -> None:
    # UP017 needs Python 3.11, which the pipeline targets and the client does not.
    code = '"""UTC."""\n\nimport datetime\n\nX = datetime.timezone.utc\n'
    path = [_bin(tmp_path, ruff=RUFF)]
    out = _commit(repo, {"clients/python/src/publicdata_au/utc.py": code}, path)
    assert out.returncode == 0, out.stdout + out.stderr
    out = _commit(repo, {"pipeline/publicdata/utc.py": code}, path)
    assert out.returncode != 0
    assert "UP017" in out.stdout + out.stderr
    # ruff's defaults leave UP out, so this fails only under the client's own settings.
    _run([GIT, "reset", "-q", "--hard"], repo, path, check=True)
    old = '"""Old."""\n\nfrom typing import Optional\n\nX: Optional[int] = None\n'
    out = _commit(repo, {"clients/python/src/publicdata_au/old.py": old}, path)
    assert out.returncode != 0
    assert "UP0" in out.stdout + out.stderr


@needs_ruff
def test_pre_commit_checks_the_staged_content(repo: Path, tmp_path: Path) -> None:
    path = [_bin(tmp_path, ruff=RUFF)]
    f = repo / "pipeline" / "publicdata" / "bad.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("import os\n", encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    f.write_text("", encoding="utf-8")
    assert _run([GIT, "commit", "-m", "test: change"], repo, path).returncode != 0


@needs_ruff
def test_a_clean_commit_passes_with_one_sign_off(repo: Path, tmp_path: Path) -> None:
    path = [_bin(tmp_path, ruff=RUFF)]
    out = _commit(repo, {"pipeline/publicdata/good.py": '"""Good."""\n\nX = 1\n'}, path)
    assert out.returncode == 0, out.stdout + out.stderr
    _run([GIT, "commit", "-q", "-s", "--amend", "--no-edit"], repo, path, check=True)
    body = _run([GIT, "log", "-1", "--format=%B"], repo, path, check=True).stdout
    assert body.count("Signed-off-by: Test <test@example.org>") == 1


def test_pre_commit_runs_no_python_check_without_staged_python(repo: Path, tmp_path: Path) -> None:
    fake = tmp_path / "fake-ruff"
    fake.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    files = {"register/x.yaml": "slug: x\n", "docs/x.md": "# X\n", "pipeline/x.py.txt": "x\n"}
    out = _commit(repo, files, [_bin(tmp_path, ruff=fake)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert not (tmp_path / "ran").exists()


def test_pre_commit_skips_when_ruff_is_missing(repo: Path, tmp_path: Path) -> None:
    out = _commit(repo, {"pipeline/publicdata/bad.py": "import os\n"}, [_bin(tmp_path)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "ruff not found, lint skipped" in out.stdout + out.stderr


def _push(
    repo: Path, tmp_path: Path, files: dict[str, str], path: Iterable[Path]
) -> subprocess.CompletedProcess[str]:
    remote = tmp_path / "remote.git"
    _run([GIT, "init", "-q", "--bare", str(remote)], tmp_path, path, check=True)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _run([GIT, "add", "."], repo, path, check=True)
    _run([GIT, "commit", "-q", "--no-verify", "-m", "test: change"], repo, path, check=True)
    return _run([GIT, "push", str(remote), "HEAD:main"], repo, path)


SLOW_FAILURE = "import pytest\n\n\n@pytest.mark.slow\ndef test_slow():\n    assert False\n"


def test_pre_push_stops_a_failing_fast_test_and_names_it(repo: Path, tmp_path: Path) -> None:
    files = {"pipeline/tests/test_x.py": "def test_breaks():\n    assert False\n"}
    out = _push(repo, tmp_path, files, [_bin(tmp_path), VENV_BIN])
    assert out.returncode != 0
    assert "test_breaks" in out.stdout + out.stderr


def test_pre_push_passes_when_the_fast_tests_pass_and_leaves_slow_ones_out(
    repo: Path, tmp_path: Path
) -> None:
    files = {
        "pipeline/tests/test_x.py": "def test_holds():\n    assert True\n",
        "pipeline/tests/test_y.py": SLOW_FAILURE,
    }
    out = _push(repo, tmp_path, files, [_bin(tmp_path), VENV_BIN])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "node not found, JavaScript tests skipped" in out.stdout + out.stderr


def test_pre_push_from_a_worktree_hides_the_repository_from_the_tests(
    repo: Path, tmp_path: Path
) -> None:
    path = [_bin(tmp_path), VENV_BIN]
    tree = tmp_path / "tree"
    _run([GIT, "worktree", "add", "-q", "-b", "side", str(tree)], repo, path, check=True)
    files = {"pipeline/tests/test_x.py": "import os\n\n\ndef test_env():\n    assert 'GIT_DIR' not in os.environ\n"}  # fmt: skip
    out = _push(tree, tmp_path, files, path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_pre_push_skips_when_pytest_is_missing(repo: Path, tmp_path: Path) -> None:
    files = {"pipeline/tests/test_x.py": "def test_breaks():\n    assert False\n"}
    out = _push(repo, tmp_path, files, [_bin(tmp_path)])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Python tests skipped" in out.stdout + out.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_pre_push_stops_a_failing_javascript_test(repo: Path, tmp_path: Path) -> None:
    files = {"functions/x.test.mjs": "import { test } from 'node:test';\ntest('x', () => { throw new Error('no'); });\n"}  # fmt: skip
    out = _push(repo, tmp_path, files, [_bin(tmp_path, node=NODE)])
    assert out.returncode != 0
    assert "tests failed" in out.stdout + out.stderr


def test_every_test_named_slow_exists() -> None:
    tests = Path(__file__).parent
    missing = [
        n
        for n in sorted(SLOW_TESTS)
        if f"def {n.split('::')[1]}(" not in (tests / n.split("::")[0]).read_text(encoding="utf-8")
    ]
    assert missing == []
