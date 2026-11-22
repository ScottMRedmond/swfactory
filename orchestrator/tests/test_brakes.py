"""Brakes (orchestrator.md#limits-and-brakes): the usage pause, the daily cap
and the manual pause hold queued runs back, and each held-back run's status
line says why. Driven through `orchestrator.run_once` with the FakeRunner,
an in-memory GitHub and a fake usage reader (`usage.READ`)."""

from datetime import UTC, datetime, timedelta

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import cli, db, enrolment, loop, orchestrator, state, usage
from factory.brakes import Limits
from factory.github import AppAuth, GitHub

REPO = "octo/widgets"
OTHER = "octo/gadgets"
MARK = "<!-- factory:status -->"


def from_now(**kwargs) -> str:
    return (datetime.now(UTC) + timedelta(**kwargs)).isoformat(timespec="seconds")


def ago(**kwargs) -> str:
    return (datetime.now(UTC) - timedelta(**kwargs)).isoformat(timespec="seconds")


def reading(session=10, weekly=5, *, resets=None, extra=()):
    return [
        {"kind": "session", "percent": session, "resets_at": resets or from_now(hours=2)},
        {"kind": "weekly_all", "percent": weekly, "resets_at": from_now(days=3)},
        *extra,
    ]


class Reader:
    """A fake `claude -p /usage`: returns `limits` (or an error) and counts calls."""

    def __init__(self, limits=None, error=None):
        self.limits = reading() if limits is None and error is None else limits
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.limits, self.error


@pytest.fixture
def reader(monkeypatch):
    r = Reader()
    monkeypatch.setattr(usage, "READ", r)
    return r


@pytest.fixture
def path(tmp_path):
    return tmp_path / "factory.db"


@pytest.fixture
def conn(path):
    return db.connect(path)


@pytest.fixture
def fake():
    fake = FakeGitHub()
    for name in (REPO, OTHER):
        repo = fake.add_repo(name)
        repo.files[".factory/config.yml"] = "setup: uv sync\ncheck: uv run pytest\n"
    return fake


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


@pytest.fixture
def runner():
    from factory.runner import FakeRunner
    return FakeRunner()


@pytest.fixture(autouse=True)
def enrolled(conn, github):
    enrolment.refresh(conn, github, factory_repo="octo/factory")


def queue(conn, ticket=7, repo=REPO, task="explore"):
    return loop.enqueue(conn, repo=repo, task=task, ticket=str(ticket),
                        dispatch={"task": task, "repo": repo})


def run_once(conn, github, runner, settings=None, **kw):
    orchestrator.run_once(conn, github, runner, settings or orchestrator.Settings(), **kw)


def statuses(conn):
    return [r["status"] for r in conn.execute("SELECT status FROM runs ORDER BY id")]


def status_line(fake, ticket=7, repo=REPO):
    issue = fake.repos[repo].issues.get(ticket)
    [comment] = [c for c in issue.comments if MARK in c["body"]]
    [line] = [l for l in comment["body"].splitlines() if l.startswith("- ")]
    return line


@pytest.fixture(autouse=True)
def issues(fake):
    for name in (REPO, OTHER):
        for n in (7, 8, 9):
            fake.repos[name].add_issue(n, f"Ticket {n}", "Body")


# --- the usage pause ---


def test_low_usage_lets_runs_dispatch_and_the_reading_is_kept(conn, github, runner, reader):
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched"]
    assert reader.calls == 1
    assert state.latest_usage(conn).limits == reader.limits


def test_a_reading_under_5_minutes_old_is_reused(conn, github, runner, reader):
    state.record_usage(conn, reading(), read_at=ago(minutes=4))
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched"]
    assert reader.calls == 0


def test_an_older_reading_is_read_again_before_dispatching(conn, github, runner, reader):
    state.record_usage(conn, reading(), read_at=ago(minutes=6))
    queue(conn)

    run_once(conn, github, runner)

    assert reader.calls == 1


def test_usage_is_not_read_when_nothing_would_be_dispatched(conn, github, runner, reader):
    run_once(conn, github, runner)  # nothing queued
    queue(conn)
    run_once(conn, github, runner, orchestrator.Settings(limits=Limits(max_in_flight=0)))

    assert reader.calls == 0


