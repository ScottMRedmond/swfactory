"""Timeouts and cancelling: overrunning runs, closed issues and PRs, and
`factory cancel`. GitHub is in-memory (fake_github.py), the runner is the
FakeRunner."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub, discover, enrol

from factory import cli, db, loop, orchestrator
from factory.github import AppAuth, GitHub
from factory.runner import FakeRunner, RunFiles, RunnerUnavailable

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
SMOKE_DISPATCH = json.loads((EXAMPLES / "dispatch" / "smoke.valid.json").read_text())
REPO = "octo/widgets"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "factory.db"


@pytest.fixture
def conn(path):
    return db.connect(path)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def fake():
    fake = FakeGitHub()
    enrol(fake.add_repo(REPO))
    return fake


@pytest.fixture(autouse=True)
def enrolled(conn, fake):
    discover(conn, fake)


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


def enqueue(conn, *, timeout_minutes=20, ticket=None, **dispatch):
    d = {k: v for k, v in SMOKE_DISPATCH.items() if k != "run_id"}
    d.update(repo=REPO, timeout_minutes=timeout_minutes, **dispatch)
    return loop.enqueue(conn, repo=REPO, task="smoke", ticket=ticket, dispatch=d)


def row(conn, run_id):
    return loop.get_run(conn, run_id)


def dispatched_at(conn, run_id):
    return datetime.fromisoformat(row(conn, run_id)["dispatched_at"])


# --- timeouts -----------------------------------------------------------------


def test_a_run_still_running_15_minutes_past_its_timeout_is_cancelled_and_timed_out(conn, runner):
    run_id = enqueue(conn, timeout_minutes=20)
    loop.tick(conn, runner, limit=2)
    rid = row(conn, run_id)["runner_run_id"]

    loop.tick(conn, runner, limit=2, now=dispatched_at(conn, run_id) + timedelta(minutes=35, seconds=1))

    r = row(conn, run_id)
    assert (r["status"], r["outcome"], r["result"]) == ("done", "timed_out", None)
    assert runner.cancelled == [rid]


def test_a_run_within_its_grace_period_keeps_running(conn, runner):
    run_id = enqueue(conn, timeout_minutes=20)
    loop.tick(conn, runner, limit=2)

    loop.tick(conn, runner, limit=2, now=dispatched_at(conn, run_id) + timedelta(minutes=35))

    assert row(conn, run_id)["status"] == "dispatched"
    assert runner.cancelled == []


def test_each_run_uses_the_timeout_in_its_own_dispatch(conn, runner):
    short = enqueue(conn, timeout_minutes=5)
    long = enqueue(conn, timeout_minutes=45)
    loop.tick(conn, runner, limit=2)

    loop.tick(conn, runner, limit=2, now=dispatched_at(conn, short) + timedelta(minutes=30))

    assert row(conn, short)["outcome"] == "timed_out"
    assert row(conn, long)["status"] == "dispatched"


def test_a_late_run_that_has_finished_is_collected_not_timed_out(conn, runner):
    run_id = enqueue(conn, timeout_minutes=20)
    loop.tick(conn, runner, limit=2)
    result = {**json.loads((EXAMPLES / "result" / "smoke.valid.json").read_text()), "run_id": run_id}
    runner.finish(row(conn, run_id)["runner_run_id"], RunFiles(json.dumps(result).encode()))

    loop.tick(conn, runner, limit=2, now=dispatched_at(conn, run_id) + timedelta(hours=10))

    assert row(conn, run_id)["outcome"] == "succeeded"
    assert runner.cancelled == []


def test_task_timeouts_default_to_the_documented_limits():
    assert orchestrator.Settings().timeouts == {
        "explore": 20, "auto-tickets": 20, "implement": 45, "review": 45,
    }


def test_the_configured_timeout_travels_in_the_dispatch(conn, github, repo, runner):
    settings = orchestrator.Settings(timeouts={"explore": 7})
    repo.add_issue(7, "T", labels=["agent:explore"])

    orchestrator.run_once(conn, github, runner, settings)

    [(_, sent)] = runner.dispatched
    assert sent["timeout_minutes"] == 7


# --- closing an issue or PR ---------------------------------------------------


def settings():
    return orchestrator.Settings()


def test_closing_an_issue_cancels_its_queued_rows(conn, github, repo, runner):
    repo.add_issue(7, "T", state="closed")
    run_id = enqueue(conn, ticket="7")

    orchestrator.run_once(conn, github, runner, settings())

    r = row(conn, run_id)
    assert (r["status"], r["outcome"]) == ("done", "cancelled")
    assert runner.dispatched == []


def test_closing_an_issue_cancels_its_dispatched_run_and_discards_its_result(
    conn, github, repo, runner
):
    issue = repo.add_issue(7, "T")
    run_id = enqueue(conn, ticket="7")
    orchestrator.run_once(conn, github, runner, settings())
    rid = row(conn, run_id)["runner_run_id"]
    result = {**json.loads((EXAMPLES / "result" / "smoke.valid.json").read_text()), "run_id": run_id}
    runner.finish(rid, RunFiles(json.dumps(result).encode()))
    issue.state = "closed"

    orchestrator.run_once(conn, github, runner, settings())

    r = row(conn, run_id)
    assert (r["status"], r["outcome"], r["result"]) == ("done", "cancelled", None)


def test_closing_an_issue_cancels_a_running_run_on_the_runner(conn, github, repo, runner):
    issue = repo.add_issue(7, "T")
    run_id = enqueue(conn, ticket="7")
    orchestrator.run_once(conn, github, runner, settings())
    issue.state = "closed"

    orchestrator.run_once(conn, github, runner, settings())

    assert runner.cancelled == [row(conn, run_id)["runner_run_id"]]
    assert row(conn, run_id)["outcome"] == "cancelled"


def test_closing_a_pr_cancels_the_runs_working_on_it(conn, github, repo, runner):
    repo.add_issue(3, "Ticket")
    repo.add_issue(12, "PR for #3", state="closed", pull_request=True)
    run_id = enqueue(conn, ticket="3", pr_number=12)

    orchestrator.run_once(conn, github, runner, settings())

    assert row(conn, run_id)["outcome"] == "cancelled"


def test_runs_on_open_issues_are_left_alone(conn, github, repo, runner):
    repo.add_issue(7, "T")
    repo.add_issue(8, "Other", state="closed")
    run_id = enqueue(conn, ticket="7")

    orchestrator.run_once(conn, github, runner, settings())

    assert row(conn, run_id)["status"] == "dispatched"
    assert runner.cancelled == []


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def test_an_incremental_poll_sees_an_issue_closed_since_the_last_poll(conn, runner):
    clock = Clock()
    fake = FakeGitHub(clock=clock)
    enrol(fake.add_repo(REPO))
    discover(conn, fake)
    repo = fake.repos[REPO]
    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=clock), fake)
    poller = orchestrator.Poller(clock=clock)
    closing = repo.add_issue(7, "Closed later")
    repo.add_issue(8, "Stays open")
    closed_run, open_run = enqueue(conn, ticket="7"), enqueue(conn, ticket="8")
    orchestrator.run_once(conn, github, runner, settings(), poller=poller)  # full scan
    clock.t += 60
    closing.state, closing.updated_at = "closed", clock()

    orchestrator.run_once(conn, github, runner, settings(), poller=poller)

    assert row(conn, closed_run)["outcome"] == "cancelled"
    assert runner.cancelled == [row(conn, closed_run)["runner_run_id"]]
    assert row(conn, open_run)["status"] == "dispatched"


# --- factory cancel -----------------------------------------------------------


def run_cli(capsys, path, *args):
    code = cli.main(["--db", str(path), *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_factory_cancel_only_records_a_request(conn, path, runner, capsys):
    run_id = enqueue(conn)
    loop.tick(conn, runner, limit=2)

    code, out, _ = run_cli(capsys, path, "cancel", str(run_id))

    assert code == 0
    assert f"run {run_id}" in out
    assert row(conn, run_id)["status"] == "dispatched"
    assert runner.cancelled == []


def test_the_next_loop_cancels_the_requested_run_on_the_runner(conn, path, runner, capsys):
    run_id = enqueue(conn)
    other = enqueue(conn)
    loop.tick(conn, runner, limit=2)
    run_cli(capsys, path, "cancel", str(run_id))

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"]) == ("done", "cancelled")
    assert runner.cancelled == [r["runner_run_id"]]
    assert row(conn, other)["status"] == "dispatched"


def test_cancelling_a_queued_run_means_it_is_never_dispatched(conn, path, runner, capsys):
    run_id = enqueue(conn)
    run_cli(capsys, path, "cancel", str(run_id))

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["outcome"] == "cancelled"
    assert runner.dispatched == []


def test_a_finished_run_cant_be_cancelled(conn, path, runner, capsys):
    run_id = enqueue(conn)
    loop.tick(conn, runner, limit=2)
    runner.forget(row(conn, run_id)["runner_run_id"])
    loop.tick(conn, runner, limit=2)

    code, _, err = run_cli(capsys, path, "cancel", str(run_id))

    assert code == 1
    assert "already done" in err
    assert row(conn, run_id)["outcome"] == "failed"


def test_cancelling_an_unknown_run_fails(conn, path, capsys):
    code, _, err = run_cli(capsys, path, "cancel", "99")

    assert code == 1
    assert "no run 99" in err


def test_cancel_doesnt_create_a_missing_database(tmp_path, capsys):
    path = tmp_path / "nothing.db"

    code, _, _ = run_cli(capsys, path, "cancel", "1")

    assert code == 1
    assert not path.exists()


class FlakyCancel(FakeRunner):
    down = True

    def cancel(self, repo, runner_run_id):
        if self.down:
            raise RunnerUnavailable("runner down")
        super().cancel(repo, runner_run_id)


def test_a_cancel_the_runner_cant_take_yet_is_tried_again_next_loop(conn, path, capsys):
    runner = FlakyCancel()
    run_id = enqueue(conn)
    loop.tick(conn, runner, limit=2)
    run_cli(capsys, path, "cancel", str(run_id))

    loop.tick(conn, runner, limit=2)
    assert row(conn, run_id)["status"] == "dispatched"

    runner.down = False
    loop.tick(conn, runner, limit=2)
    assert row(conn, run_id)["outcome"] == "cancelled"


def test_a_timeout_cancel_the_runner_cant_take_yet_leaves_the_run_dispatched(conn):
    runner = FlakyCancel()
    run_id = enqueue(conn, timeout_minutes=20)
    loop.tick(conn, runner, limit=2)

    loop.tick(conn, runner, limit=2, now=dispatched_at(conn, run_id) + timedelta(hours=1))

    assert row(conn, run_id)["status"] == "dispatched"
