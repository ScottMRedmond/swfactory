"""Explore verdicts and the implement-ticket workflow's state (orchestrator.md#explore,
#workflows, #chaining). GitHub is in-memory (fake_github.py), the runner is the FakeRunner."""

import json
from pathlib import Path

import pytest
from fake_github import APP_ID, APP_LOGIN, PRIVATE_KEY, FakeGitHub, user_id

from factory import db, enrolment, loop, orchestrator
from factory.contract import parse_dispatch
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
HEAD = "c" * 40
REPO = "octo/widgets"
CONFIG = "setup: uv sync\ncheck: uv run pytest\n"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    fake.add_repo(REPO, head_sha=HEAD).files[".factory/config.yml"] = CONFIG
    return fake


@pytest.fixture
def repo(fake):
    repo = fake.repos[REPO]
    repo.permissions["alice"] = "write"
    return repo


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def settings():
    return orchestrator.Settings()


@pytest.fixture(autouse=True)
def enrolled(conn, github):
    enrolment.refresh(conn, github, factory_repo="octo/factory")


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def workflows(conn):
    return conn.execute("SELECT * FROM workflows ORDER BY id").fetchall()


def ticket(repo, number=7, author="alice", labels=("agent:explore",), **kw):
    """An issue `author` wrote, with a label alice added."""
    issue = repo.add_issue(number, "Search ignores accents", "Body", author=author, **kw)
    for name in labels:
        issue.label(name, by="alice")
    return issue


def explore_result(run_id, **output):
    result = json.loads((EXAMPLES / "result" / "explore.valid.json").read_text())
    result["run_id"] = run_id
    result["output"].update(output)
    return result


def finish_explore(conn, github, runner, settings, **output):
    """Queue and dispatch the explore run, finish it with `output`, collect and act on it."""
    orchestrator.run_once(conn, github, runner, settings)
    [row] = [r for r in runs(conn) if r["status"] == "dispatched" and r["task"] == "explore"]
    runner.finish(row["runner_run_id"],
                  RunFiles(result_json=json.dumps(explore_result(row["id"], **output)).encode()))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


# --- workflow state -------------------------------------------------------------


def test_an_explore_trigger_starts_an_implement_ticket_workflow_and_marks_the_ticket_in_progress(
    conn, github, repo, settings
):
    issue = ticket(repo, labels=["agent:explore", "needs-a-human", "waiting", "agent:blocked"])
    issue.labels.append("bug")

    orchestrator.poll(conn, github, REPO, settings)

    [workflow] = workflows(conn)
    assert (workflow["repo"], workflow["kind"], workflow["subject"], workflow["status"]) == (
        REPO, "implement-ticket", "7", "active")
    [run] = runs(conn)
    assert run["workflow_id"] == workflow["id"]
    assert issue.labels == ["bug", "agent:in-progress"]


# --- verdicts -----------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["needs-a-human", "waiting"])
def test_needs_a_human_and_waiting_post_the_findings_label_the_ticket_and_stop(
    conn, github, repo, runner, settings, verdict
):
    issue = ticket(repo)

    finish_explore(conn, github, runner, settings, verdict=verdict, questions=["Which locale?"])

    [comment] = issue.comments
    assert f"**Verdict:** `{verdict}`" in comment["body"]
    assert issue.labels == [verdict]
    assert [w["status"] for w in workflows(conn)] == ["stopped"]
    assert [r["task"] for r in runs(conn)] == ["explore"]