@pytest.mark.parametrize("limits, why", [
    (reading(session=60), "Claude 5-hour usage at 60%"),
    (reading(weekly=50), "Claude weekly usage at 50%"),
    (reading(extra=[{"kind": "weekly_scoped", "group": "weekly", "percent": 50,
                     "scope": {"model": {"display_name": "Opus"}}, "resets_at": from_now(days=2)}]),
     "Claude weekly (Opus) usage at 50%"),
    (reading(extra=[{"kind": "monthly_new", "percent": 51, "resets_at": from_now(days=9)}]),
     "Claude monthly_new usage at 51%"),
])
def test_high_usage_holds_the_queue(conn, fake, github, runner, reader, limits, why):
    reader.limits = limits
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["queued"]
    assert runner.dispatched == []
    assert status_line(fake).startswith(f"- `explore` 1/2: queued: {why}, resumes about ")


def test_usage_just_under_the_thresholds_dispatches(conn, github, runner, reader):
    reader.limits = reading(session=59, weekly=49)
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched"]


def test_the_thresholds_are_config(conn, github, runner, reader):
    reader.limits = reading(session=64, weekly=55)
    queue(conn)

    run_once(conn, github, runner, orchestrator.Settings(
        limits=Limits(five_hour_threshold=70, weekly_threshold=60)))

    assert statuses(conn) == ["dispatched"]


def test_while_paused_for_usage_it_doesnt_read_again_until_the_reset(conn, github, runner, reader):
    state.record_usage(conn, reading(session=64, resets=from_now(minutes=30)), read_at=ago(minutes=50))
    queue(conn)

    run_once(conn, github, runner)

    assert reader.calls == 0
    assert statuses(conn) == ["queued"]


def test_after_the_reset_it_reads_again_and_dispatches(conn, github, runner, reader):
    state.record_usage(conn, reading(session=64, resets=ago(minutes=1)), read_at=ago(minutes=50))
    queue(conn)

    run_once(conn, github, runner)

    assert reader.calls == 1
    assert statuses(conn) == ["dispatched"]


@pytest.mark.parametrize("error", ["no usage_report", "rate_limits is null", "claude exited 1"])
def test_unreadable_usage_holds_the_queue(conn, fake, github, runner, reader, error):
    reader.limits, reader.error = None, error
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["queued"]
    assert state.latest_usage(conn).error == error
    assert status_line(fake).startswith("- `explore` 1/2: queued: Claude usage unknown")


def test_a_reading_over_60_minutes_old_holds_the_queue(conn, github, runner, reader):
    # The last reading is old, and reading again fails: the old one isn't trusted.
    state.record_usage(conn, reading(), read_at=ago(minutes=61))
    reader.limits, reader.error = None, "claude exited 1"
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["queued"]


def test_crossing_a_threshold_never_cancels_runs_in_flight(conn, github, runner, reader):
    queue(conn)
    run_once(conn, github, runner)
    queue(conn, ticket=8)
    state.record_usage(conn, reading(session=90))

    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched", "queued"]
    assert runner.cancelled == []


def test_waking_up_reads_usage_afresh(conn, github, runner, reader):
    now = [1_800_000_000.0]
    poller = orchestrator.Poller(clock=lambda: now[0])
    run_once(conn, github, runner, poller=poller)  # startup: a fresh reading
    assert reader.calls == 1

    now[0] += 60
    run_once(conn, github, runner, poller=poller)
    assert reader.calls == 1

    now[0] += 6 * 60  # asleep for 6 minutes
    run_once(conn, github, runner, poller=poller)
    assert reader.calls == 2


# --- the daily cap ---


def dispatched_at(conn, run_id, when):
    with conn:
        conn.execute("UPDATE runs SET status = 'done', outcome = 'succeeded', handled_at = ?, "
                     "shown_at = ?, dispatched_at = ? WHERE id = ?", (when, when, when, run_id))


def test_the_daily_cap_counts_every_run_dispatched_today_except_smoke(conn, fake, github, runner, reader):
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today = (midnight + timedelta(seconds=1)).astimezone(UTC).isoformat(timespec="seconds")
    yesterday = (midnight - timedelta(seconds=1)).astimezone(UTC).isoformat(timespec="seconds")
    dispatched_at(conn, queue(conn, ticket=1), today)
    dispatched_at(conn, queue(conn, ticket=2, task="smoke"), today)
    dispatched_at(conn, queue(conn, ticket=3), yesterday)
    first, second = queue(conn, ticket=8), queue(conn, ticket=9)

    run_once(conn, github, runner, orchestrator.Settings(limits=Limits(daily_cap=2)))

    assert loop.get_run(conn, first)["status"] == "dispatched"
    assert loop.get_run(conn, second)["status"] == "queued"
    assert status_line(fake, 9).startswith("- `explore` 1/2: queued: daily cap reached")


# --- the manual pause ---


def cli_run(path, *args):
    return cli.main(["--db", str(path), *args])


