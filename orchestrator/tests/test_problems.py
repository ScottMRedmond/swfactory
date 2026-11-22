"""Factory problem issues (orchestrator.md#observability).

A problem that belongs to no ticket opens one issue per kind, assigned to the
`owner` named in the factory repo's `factory-config.yml`: on the factory repo
when it's about the whole factory, on the repo when it's about one repo. It's
commented on when the problem changes and closed when it clears. GitHub is
in-memory (fake_github.py), the runner is the FakeRunner.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import LOW_USAGE
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import db, enrolment, orchestrator, state
from factory.brakes import Limits
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles

HEAD = "c" * 40
REPO = "octo/widgets"
FACTORY = "octo/factory"
CONFIG = "setup: uv sync\ncheck: uv run pytest\n"
MARKER = "<!-- factory:problem"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    fake.add_repo(FACTORY, installation_id=200).files["factory-config.yml"] = "owner: octocat\n"
    fake.add_repo(REPO, head_sha=HEAD).files[".factory/config.yml"] = CONFIG
    fake.repos[REPO].permissions["alice"] = "write"
    return fake


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def settings():
    return orchestrator.Settings(factory_repo=FACTORY)


@pytest.fixture(autouse=True)
def enrolled(conn, github):
    enrolment.refresh(conn, github, factory_repo=FACTORY)


def problem_issues(fake, name):
    return [i for i in fake.repos[name].issues.values() if MARKER in (i.body or "")]


def explore_failed(run_id, failure):
    from conftest import example

    return {**example("result", "explore.valid-failed.json"), "run_id": run_id, "failure": failure}


def fail_a_run(conn, github, fake, runner, settings, failure):
    issue = fake.repos[REPO].add_issue(7, "Search ignores accents", "Body", author="alice")
    issue.label("agent:explore", by="alice")
    orchestrator.run_once(conn, github, runner, settings)
    [row] = conn.execute("SELECT * FROM runs WHERE status = 'dispatched'").fetchall()
    data = json.dumps(explore_failed(row["id"], failure)).encode()
    runner.finish(row["runner_run_id"], RunFiles(result_json=data))
    orchestrator.run_once(conn, github, runner, settings)


def test_a_contract_mismatch_opens_one_issue_on_the_factory_repo_assigned_to_the_owner(
    conn, github, fake, runner, settings
):
    fail_a_run(conn, github, fake, runner, settings, "contract_mismatch")
    orchestrator.run_once(conn, github, runner, settings)

    [issue] = problem_issues(fake, FACTORY)
    assert issue.state == "open"
    assert issue.assignees == ["octocat"]
    assert "contract_mismatch" in issue.title
    assert REPO not in issue.title + issue.body
    assert problem_issues(fake, REPO) == []


def test_claude_auth_opens_its_issue_on_the_repo_itself(conn, github, fake, runner, settings):
    fail_a_run(conn, github, fake, runner, settings, "claude_auth")

    [issue] = problem_issues(fake, REPO)
    assert issue.assignees == ["octocat"]
    assert "Claude auth" in issue.title
    assert problem_issues(fake, FACTORY) == []


def test_there_is_one_issue_per_kind_commented_on_when_the_problem_changes(
    conn, github, fake, runner, settings
):
    state.open_problem(conn, "runner_unavailable", "Unavailable since 10:00.")
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)
    state.open_problem(conn, "runner_unavailable", "Refusing runs since 10:00.")
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    [issue] = problem_issues(fake, FACTORY)
    assert "Unavailable since 10:00." in issue.body
    assert [c["body"] for c in issue.comments] == ["Refusing runs since 10:00."]


def test_a_problem_that_clears_closes_its_issue_and_a_new_one_opens_a_new_issue(
    conn, github, fake, runner, settings
):
    state.open_problem(conn, "runner_unavailable", "Unavailable since 10:00.")
    orchestrator.run_once(conn, github, runner, settings)
    state.close_problem(conn, "runner_unavailable")
    orchestrator.run_once(conn, github, runner, settings)

    [issue] = problem_issues(fake, FACTORY)
    assert issue.state == "closed"
    assert "Cleared" in issue.comments[-1]["body"]

    state.open_problem(conn, "runner_unavailable", "Unavailable since 11:00.")
    orchestrator.run_once(conn, github, runner, settings)
    assert [i.state for i in problem_issues(fake, FACTORY)] == ["closed", "open"]


def test_a_restart_lifts_the_claude_auth_hold_and_closes_its_issue(
    conn, github, fake, runner, settings
):
    fail_a_run(conn, github, fake, runner, settings, "claude_auth")

    def sleep(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        orchestrator.serve(conn, github, runner, settings, sleep=sleep)

    [issue] = problem_issues(fake, REPO)
    assert issue.state == "closed"


def test_closing_the_issue_by_hand_doesnt_lift_the_hold(conn, github, fake, runner, settings):
    fail_a_run(conn, github, fake, runner, settings, "claude_auth")
    [issue] = problem_issues(fake, REPO)
    issue.state = "closed"

    orchestrator.run_once(conn, github, runner, settings)

    assert [p["kind"] for p in state.open_problems(conn)] == ["claude_auth"]
    assert conn.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "queued"


def test_an_enrolment_failing_its_schema_opens_a_problem_on_the_repo_until_its_fixed(
    conn, github, fake, runner, settings
):
    repo = fake.repos[REPO]
    repo.files[".factory/config.yml"] = "setup: uv sync\n"  # no check
    enrolment.refresh(conn, github, factory_repo=FACTORY)
    orchestrator.run_once(conn, github, runner, settings)

    [issue] = problem_issues(fake, REPO)
    assert issue.state == "open" and issue.assignees == ["octocat"]
    assert "config.yml" in issue.title and "check" in issue.body

    enrolment.refresh(conn, github, factory_repo=FACTORY)
    orchestrator.run_once(conn, github, runner, settings)
    assert len(problem_issues(fake, REPO)) == 1

    repo.files[".factory/config.yml"] = CONFIG
    enrolment.refresh(conn, github, factory_repo=FACTORY)
    orchestrator.run_once(conn, github, runner, settings)
    assert [i.state for i in problem_issues(fake, REPO)] == ["closed"]


def ago(minutes):
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_usage_unreadable_for_15_minutes_opens_a_problem_on_the_factory_repo_until_its_read(
    conn, github, fake, runner, settings
):
    state.record_usage(conn, LOW_USAGE, read_at=ago(30))
    state.record_usage(conn, None, error="no usage_report", read_at=ago(16))
    state.record_usage(conn, None, error="claude exited 1", read_at=ago(1))
    orchestrator.run_once(conn, github, runner, settings)
    [issue] = problem_issues(fake, FACTORY)
    assert "usage" in issue.title and "claude exited 1" in issue.body
    assert problem_issues(fake, REPO) == []

    state.record_usage(conn, LOW_USAGE)
    orchestrator.run_once(conn, github, runner, settings)
    assert issue.state == "closed"


def test_usage_unreadable_for_less_than_15_minutes_opens_nothing(
    conn, github, fake, runner, settings
):
    state.record_usage(conn, LOW_USAGE, read_at=ago(30))
    state.record_usage(conn, None, error="no usage_report", read_at=ago(14))
    orchestrator.run_once(conn, github, runner, settings)

    assert state.open_problems(conn) == []


def test_the_daily_cap_and_a_usage_pause_never_open_one(conn, github, fake, runner):
    settings = orchestrator.Settings(factory_repo=FACTORY, limits=Limits(daily_cap=0))
    issue = fake.repos[REPO].add_issue(7, "Search ignores accents", "Body", author="alice")
    issue.label("agent:explore", by="alice")
    state.record_usage(conn, [{"kind": "session", "percent": 99,
                               "resets_at": "2099-01-01T00:00:00+00:00"}], read_at=ago(30))
    for _ in range(3):
        orchestrator.run_once(conn, github, runner, settings)

    assert state.open_problems(conn) == []
    assert problem_issues(fake, FACTORY) == problem_issues(fake, REPO) == []


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def failing_polls(fake, clock, broken):
    """A GitHub client over `fake` where `broken(request)` answers 503 (with a
    body that mustn't leak) instead of the fake."""

    def transport(request):
        if broken(request):
            return Response(503, {}, b'{"message": "upstream ghp_' + b"a" * 36 + b' secret detail"}')
        return fake(request)

    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=clock), transport)


