"""Implement's verdicts in the implement-ticket workflow (orchestrator.md#implement,
#implement-ticket): the bundle is checked, then pushed to a new branch and opened
as a draft PR, or nothing is pushed. GitHub's REST API is in-memory
(fake_github.py); its git side is a real bare repo (git_repos.py)."""

import json
from dataclasses import replace

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub
from git_repos import Remote

from factory import db, enrolment, loop, orchestrator
from factory.github import AppAuth, GitHub
from factory.runner import FakeRunner, RunFiles
from test_explore_verdicts import explore_result

REPO = "octo/widgets"
CONFIG = "setup: uv sync\ncheck: uv run pytest\n"


@pytest.fixture
def remote(tmp_path):
    return Remote(tmp_path / "github")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake(remote):
    fake = FakeGitHub()
    fake.add_repo(REPO, head_sha=remote.base).files[".factory/config.yml"] = CONFIG
    fake.repos[REPO].permissions["alice"] = "write"
    return fake


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


@pytest.fixture
def github(fake, remote):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake,
                  git_url=lambda repo: remote.url)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def settings(tmp_path):
    return orchestrator.Settings(git_dir=tmp_path / "factory-git")


def enrol(conn, github, repo, config=CONFIG):
    repo.files[".factory/config.yml"] = config
    enrolment.refresh(conn, github, factory_repo="octo/factory")


@pytest.fixture(autouse=True)
def enrolled(conn, github, repo):
    enrol(conn, github, repo)


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def ticket(repo, number=7):
    issue = repo.add_issue(number, "Search ignores accents", "Body", author="alice")
    issue.label("agent:explore", by="alice")
    return issue


def implement_dispatched(conn, github, runner, settings):
    """Explore says easy-call; implement is queued and dispatched. Returns its row."""
    orchestrator.run_once(conn, github, runner, settings)
    [explore] = runs(conn)
    runner.finish(explore["runner_run_id"], RunFiles(json.dumps(
        explore_result(explore["id"], verdict="easy-call")).encode()))
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)
    [implement] = [r for r in runs(conn) if r["task"] == "implement"]
    assert implement["status"] == "dispatched"
    return implement


def finish_implement(conn, github, runner, settings, row, bundle, **output):
    result = {
        "contract_version": 1, "run_id": row["id"], "task": "implement", "outcome": "succeeded",
        "check": {"passed": True, "output_tail": "ok\n"},
        "output": {"verdict": "built", **output},
    }
    runner.finish(row["runner_run_id"], RunFiles(json.dumps(result).encode(), bundle))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


def commits(remote, row, files=None, message="Fold accents in search"):
    """The agent's commits on the run's branch, bundled like the run script does."""
    branch = json.loads(row["dispatch"])["branch"]
    remote.branch(branch)
    remote.commit(files or {"src/search.py": "fold()\n"}, message)
    return remote.bundle(branch)


def pulls(repo):
    return list(repo.pulls.values())


# --- built ------------------------------------------------------------------------


def test_built_pushes_the_branch_and_opens_a_draft_pr_for_the_ticket(
    conn, github, repo, remote, runner, settings
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    bundle = commits(remote, row)
    tip = remote.head()

    run = finish_implement(conn, github, runner, settings, row, bundle,
                           pr_body="Search now folds accents.")

    assert run["outcome"] == "succeeded"
    assert remote.remote_branch(f"factory/7-{row['id']}") == tip
    [pr] = pulls(repo)
    assert (pr["title"], pr["head"], pr["base"], pr["draft"]) == (
        "Search ignores accents", f"factory/7-{row['id']}", "main", True)
    assert pr["body"].startswith("Search now folds accents.\n")
    assert "Closes #7" in pr["body"]
    assert f"Factory run {row['id']}" in pr["body"]
    assert runner.log_link(REPO, row["runner_run_id"]) in pr["body"]
    assert "draft" in pr["body"].split("Closes #7")[1]
    assert "⚠️" not in pr["body"]
    [workflow] = conn.execute("SELECT * FROM workflows").fetchall()
    assert workflow["pr"] == pr["number"]
    assert "agent:in-progress" in issue.labels


def test_without_a_pr_body_the_pr_body_is_the_commit_log(conn, github, repo, remote, runner, settings):
    ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    bundle = commits(remote, row, message="Fold accents in search")

    finish_implement(conn, github, runner, settings, row, bundle)

    [pr] = pulls(repo)
    sha = remote.head()[:7]
    assert f"{sha} Fold accents in search" in pr["body"]


def test_the_branch_is_pushed_without_force_and_an_existing_pr_is_reused_on_a_retry(
    conn, github, repo, remote, runner, settings
):
    """A crash after opening the PR but before marking the run handled."""
    ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    finish_implement(conn, github, runner, settings, row, commits(remote, row))
    conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (row["id"],))
    conn.execute("UPDATE workflows SET pr = NULL")

    orchestrator.run_once(conn, github, runner, settings)

    [pr] = pulls(repo)
    assert conn.execute("SELECT pr FROM workflows").fetchone()["pr"] == pr["number"]