def test_pause_holds_the_queue_but_in_flight_runs_are_collected(conn, path, github, runner, reader, capsys):
    queue(conn)
    run_once(conn, github, runner)
    [(rid, _)] = runner.dispatched

    assert cli_run(path, "pause") == 0
    assert "paused" in capsys.readouterr().out
    queue(conn, ticket=8)
    runner.finish(rid, None)
    run_once(conn, github, runner)

    assert statuses(conn) == ["done", "queued"]
    assert reader.calls == 1  # no reading while paused
    assert runner.cancelled == []


def test_a_paused_run_says_so(conn, fake, path, github, runner, reader):
    cli_run(path, "pause")
    queue(conn)

    run_once(conn, github, runner)

    assert status_line(fake).startswith("- `explore` 1/2: queued: factory paused")


def test_pause_survives_a_restart_and_resume_lifts_it(conn, path, github, runner, reader):
    cli_run(path, "pause")
    conn.close()
    conn = db.connect(path)
    queue(conn)
    run_once(conn, github, runner)
    assert statuses(conn) == ["queued"]

    assert cli_run(path, "resume") == 0
    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched"]


def test_pause_refuses_a_database_at_another_schema_version(path, capsys):
    raw = db.connect(path)
    raw.execute("PRAGMA user_version = 1")
    raw.close()

    assert cli_run(path, "pause") == 1
    assert "schema" in capsys.readouterr().err


# --- per-repo holds ---


def test_a_disabled_repos_run_doesnt_take_the_free_slot(conn, fake, github, runner, reader):
    fake.repos[REPO].files[".factory/config.yml"] += "enabled: false\n"
    enrolment.refresh(conn, github, factory_repo="octo/factory")
    held = queue(conn)
    other = queue(conn, repo=OTHER)

    run_once(conn, github, runner, orchestrator.Settings(limits=Limits(max_in_flight=1)))

    assert loop.get_run(conn, held)["status"] == "queued"
    assert loop.get_run(conn, other)["status"] == "dispatched"
    assert status_line(fake).startswith("- `explore` 1/2: queued: `enabled: false`")


def test_status_shows_a_disabled_repos_run_as_held(conn, fake, path, github, runner, capsys):
    fake.repos[REPO].files[".factory/config.yml"] += "enabled: false\n"
    enrolment.refresh(conn, github, factory_repo="octo/factory")
    state.record_usage(conn, reading())
    queue(conn)
    queue(conn, repo=OTHER)

    assert cli_run(path, "status") == 0
    out = capsys.readouterr().out
    lines = out[out.index("queue:"):].splitlines()
    assert lines[2].endswith("`enabled: false` in `.factory/config.yml`")
    assert lines[3].endswith("next to dispatch")


# --- holds from failures that aren't attempts (factory.attempts) ---


def test_a_claude_auth_hold_is_a_per_repo_hold_and_takes_no_slot(conn, fake, github, runner, reader):
    state.open_problem(conn, "claude_auth", "token rejected", repo=REPO)
    held = queue(conn)
    other = queue(conn, repo=OTHER)

    run_once(conn, github, runner, orchestrator.Settings(limits=Limits(max_in_flight=1)))

    assert loop.get_run(conn, held)["status"] == "queued"
    assert loop.get_run(conn, other)["status"] == "dispatched"
    assert status_line(fake).startswith("- `explore` 1/2: queued: Claude rejected this repo's token")


def test_resume_doesnt_lift_a_contract_mismatch_pause(conn, fake, path, github, runner, reader, capsys):
    state.open_problem(conn, "contract_mismatch", "runner rejected the contract version")
    cli_run(path, "pause")
    capsys.readouterr()

    assert cli_run(path, "resume") == 0
    assert "contract_mismatch" in capsys.readouterr().out
    queue(conn)
    run_once(conn, github, runner)

    assert statuses(conn) == ["queued"]
    assert reader.calls == 0
    assert status_line(fake).startswith(
        "- `explore` 1/2: queued: dispatching paused: the runner rejected the contract version")
    cli_run(path, "status")
    assert "paused: yes (contract_mismatch)" in capsys.readouterr().out


def test_after_a_refusal_dispatching_waits_and_says_why(conn, fake, github, runner, reader):
    state.set_runner_refused_at(conn, ago(minutes=5))
    queue(conn)

    run_once(conn, github, runner)

    assert statuses(conn) == ["queued"]
    assert reader.calls == 0  # no usage reading while nothing can go
    assert status_line(fake).startswith(
        "- `explore` 1/2: queued: the runner refused a run, next try about ")

    state.set_runner_refused_at(conn, ago(minutes=16))
    run_once(conn, github, runner)

    assert statuses(conn) == ["dispatched"]