def poll_for(conn, github, runner, settings, poller, clock, minutes):
    for _ in range(minutes):
        orchestrator.run_once(conn, github, runner, settings, poller=poller)
        clock.t += 60


def test_polls_failing_for_30_minutes_open_a_problem_once_the_repos_api_answers(
    conn, fake, runner, settings
):
    clock = Clock()
    fake.clock = clock
    down = {"on": True}
    github = failing_polls(fake, clock, lambda r: down["on"] and f"/repos/{REPO}/" in r.url)
    poller = orchestrator.Poller(clock=clock)

    poll_for(conn, github, runner, settings, poller, clock, 29)
    assert state.open_problems(conn) == []
    poll_for(conn, github, runner, settings, poller, clock, 20)
    assert [(p["kind"], p["repo"]) for p in state.open_problems(conn)] == [("poll_failing", REPO)]
    assert problem_issues(fake, REPO) == []  # its API isn't answering

    down["on"] = False
    poll_for(conn, github, runner, settings, poller, clock, 16)

    [issue] = problem_issues(fake, REPO)
    assert "polling" in issue.title
    assert issue.state == "closed"
    text = issue.body + "".join(c["body"] for c in issue.comments)
    assert "secret detail" not in text and "ghp_" not in text
    assert problem_issues(fake, FACTORY) == []


def test_polls_failing_while_the_repo_takes_issues_open_the_problem_at_once(
    conn, fake, runner, settings
):
    clock = Clock()
    fake.clock = clock
    github = failing_polls(fake, clock, lambda r: r.method == "GET" and r.url.split("?")[0]
                           .endswith(f"/repos/{REPO}/issues"))
    poller = orchestrator.Poller(clock=clock)

    poll_for(conn, github, runner, settings, poller, clock, 50)  # backoff: tries at 29 and 44 min

    [issue] = problem_issues(fake, REPO)
    assert issue.state == "open" and "HTTP 503" in issue.body
    assert "secret detail" not in issue.body


def test_changing_the_owner_is_a_commit_to_factory_config(conn, github, fake, runner, settings):
    fake.repos[FACTORY].files["factory-config.yml"] = "owner: hubot\n"
    enrolment.refresh(conn, github, factory_repo=FACTORY)
    state.open_problem(conn, "contract_mismatch", "The runner rejected the contract.")
    orchestrator.run_once(conn, github, runner, settings)

    [issue] = problem_issues(fake, FACTORY)
    assert issue.assignees == ["hubot"]


def test_a_failing_write_is_tried_again_on_the_next_pass(conn, fake, runner, settings):
    down = {"on": True}

    def transport(request):
        if down["on"] and request.method == "POST" and request.url.endswith(f"{FACTORY}/issues"):
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)
    state.open_problem(conn, "contract_mismatch", "The runner rejected the contract.")
    orchestrator.run_once(conn, github, runner, settings)
    assert problem_issues(fake, FACTORY) == []

    down["on"] = False
    orchestrator.run_once(conn, github, runner, settings)
    assert len(problem_issues(fake, FACTORY)) == 1
