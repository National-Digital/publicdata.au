import re

import yaml

from .conftest import ROOT

GUARD = "github.repository == 'National-Digital/publicdata.au'"
# Checks that only read the tree, which a fork or a private copy may run.
CHECKS = {
    "ci.yml": "*",
    "clients.yml": {"python", "r"},
    "codeql.yml": "*",
    "cost.yml": "*",
    "dco.yml": "*",
    "dependency-review.yml": "*",
    "pr-title.yml": "*",
    "secrets.yml": "*",
}


def _guarded(jobs: dict, name: str) -> bool:
    """The job's own condition names the repo, or a job it needs does."""
    job = jobs[name]
    if GUARD in str(job.get("if", "")):
        return True
    needs = job.get("needs", [])
    return any(_guarded(jobs, n) for n in ([needs] if isinstance(needs, str) else needs))


def test_every_job_with_side_effects_runs_only_in_the_public_repo():
    unguarded = []
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(f.read_text(encoding="utf-8"))["jobs"]
        allowed = CHECKS.get(f.name, set())
        for name in jobs:
            if allowed != "*" and name not in allowed and not _guarded(jobs, name):
                unguarded.append(f"{f.name}: {name}")
    assert unguarded == []


def test_every_fixture_dataset_is_in_the_third_party_notices():
    notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    store = ROOT / "pipeline" / "tests" / "fixtures" / "store"
    missing = [
        d.name for d in sorted(store.iterdir()) if d.is_dir() and f"`{d.name}`" not in notices
    ]
    assert missing == []


def test_a_job_that_pushes_as_the_app_keeps_no_checkout_credential():
    """Checkout keeps its token in an included config, so git would send two Authorization headers."""
    wrong = []
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for name, job in yaml.safe_load(f.read_text(encoding="utf-8"))["jobs"].items():
            steps = job.get("steps", [])
            if not any("create-github-app-token" in str(s.get("uses", "")) for s in steps):
                continue
            for s in steps:
                if "actions/checkout" in str(s.get("uses", "")):
                    if (s.get("with") or {}).get("persist-credentials") is not False:
                        wrong.append(f"{f.name}: {name}")
    assert wrong == []


def test_every_pull_request_check_reports_on_each_new_head():
    # A required check that skips a push leaves the PR waiting on a result that never comes.
    skipped = []
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        on = yaml.safe_load(f.read_text(encoding="utf-8"))[True]
        pr = on.get("pull_request") if isinstance(on, dict) else None
        if isinstance(pr, dict) and "types" in pr and "synchronize" not in pr["types"]:
            skipped.append(f.name)
    assert skipped == []


def test_the_readme_links_every_document():
    """AGENTS.md sends agents to the README and every document it links, so none may go unlinked."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    linked = {m.split("#")[0] for m in re.findall(r"\]\(([^)\s]+)\)", readme)}
    docs = [*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")]
    names = sorted(str(p.relative_to(ROOT)) for p in docs if p.name != "README.md")
    assert [n for n in names if n not in linked] == []