def test_easy_call_posts_the_findings_and_queues_implement_in_the_same_workflow(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)

    explore = finish_explore(conn, github, runner, settings, verdict="easy-call")

    [comment] = issue.comments
    assert "**Verdict:** `easy-call`" in comment["body"]
    [implement] = [r for r in runs(conn) if r["task"] == "implement"]
    assert implement["workflow_id"] == explore["workflow_id"]
    assert (implement["ticket"], implement["triggered_by"], implement["triggered_by_id"]) == (
        "7", "alice", user_id("alice"))
    dispatch = parse_dispatch(json.loads(implement["dispatch"]))
    assert dispatch.branch == f"factory/7-{implement['id']}"
    assert dispatch.base_sha == HEAD
    assert dispatch.triggered_by.login == "alice"
    assert dispatch.triggered_by.id == user_id("alice")
    assert dispatch.model == "claude-opus-4-6"
    assert dispatch.timeout_minutes == 45
    # The implementer reads the plan from explore's findings, the App's own comment.
    assert "## Explore findings" in dispatch.ticket_markdown
    assert [w["status"] for w in workflows(conn)] == ["active"]
    assert issue.labels == ["agent:in-progress"]


@pytest.mark.parametrize("permission", ["read", "triage", None])
def test_easy_call_on_an_outsiders_issue_waits_for_a_human_to_add_implement(
    conn, github, repo, runner, settings, permission
):
    if permission:
        repo.permissions["mallory"] = permission
    issue = ticket(repo, author="mallory")

    finish_explore(conn, github, runner, settings, verdict="easy-call")

    [comment] = issue.comments
    assert ("@mallory, who opened this ticket, doesn't have write access, so nothing is built "
            "until someone who does reads the plan and adds `agent:implement`.") in comment["body"]
    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert [w["status"] for w in workflows(conn)] == ["stopped"]
    assert issue.labels == ["needs-a-human"]


def test_easy_call_on_a_repo_without_implement_enabled_stops_after_the_findings(
    conn, fake, github, repo, runner, settings
):
    repo.files[".factory/config.yml"] = CONFIG + "tasks: [explore]\n"
    enrolment.refresh(conn, github, factory_repo="octo/factory")
    issue = ticket(repo)

    finish_explore(conn, github, runner, settings, verdict="easy-call")

    assert ("`implement` isn't enabled for this repo (see `tasks` in `.factory/config.yml`), "
            "so nothing is built.") in issue.comments[0]["body"]
    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert [w["status"] for w in workflows(conn)] == ["stopped"]
    assert issue.labels == ["needs-a-human"]


def test_easy_call_whose_trigger_has_no_known_user_waits_for_a_human(
    conn, github, repo, runner, settings
):
    # The issue's events don't say who added the label, so there's no one to credit.
    issue = repo.add_issue(7, "T", labels=["agent:explore"], author="alice")

    finish_explore(conn, github, runner, settings, verdict="easy-call")

    assert ("Nothing is built until someone with write access adds `agent:implement`."
            in issue.comments[0]["body"])
    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert issue.labels == ["needs-a-human"]


def test_duplicate_closes_the_ticket_as_a_duplicate_as_the_app(conn, github, repo, runner, settings):
    repo.add_issue(3, "Accents in search")
    issue = ticket(repo)

    finish_explore(conn, github, runner, settings, verdict="duplicate", duplicate_of="3")

    [comment] = issue.comments
    assert "### Duplicate of\n\n#3\n" in comment["body"]
    assert (issue.state, issue.state_reason) == ("closed", "duplicate")
    [closed] = [e for e in issue.events if e["event"] == "closed"]
    assert closed["actor"]["login"] == APP_LOGIN
    assert issue.labels == []
    assert [w["status"] for w in workflows(conn)] == ["stopped"]


def test_a_ticket_closed_as_a_duplicate_and_reopened_says_so_on_its_next_explore(
    conn, github, repo, runner, settings
):
    repo.add_issue(3, "Accents in search")
    issue = ticket(repo)
    finish_explore(conn, github, runner, settings, verdict="duplicate", duplicate_of="3")
    issue.state = "open"
    issue.add_event("reopened", "alice", "2099-01-01T00:00:00Z")
    issue.label("agent:explore", by="alice", at="2099-01-01T00:00:01Z")

    orchestrator.poll(conn, github, REPO, settings)

    [_, again] = runs(conn)
    assert "duplicate of #3" in json.loads(again["dispatch"])["ticket_markdown"]
    assert again["workflow_id"] == runs(conn)[0]["workflow_id"]
    assert [w["status"] for w in workflows(conn)] == ["active"]