@pytest.mark.parametrize(("config", "files", "flagged"), [
    (CONFIG + "test_paths: ['tests/**']\n", {"src/search.py": "x\n"}, True),
    (CONFIG + "test_paths: ['tests/**']\n", {"tests/unit/test_search.py": "x\n"}, False),
    (CONFIG + "test_paths: ['**/*.test.ts']\n", {"web/search.test.ts": "x\n"}, False),
    (CONFIG, {"src/search.py": "x\n"}, False),
])
def test_the_footer_flags_a_pr_that_changes_no_test_files(
    conn, github, repo, remote, runner, settings, config, files, flagged
):
    enrol(conn, github, repo, config)
    ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)

    finish_implement(conn, github, runner, settings, row, commits(remote, row, files))

    [pr] = pulls(repo)
    assert ("⚠️ no test files changed" in pr["body"]) is flagged


def test_the_footer_flags_a_ticket_edited_after_the_run_started(
    conn, github, repo, remote, runner, settings
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    issue.body = "Body, edited: also handle umlauts"

    finish_implement(conn, github, runner, settings, row, commits(remote, row))

    [pr] = pulls(repo)
    assert "the ticket changed after this run started" in pr["body"]


@pytest.mark.parametrize(("author", "flagged"), [("alice", True), ("mallory", False)])
def test_the_footer_flags_a_comment_from_a_writer_after_the_run_started(
    conn, github, repo, remote, runner, settings, author, flagged
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    issue.add_comment(author, "Oh, and umlauts too.", created_at="2099-01-01T00:00:00Z")

    finish_implement(conn, github, runner, settings, row, commits(remote, row))

    [pr] = pulls(repo)
    assert ("the ticket changed after this run started" in pr["body"]) is flagged


def test_implement_starts_from_the_default_branch_head_at_dispatch_time(
    conn, github, repo, remote, runner, settings
):
    ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    [explore] = runs(conn)
    runner.finish(explore["runner_run_id"], RunFiles(json.dumps(
        explore_result(explore["id"], verdict="easy-call")).encode()))
    orchestrator.run_once(conn, github, runner, settings)  # implement queued, base = old head
    repo.head_sha = moved = "d" * 40

    orchestrator.run_once(conn, github, runner, settings)

    [implement] = [r for r in runs(conn) if r["task"] == "implement"]
    assert implement["status"] == "dispatched"
    assert runner.dispatched[-1][1]["base_sha"] == moved
    assert json.loads(implement["dispatch"])["base_sha"] == moved


# --- needs-a-human ------------------------------------------------------------------


def test_needs_a_human_pushes_nothing_posts_the_comment_and_labels_the_ticket(
    conn, github, repo, remote, runner, settings
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    bundle = commits(remote, row)

    run = finish_implement(conn, github, runner, settings, row, bundle, verdict="needs-a-human",
                           comment="The ticket contradicts the README on sort order.")

    assert run["outcome"] == "succeeded"
    assert remote.remote_branch(f"factory/7-{row['id']}") is None
    assert pulls(repo) == []
    comment = issue.posts[-1]["body"]
    assert "The ticket contradicts the README on sort order." in comment
    assert f"Factory run {row['id']}" in comment
    assert issue.labels == ["needs-a-human"]
    assert [w["status"] for w in conn.execute("SELECT * FROM workflows")] == ["stopped"]
    assert [r["task"] for r in runs(conn)] == ["explore", "implement"]


# --- invalid results ------------------------------------------------------------------


def test_a_bundle_touching_a_protected_path_is_invalid_and_nothing_is_written(
    conn, github, repo, remote, runner, settings
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    comments = len(issue.posts)
    bundle = commits(remote, row, {".github/workflows/ci.yml": "on: push\n"})

    run = finish_implement(conn, github, runner, settings, row, bundle)

    assert run["outcome"] == "invalid"
    assert remote.remote_branch(f"factory/7-{row['id']}") is None
    assert pulls(repo) == []
    assert len(issue.posts) == comments


def test_built_without_a_bundle_is_invalid(conn, github, repo, runner, settings):
    ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)

    run = finish_implement(conn, github, runner, settings, row, None)

    assert run["outcome"] == "invalid"
    assert pulls(repo) == []


@pytest.mark.parametrize("verdict", ["built", "needs-a-human"])
def test_a_token_in_the_agents_prose_is_invalid_and_nothing_is_posted(
    conn, github, repo, remote, runner, settings, verdict
):
    issue = ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    comments = len(issue.posts)
    prose = {"pr_body": "Done. sk-ant-oat01-abcdef"} if verdict == "built" else {
        "comment": "Here: sk-ant-oat01-abcdef"}

    run = finish_implement(conn, github, runner, settings, row, commits(remote, row),
                           verdict=verdict, **prose)

    assert run["outcome"] == "invalid"
    assert pulls(repo) == []
    assert remote.remote_branch(f"factory/7-{row['id']}") is None
    assert len(issue.posts) == comments
    assert "needs-a-human" not in issue.labels


def test_git_trouble_while_checking_leaves_the_run_to_be_collected_again(
    conn, github, repo, remote, runner, settings, tmp_path
):
    ticket(repo)
    row = implement_dispatched(conn, github, runner, settings)
    bundle = commits(remote, row)
    broken = replace(settings, git_dir=tmp_path / "file-not-dir")
    broken.git_dir.write_text("in the way")

    finish_implement(conn, github, runner, broken, row, bundle)
    assert loop.get_run(conn, row["id"])["status"] == "dispatched"

    orchestrator.run_once(conn, github, runner, settings)
    assert loop.get_run(conn, row["id"])["outcome"] == "succeeded"
    assert len(pulls(repo)) == 1

