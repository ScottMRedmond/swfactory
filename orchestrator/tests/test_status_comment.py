"""The status comment (orchestrator.md#observability): one comment per issue or
PR, edited in place, one line per run. Driven through `orchestrator.run_once`;
GitHub is in-memory (fake_github.py), the runner is the FakeRunner."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fake_github import APP_ID, APP_LOGIN, PRIVATE_KEY, FakeGitHub

from factory import db, enrolment, loop, orchestrator, state
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles
from factory.workflows import base

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
REPO = "octo/widgets"
MARK = "<!-- factory:status -->"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    repo = fake.add_repo(REPO)
    repo.files[".factory/config.yml"] = "setup: uv sync\ncheck: uv run pytest\n"
    repo.permissions["alice"] = "write"
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


def ticket(repo, number=7, label="agent:explore"):
    issue = repo.add_issue(number, "Search ignores accents", "Body", author="alice")
    issue.label(label, by="alice")
    return issue


def status_comments(issue):
    return [c for c in issue.comments if MARK in c["body"]]


def status(issue) -> str:
    [comment] = status_comments(issue)
    assert comment["user"]["login"] == APP_LOGIN
    return comment["body"]


def run_lines(issue) -> list[str]:
    return [line for line in status(issue).splitlines() if line.startswith("- ")]


def only_run(conn):
    [row] = conn.execute("SELECT * FROM runs").fetchall()
    return row


def finish(conn, runner, row, result):
    runner.finish(row["runner_run_id"], RunFiles(result_json=json.dumps(result).encode()))


def explore_result(run_id, name="explore.valid.json"):
    result = json.loads((EXAMPLES / "result" / name).read_text())
    result["run_id"] = run_id
    return result


def fresh_usage(conn, percent=10):
    state.record_usage(conn, [{"kind": "session", "percent": percent,
                               "resets_at": "2099-01-01T00:00:00+00:00"}])


def test_queuing_the_first_run_creates_the_status_comment(conn, github, repo, runner):
    issue = ticket(repo)
    fresh_usage(conn)

    orchestrator.run_once(conn, github, runner, orchestrator.Settings(max_in_flight=0))

    run = only_run(conn)
    assert status(issue).startswith(MARK)
    assert run_lines(issue) == [f"- `explore` 1/2: queued: waiting for a free slot · run {run['id']}"]


def backdate(conn, run_id, minutes):
    """The run was dispatched `minutes` ago."""
    row = conn.execute("SELECT dispatched_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    then = datetime.fromisoformat(row["dispatched_at"]) - timedelta(minutes=minutes)
    with conn:
        conn.execute("UPDATE runs SET dispatched_at = ? WHERE id = ?",
                     (then.isoformat(timespec="seconds"), run_id))


def test_the_comment_is_edited_in_place_as_the_run_goes_from_running_to_done(
    conn, github, repo, runner, settings
):
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    run = only_run(conn)
    backdate(conn, run["id"], 4)
    orchestrator.run_once(conn, github, runner, settings)
    log = f"[log]({runner.log_link(REPO, run['runner_run_id'])})"
    assert run_lines(issue) == [f"- `explore` 1/2: running · 4 min · {log} · run {run['id']}"]
    comment_id = status_comments(issue)[0]["id"]

    finish(conn, runner, run, explore_result(run["id"]))
    orchestrator.run_once(conn, github, runner, settings)

    explore, implement = run_lines(issue)  # easy-call queued implement
    assert explore == f"- `explore` 1/2: succeeded · 4 min · {log} · run {run['id']}"
    assert implement.startswith("- `implement` 1/2: queued")
    assert status_comments(issue)[0]["id"] == comment_id


def test_a_failed_run_says_why(conn, github, repo, runner, settings):
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    run = only_run(conn)

    finish(conn, runner, run, explore_result(run["id"], "explore.valid-failed.json"))
    orchestrator.run_once(conn, github, runner, settings)

    line, retry = run_lines(issue)
    assert line.startswith("- `explore` 1/2: failed: agent_timeout · <1 min · [log](")
    assert retry.startswith("- `explore` 2/2: ")


def test_nothing_is_edited_when_nothing_changed(conn, fake, github, repo, runner, settings):
    ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    fake.requests.clear()

    orchestrator.run_once(conn, github, runner, settings)

    assert [r for r in fake.requests if "/comments" in r.url] == []


def test_a_later_trigger_adds_a_line_to_the_same_comment(conn, github, repo, runner, settings):
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    first = only_run(conn)
    finish(conn, runner, first, explore_result(first["id"], "explore.valid-failed.json"))
    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)  # dispatches the retry
    retry = conn.execute("SELECT * FROM runs WHERE retry_of = ?", (first["id"],)).fetchone()
    finish(conn, runner, retry, explore_result(retry["id"], "explore.valid-failed.json"))
    orchestrator.run_once(conn, github, runner, settings)  # gives up: agent:blocked

    issue.label("agent:explore", by="alice")
    orchestrator.run_once(conn, github, runner, settings)

    lines = run_lines(issue)
    assert len(lines) == 3
    assert lines[0].startswith("- `explore` 1/2: failed: agent_timeout")
    assert lines[1].startswith("- `explore` 2/2: failed: agent_timeout")
    assert lines[2].startswith("- `explore` 1/2: running")
    assert lines[2].endswith(f"run {retry['id'] + 1}")


@pytest.mark.parametrize("brake, why", [
    (lambda conn: state.set_paused(conn, True), "factory paused"),
    (lambda conn: fresh_usage(conn, percent=64), "Claude 5-hour usage at 64%"),
])
def test_a_held_back_run_says_why(conn, github, repo, runner, brake, why):
    issue = ticket(repo)
    fresh_usage(conn)
    brake(conn)

    orchestrator.run_once(conn, github, runner, orchestrator.Settings(max_in_flight=0))

    [line] = run_lines(issue)
    assert line.startswith(f"- `explore` 1/2: queued: {why}")


def test_a_run_of_a_disabled_repo_says_so(conn, github, repo, runner, settings):
    issue = ticket(repo)
    repo.files[".factory/config.yml"] += "enabled: false\n"
    enrolment.refresh(conn, github, factory_repo="octo/factory")

    orchestrator.run_once(conn, github, runner, settings)

    [line] = run_lines(issue)
    assert line.startswith("- `explore` 1/2: queued: `enabled: false` in `.factory/config.yml`")


def test_a_run_on_a_pr_shows_on_the_pr_and_on_its_ticket(conn, github, repo, runner, settings):
    issue = ticket(repo, label="bug")
    pr = repo.add_issue(9, "Fix search", "Closes #7", pull_request=True)
    run_id = loop.enqueue(conn, repo=REPO, task="review", ticket="7",
                          dispatch={"pr_number": 9, "task": "review"}, attempt=2)

    orchestrator.run_once(conn, github, runner, settings)

    for subject in (issue, pr):
        [line] = run_lines(subject)
        assert line.startswith("- `review` 2/2: running")
        assert line.endswith(f"run {run_id}")


def test_a_spec_issue_links_its_spec_pr(conn, github, repo, runner, settings):
    spec = ticket(repo, number=5)
    repo.add_issue(40, "Spec: search", "", pull_request=True)
    workflow = base.start(conn, repo=REPO, kind="implement-spec", subject="5")
    with conn:
        conn.execute("UPDATE workflows SET pr = 40 WHERE id = ?", (workflow,))

    orchestrator.run_once(conn, github, runner, settings)

    assert "\nSpec PR: #40\n" in status(spec)
    assert len(run_lines(spec)) == 1


def test_a_deleted_status_comment_is_made_again(conn, github, repo, runner, settings):
    issue = ticket(repo)
    orchestrator.run_once(conn, github, runner, settings)
    run = only_run(conn)
    issue.comments.clear()

    finish(conn, runner, run, explore_result(run["id"], "explore.valid-failed.json"))
    orchestrator.run_once(conn, github, runner, settings)

    line, _retry = run_lines(issue)
    assert "failed: agent_timeout" in line


def test_a_failed_edit_is_tried_again_on_the_next_pass(conn, fake, repo, runner, settings):
    issue = ticket(repo)
    broken = {"on": False}

    def transport(request):
        if broken["on"] and request.method == "PATCH" and "/issues/comments/" in request.url:
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)
    orchestrator.run_once(conn, github, runner, settings)
    run = only_run(conn)
    finish(conn, runner, run, explore_result(run["id"], "explore.valid-failed.json"))
    broken["on"] = True
    orchestrator.run_once(conn, github, runner, settings)
    assert "running" in run_lines(issue)[0]

    broken["on"] = False
    orchestrator.run_once(conn, github, runner, settings)
    assert "failed: agent_timeout" in run_lines(issue)[0]