def test_duplicate_of_an_issue_closed_since_leaves_the_ticket_open_for_a_human(
    conn, github, repo, runner, settings
):
    older = repo.add_issue(3, "Accents in search")
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    older.state = "closed"

    finish_explore(conn, github, runner, settings, verdict="duplicate", duplicate_of="3")

    assert issue.state == "open"
    assert ("#3 has been closed since, so this ticket was left open: only a human can tell "
            "whether its fix covers this one too.") in issue.comments[0]["body"]
    assert issue.labels == ["needs-a-human"]
    assert [w["status"] for w in workflows(conn)] == ["stopped"]


def test_a_duplicate_of_an_issue_that_wasnt_sent_is_invalid_and_nothing_is_posted(
    conn, github, repo, runner, settings
):
    repo.add_issue(3, "Accents in search")
    issue = ticket(repo)

    run = finish_explore(conn, github, runner, settings, verdict="duplicate", duplicate_of="5")

    assert run["outcome"] == "invalid"
    assert issue.comments == []
    assert issue.state == "open"


def test_a_github_without_the_duplicate_close_reason_closes_it_as_not_planned(
    conn, fake, repo, runner, settings
):
    repo.add_issue(3, "Accents in search")
    issue = ticket(repo)

    def no_duplicate_reason(request):
        if request.method == "PATCH" and b'"duplicate"' in (request.body or b""):
            return Response(422, {}, b'{"message": "Validation Failed"}')
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, no_duplicate_reason, clock=fake.clock),
                    no_duplicate_reason)

    finish_explore(conn, github, runner, settings, verdict="duplicate", duplicate_of="3")

    assert (issue.state, issue.state_reason) == ("closed", "not_planned")


def test_too_big_asks_a_human_and_offers_implement_spec(conn, github, repo, runner, settings):
    issue = ticket(repo)

    finish_explore(conn, github, runner, settings, verdict="too-big")

    [comment] = issue.comments
    assert "Add `agent:implement-spec` to build this as a spec." in comment["body"]
    assert issue.labels == ["needs-a-human"]
    assert [w["status"] for w in workflows(conn)] == ["stopped"]
    assert [r["task"] for r in runs(conn)] == ["explore"]


def test_triggering_a_stopped_workflow_again_reuses_its_row_and_clears_the_stale_label(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    first = finish_explore(conn, github, runner, settings, verdict="waiting", questions=["When?"])
    issue.label("agent:explore", by="alice", at="2099-01-01T00:00:00Z")

    orchestrator.poll(conn, github, REPO, settings)

    [workflow] = workflows(conn)
    assert workflow["status"] == "active"
    assert [r["workflow_id"] for r in runs(conn)] == [workflow["id"], workflow["id"]]
    assert first["workflow_id"] == workflow["id"]
    assert issue.labels == ["agent:in-progress"]


def test_a_ticket_closed_mid_run_cancels_its_workflow_and_takes_in_progress_off(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    issue.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)  # finds it closed, asks to cancel
    orchestrator.run_once(conn, github, runner, settings)  # cancels, then ends the workflow

    assert [r["outcome"] for r in runs(conn)] == ["cancelled"]
    assert [w["status"] for w in workflows(conn)] == ["cancelled"]
    assert issue.labels == []
    assert issue.comments == []


def test_easy_call_is_handled_once_however_many_passes_follow(conn, github, repo, runner, settings):
    issue = ticket(repo)
    finish_explore(conn, github, runner, settings, verdict="easy-call")

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert len(issue.comments) == 1
    assert [r["task"] for r in runs(conn)] == ["explore", "implement"]
