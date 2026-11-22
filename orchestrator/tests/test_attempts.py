"""Attempts, retries and failures that aren't attempts (orchestrator.md#limits-and-brakes,
#dispatching-and-collecting). GitHub is in-memory (fake_github.py), the runner is the FakeRunner."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import db, enrolment, loop, orchestrator, state
from factory.contract import parse_dispatch
from factory.github import AppAuth, GitHub
from factory.runner import FakeRunner, RunFiles

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
HEAD = "c" * 40
REPO = "octo/widgets"
OTHER = "octo/gadgets"
CONFIG = "setup: uv sync\ncheck: uv run pytest\n"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    for name in (REPO, OTHER):
        fake.add_repo(name, head_sha=HEAD).files[".factory/config.yml"] = CONFIG
        fake.repos[name].permissions["alice"] = "write"
    return fake


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


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


def ticket(repo, number=7, label="agent:explore"):
    issue = repo.add_issue(number, "Search ignores accents", "Body", author="alice")
    issue.label(label, by="alice")
    return issue


def result(name, run_id, **fields):
    data = json.loads((EXAMPLES / "result" / name).read_text())
    data["run_id"] = run_id
    data.update(fields)
    return data


def explore_failed(run_id, failure="agent_error"):
    return result("explore.valid-failed.json", run_id, failure=failure)


def explore_easy_call(run_id):
    return result("explore.valid.json", run_id)


def dispatched(conn, task=None):
    [row] = [r for r in runs(conn) if r["status"] == "dispatched" and task in (None, r["task"])]
    return row


def finish(conn, github, runner, settings, data, task=None):
    """Dispatch what's queued, finish the dispatched run with `data` (a dict, raw
    bytes, or None for no files), then collect it and act on it. Returns the row."""
    orchestrator.run_once(conn, github, runner, settings)
    row = dispatched(conn, task)
    if callable(data):
        data = data(row["id"])
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    runner.finish(row["runner_run_id"], None if data is None else RunFiles(result_json=data))
    orchestrator.run_once(conn, github, runner, settings)
    return loop.get_run(conn, row["id"])


def previous_attempt(row):
    return json.loads(row["dispatch"]).get("previous_attempt")


# --- retries --------------------------------------------------------------------


def test_a_failed_run_is_retried_at_once_as_a_new_row_at_the_back_of_the_queue(
    conn, github, repo, runner, settings
):
    ticket(repo)
    first = finish(conn, github, runner, settings, explore_failed)

    retry = runs(conn)[-1]
    assert (retry["task"], retry["attempt"], retry["retry_of"], retry["status"]) == (
        "explore", 2, first["id"], "queued")
    assert first["attempt"] == 1
    assert json.loads(retry["dispatch"])["run_id"] == retry["id"]
    assert previous_attempt(retry) == {"outcome": "failed", "failure": "agent_error"}
    parse_dispatch(json.loads(retry["dispatch"]))


def implement_run(conn, github, repo, runner, settings):
    """Explore the ticket to `easy-call`, so implement is queued."""
    ticket(repo)
    finish(conn, github, runner, settings, explore_easy_call)


def check_failed(run_id):
    return result("implement.valid-check-failed.json", run_id)


def test_a_failed_check_is_retried_with_its_output_on_a_branch_named_after_the_new_run(
    conn, github, repo, runner, settings
):
    implement_run(conn, github, repo, runner, settings)
    first = finish(conn, github, runner, settings, check_failed, task="implement")

    retry = runs(conn)[-1]
    assert (retry["task"], retry["attempt"], retry["retry_of"]) == ("implement", 2, first["id"])
    dispatch = json.loads(retry["dispatch"])
    assert dispatch["branch"] == f"factory/7-{retry['id']}"
    assert dispatch["previous_attempt"] == {
        "outcome": "failed", "failure": "check_failed",
        "check_output": "FAILED tests/test_search.py::test_accents - AssertionError\n"
                        "1 failed, 41 passed in 2.31s\n"}
    parse_dispatch(dispatch)


def test_an_invalid_result_is_retried_with_why_it_was_invalid(conn, github, repo, runner, settings):
    ticket(repo)
    first = finish(conn, github, runner, settings, lambda rid: explore_easy_call(rid + 100))

    assert first["outcome"] == "invalid"
    retry = runs(conn)[-1]
    assert retry["attempt"] == 2
    assert previous_attempt(retry) == {
        "outcome": "invalid", "validation_error": f"result.json is for run {first['id'] + 100}"}


def test_a_result_that_isnt_json_is_retried_with_why(conn, github, repo, runner, settings):
    ticket(repo)
    finish(conn, github, runner, settings, b"{not json")

    previous = previous_attempt(runs(conn)[-1])
    assert previous["outcome"] == "invalid"
    assert "isn't JSON" in previous["validation_error"]


def test_a_run_that_left_no_result_is_retried_without_a_previous_attempt(
    conn, github, repo, runner, settings
):
    ticket(repo)
    first = finish(conn, github, runner, settings, None)

    assert first["outcome"] == "failed"
    retry = runs(conn)[-1]
    assert (retry["attempt"], retry["retry_of"]) == (2, first["id"])
    assert previous_attempt(retry) is None


def test_an_agent_timeout_is_retried_without_a_previous_attempt(conn, github, repo, runner, settings):
    ticket(repo)
    finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "agent_timeout"))

    retry = runs(conn)[-1]
    assert retry["attempt"] == 2
    assert previous_attempt(retry) is None


@pytest.mark.parametrize("outcome", ["timed_out", "superseded"])
def test_timed_out_and_superseded_runs_are_retried_without_a_previous_attempt(
    conn, github, repo, runner, settings, outcome
):
    ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    first = dispatched(conn)
    loop.cancel(conn, runner, first, outcome=outcome)
    orchestrator.run_once(conn, github, runner, settings)

    retry = runs(conn)[-1]
    assert (retry["attempt"], retry["retry_of"]) == (2, first["id"])
    assert previous_attempt(retry) is None


# --- giving up -------------------------------------------------------------------


def test_after_two_bad_attempts_the_ticket_is_blocked_and_the_workflow_stops(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    finish(conn, github, runner, settings, explore_failed)
    second = finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "no_output"))

    assert second["attempt"] == 2
    assert runs(conn)[-1]["id"] == second["id"]  # no third attempt
    assert "agent:blocked" in issue.labels
    assert "agent:in-progress" not in issue.labels
    [workflow] = conn.execute("SELECT * FROM workflows").fetchall()
    assert workflow["status"] == "stopped"
    [notice] = issue.posts
    assert "<!-- factory:notice -->" in notice["body"]
    assert "@alice" in notice["body"]
    assert "`agent:explore`" in notice["body"]
    # Acting on it again changes nothing.
    orchestrator.run_once(conn, github, runner, settings)
    assert len(issue.posts) == 1


def test_an_implement_that_fails_twice_blocks_the_ticket(conn, github, repo, runner, settings):
    implement_run(conn, github, repo, runner, settings)
    issue = repo.issues[7]
    for _ in range(2):
        finish(conn, github, runner, settings, check_failed, task="implement")

    assert [r["task"] for r in runs(conn)] == ["explore", "implement", "implement"]
    assert "agent:blocked" in issue.labels
    assert "`agent:implement`" in issue.posts[-1]["body"]


def test_re_adding_the_trigger_label_after_giving_up_starts_again_at_attempt_one(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    finish(conn, github, runner, settings, explore_failed)
    finish(conn, github, runner, settings, explore_failed)
    issue.label("agent:explore", by="alice", at="2026-11-21T10:00:00Z")

    orchestrator.run_once(conn, github, runner, settings)

    latest = runs(conn)[-1]
    assert (latest["attempt"], latest["retry_of"]) == (1, None)
    assert previous_attempt(latest) is None
    assert "agent:blocked" not in issue.labels


def test_a_cancelled_run_is_never_retried(conn, github, repo, runner, settings):
    ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    loop.request_cancel(conn, dispatched(conn)["id"])
    orchestrator.run_once(conn, github, runner, settings)

    assert [r["outcome"] for r in runs(conn)] == ["cancelled"]


def test_a_needs_a_human_verdict_is_an_answer_not_a_failure(conn, github, repo, runner, settings):
    issue = ticket(repo)

    def needs_a_human(run_id):
        data = explore_easy_call(run_id)
        data["output"].update(verdict="needs-a-human", questions=["Which locale?"])
        return data

    finish(conn, github, runner, settings, needs_a_human)

    assert len(runs(conn)) == 1
    assert issue.labels == ["needs-a-human"]


# --- failures that aren't attempts --------------------------------------------------


def test_a_usage_limit_queues_the_run_again_without_counting_an_attempt(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    first = finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "usage_limit"))

    again = runs(conn)[-1]
    assert (again["attempt"], again["retry_of"], again["status"]) == (1, first["id"], "queued")
    assert previous_attempt(again) is None
    # The re-run's failure is still attempt 1's, so it gets its retry.
    second = finish(conn, github, runner, settings, explore_failed)
    assert second["id"] == again["id"]
    assert runs(conn)[-1]["attempt"] == 2
    assert "agent:blocked" not in issue.labels


def test_a_usage_limit_on_a_retry_keeps_its_previous_attempt(conn, github, repo, runner, settings):
    ticket(repo)
    finish(conn, github, runner, settings, explore_failed)
    retry = finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "usage_limit"))

    again = runs(conn)[-1]
    assert (again["attempt"], again["retry_of"]) == (2, retry["id"])
    assert previous_attempt(again) == {"outcome": "failed", "failure": "agent_error"}


def test_claude_auth_holds_dispatches_for_that_repo_only(conn, github, fake, repo, runner, settings):
    ticket(repo)
    first = finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "claude_auth"))
    ticket(fake.repos[OTHER], number=3)

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    again = [r for r in runs(conn) if r["repo"] == REPO][-1]
    assert (again["attempt"], again["retry_of"], again["status"]) == (1, first["id"], "queued")
    other = [r for r in runs(conn) if r["repo"] == OTHER]
    assert [r["status"] for r in other] == ["dispatched"]
    [problem] = state.open_problems(conn)
    assert (problem["kind"], problem["repo"]) == ("claude_auth", REPO)
    [status] = [c["body"] for c in repo.issues[7].comments if c not in repo.issues[7].posts]
    assert "queued: Claude rejected this repo's token" in status


def test_contract_mismatch_pauses_dispatching_and_is_logged_once(
    conn, github, fake, repo, runner, settings, caplog
):
    ticket(repo)
    ticket(fake.repos[OTHER], number=3)
    orchestrator.run_once(conn, github, runner, settings)
    for row in [r for r in runs(conn) if r["status"] == "dispatched"]:
        data = json.dumps(explore_failed(row["id"], "contract_mismatch")).encode()
        runner.finish(row["runner_run_id"], RunFiles(result_json=data))

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert [(r["attempt"], r["status"]) for r in runs(conn)] == [
        (1, "done"), (1, "done"), (1, "queued"), (1, "queued")]
    mismatches = [r for r in caplog.records
                  if r.levelname == "ERROR" and "contract_mismatch" in r.getMessage()]
    assert len(mismatches) == 1
    [problem] = state.open_problems(conn)
    assert (problem["kind"], problem["repo"]) == ("contract_mismatch", None)
    [status] = [c["body"] for c in repo.issues[7].comments if c not in repo.issues[7].posts]
    assert "queued: dispatching paused: the runner rejected the contract version" in status


def test_a_restart_after_a_redeploy_lifts_the_contract_mismatch_pause_and_auth_holds(
    conn, github, repo, runner, settings
):
    ticket(repo)
    finish(conn, github, runner, settings, lambda rid: explore_failed(rid, "contract_mismatch"))
    state.open_problem(conn, "claude_auth", "Claude auth failed", repo=REPO)

    def sleep(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        orchestrator.serve(conn, github, runner, settings, sleep=sleep)

    assert runs(conn)[-1]["status"] == "dispatched"
    assert state.open_problems(conn) == []


# --- the runner unavailable ---------------------------------------------------------

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def test_while_the_runner_is_unavailable_rows_stay_as_they_are_and_no_attempt_is_counted(
    conn, github, repo, runner, settings
):
    ticket(repo)
    ticket(repo, number=8)
    orchestrator.run_once(conn, github, runner, orchestrator.Settings(max_in_flight=1))
    before = [(r["id"], r["status"], r["attempt"]) for r in runs(conn)]
    assert [s for _, s, _ in before] == ["dispatched", "queued"]

    runner.unavailable = True
    for _ in range(3):
        orchestrator.run_once(conn, github, runner, settings)

    assert [(r["id"], r["status"], r["attempt"]) for r in runs(conn)] == before
    runner.unavailable = False
    orchestrator.run_once(conn, github, runner, settings)
    assert [r["status"] for r in runs(conn)] == ["dispatched", "dispatched"]


def test_a_refused_run_goes_back_to_the_queue_and_isnt_dispatched_again_at_once(conn, runner):
    run_id = loop.enqueue(conn, repo=REPO, task="smoke", dispatch={"task": "smoke"})
    loop.tick(conn, runner, limit=2, now=T0)
    runner.refuse(loop.get_run(conn, run_id)["runner_run_id"])

    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=1))
    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=2))

    row = loop.get_run(conn, run_id)
    assert (row["status"], row["attempt"]) == ("queued", 1)
    assert len(runner.dispatched) == 1

    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=20))
    assert loop.get_run(conn, run_id)["status"] == "dispatched"
    assert len(runner.dispatched) == 2


def problems(conn):
    return [(p["kind"], p["repo"]) for p in state.open_problems(conn)]


def test_a_runner_unavailable_for_30_minutes_is_a_factory_problem_until_it_answers(conn, runner):
    loop.enqueue(conn, repo=REPO, task="smoke", dispatch={"task": "smoke"})
    runner.unavailable = True

    loop.tick(conn, runner, limit=2, now=T0)
    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=29))
    assert problems(conn) == []
    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=30))
    assert problems(conn) == [("runner_unavailable", None)]

    runner.unavailable = False
    loop.tick(conn, runner, limit=2, now=T0 + timedelta(minutes=31))
    assert problems(conn) == []


def test_runs_refused_for_30_minutes_are_a_factory_problem(conn, runner):
    run_id = loop.enqueue(conn, repo=REPO, task="smoke", dispatch={"task": "smoke"})
    for minutes in range(0, 50, 16):
        now = T0 + timedelta(minutes=minutes)
        loop.tick(conn, runner, limit=2, now=now)  # dispatches it
        runner.refuse(loop.get_run(conn, run_id)["runner_run_id"])
        loop.tick(conn, runner, limit=2, now=now + timedelta(minutes=1))  # refused

    assert problems(conn) == [("runner_unavailable", None)]
