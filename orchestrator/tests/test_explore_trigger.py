"""The tracer bullet: an `agent:explore` label on an issue becomes an explore
run, and the run's findings become a comment. GitHub is in-memory
(fake_github.py) and the runner is the FakeRunner."""

import json
import logging
from pathlib import Path

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import db, enrolment, loop, orchestrator, state
from factory.contract import parse_dispatch
from factory.github import AppAuth, GitHub, GitHubError, Response
from factory.runner import FakeRunner, RunFiles

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
HEAD = "c" * 40
REPO = "octo/widgets"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


CONFIG = "setup: uv sync\ncheck: uv run pytest\n"


@pytest.fixture
def fake():
    fake = FakeGitHub()
    fake.add_repo(REPO, head_sha=HEAD).files[".factory/config.yml"] = CONFIG
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
    """The App's installations are read once, finding REPO enrolled."""
    enrolment.refresh(conn, github, factory_repo="octo/factory")


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def explore_result(run_id, **output):
    result = json.loads((EXAMPLES / "result" / "explore.valid.json").read_text())
    result["run_id"] = run_id
    result["output"].update(output)
    return result


def finish(runner, conn, run_id, result):
    rid = loop.get_run(conn, run_id)["runner_run_id"]
    runner.finish(rid, RunFiles(result_json=json.dumps(result).encode()))


# --- finding the label --------------------------------------------------------


def test_a_labelled_issue_is_queued_for_explore_and_the_label_removed(conn, github, repo, settings):
    issue = repo.add_issue(7, "Search ignores accents", 'Searching "cafe" misses "café".',
                           labels=["agent:explore", "bug"])

    orchestrator.poll(conn, github, REPO, settings)

    [row] = runs(conn)
    assert (row["repo"], row["task"], row["ticket"], row["status"]) == (REPO, "explore", "7", "queued")
    assert issue.labels == ["bug", "agent:in-progress"]
    dispatch = parse_dispatch(json.loads(row["dispatch"]))
    assert dispatch.task == "explore"
    assert dispatch.run_id == row["id"]
    assert dispatch.base_sha == HEAD
    assert dispatch.enrolment_sha == HEAD
    assert dispatch.timeout_minutes == 20
    assert dispatch.ticket_markdown == '# Search ignores accents\n\nSearching "cafe" misses "café".\n'


def test_the_row_is_inserted_before_the_label_is_removed(conn, fake, repo, settings):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    def label_removal_fails(request):
        if request.method == "DELETE":
            assert len(runs(conn)) == 1, "the row must exist before the label goes"
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, label_removal_fails, clock=fake.clock),
                    label_removal_fails)

    with pytest.raises(GitHubError):
        orchestrator.poll(conn, github, REPO, settings)

    assert len(runs(conn)) == 1
    assert issue.labels == ["agent:explore"]


def test_a_leftover_label_on_a_queued_ticket_is_removed_without_queueing_again(
    conn, github, repo, settings
):
    repo.add_issue(7, "T", labels=["agent:explore"])
    orchestrator.poll(conn, github, REPO, settings)
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    orchestrator.poll(conn, github, REPO, settings)

    assert len(runs(conn)) == 1
    assert issue.labels == []


def test_a_dispatched_ticket_isnt_queued_again(conn, github, repo, runner, settings):
    repo.add_issue(7, "T", labels=["agent:explore"])
    orchestrator.poll(conn, github, REPO, settings)
    loop.tick(conn, runner, limit=2)
    repo.add_issue(7, "T", labels=["agent:explore"])

    orchestrator.poll(conn, github, REPO, settings)

    assert [r["status"] for r in runs(conn)] == ["dispatched"]


def test_re_adding_the_label_after_a_run_finished_runs_it_again(conn, github, repo, runner, settings):
    repo.add_issue(7, "T", labels=["agent:explore"])
    orchestrator.poll(conn, github, REPO, settings)
    loop.tick(conn, runner, limit=2)
    finish(runner, conn, 1, explore_result(1))
    loop.tick(conn, runner, limit=2)
    repo.add_issue(7, "T", labels=["agent:explore"])

    orchestrator.poll(conn, github, REPO, settings)

    assert [r["status"] for r in runs(conn)] == ["done", "queued"]


def test_unlabelled_issues_are_left_alone(conn, github, repo, settings):
    issue = repo.add_issue(9, "Unlabelled", labels=["bug"])

    orchestrator.poll(conn, github, REPO, settings)

    assert runs(conn) == []
    assert issue.labels == ["bug"]
    assert issue.posts == []


def test_an_issue_with_no_body_renders_as_its_title(conn, github, repo, settings):
    repo.add_issue(7, "Just a title", None, labels=["agent:explore"])

    orchestrator.poll(conn, github, REPO, settings)

    [row] = runs(conn)
    assert json.loads(row["dispatch"])["ticket_markdown"] == "# Just a title\n"


# --- posting the findings -------------------------------------------------------


def run_to_finish(conn, github, runner, settings, result_for):
    """One pass to queue and dispatch, finish the run, one pass to collect and post."""
    orchestrator.run_once(conn, github, runner, settings)
    [row] = [r for r in runs(conn) if r["status"] == "dispatched"]
    finish(runner, conn, row["id"], result_for(row["id"]))
    orchestrator.run_once(conn, github, runner, settings)
    return row["id"]


def test_an_explore_result_is_posted_as_findings_under_the_factorys_headings(
    conn, github, repo, runner, settings
):
    repo.permissions["alice"] = "write"  # the author: easy-call goes straight on to implement
    issue = repo.add_issue(7, "Search ignores accents", "Body", author="alice")
    issue.label("agent:explore", by="alice")

    run_id = run_to_finish(conn, github, runner, settings, explore_result)

    [comment] = issue.posts
    assert comment["body"] == (
        f"<!-- factory:explore run={run_id} -->\n"
        "## Explore findings\n"
        "\n"
        "**Verdict:** `easy-call`\n"
        "\n"
        "### Summary\n"
        "\n"
        'Search should match words whatever their accents, so "cafe" finds "café".\n'
        "\n"
        "### Findings\n"
        "\n"
        'search() compares raw strings with LIKE. The claim holds: test_search.py has no '
        'accented case and "cafe" misses "café" today.\n'
        "\n"
        "### Plan\n"
        "\n"
        "1. Fold accents with unicodedata.normalize in search.normalise()\n"
        "2. Store a folded column and search it\n"
        "3. Add a test for accented and unaccented queries\n"
        "\n"
        "### Files\n"
        "\n"
        "- `app/search.py`\n"
        "- `tests/test_search.py`\n"
        "\n"
        "---\n"
        f"Factory run {run_id} · [runner log](https://runner.invalid/{REPO}/runs/1001)\n"
    )


def test_questions_and_duplicate_of_get_their_own_headings_and_empty_lists_say_none(
    conn, github, repo, runner, settings
):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    def result(run_id):
        return explore_result(
            run_id, verdict="needs-a-human", plan=[], files=[], questions=["Which locale?", "Why?"]
        )

    run_to_finish(conn, github, runner, settings, result)

    body = issue.posts[0]["body"]
    assert "**Verdict:** `needs-a-human`" in body
    assert "### Plan\n\nNone.\n\n### Files\n\nNone.\n" in body
    assert "### Questions\n\n1. Which locale?\n2. Why?\n" in body


def test_a_duplicate_names_the_issue_it_duplicates(conn, github, repo, runner, settings):
    repo.add_issue(3, "Older")
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    run_to_finish(
        conn, github, runner, settings,
        lambda run_id: explore_result(run_id, verdict="duplicate", duplicate_of="3"),
    )

    assert "### Duplicate of\n\n#3\n" in issue.posts[0]["body"]


def test_findings_are_posted_once_however_many_passes_follow(conn, github, repo, runner, settings):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])
    run_to_finish(conn, github, runner, settings, explore_result)

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert len(issue.posts) == 1


@pytest.mark.parametrize(
    "result",
    [
        lambda run_id: {**json.loads((EXAMPLES / "result" / "explore.valid-failed.json").read_text()),
                        "run_id": run_id},
        lambda run_id: {**explore_result(run_id), "output": {"verdict": "easy-call"}},
    ],
    ids=["failed", "invalid"],
)
def test_a_failed_or_invalid_run_posts_nothing(conn, github, repo, runner, settings, result):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    run_to_finish(conn, github, runner, settings, result)

    assert issue.posts == []


@pytest.mark.parametrize(
    "secret",
    ["sk-ant-oat01-abcdefghijklmnop", "ghs_" + "a" * 36, "github_pat_" + "a" * 40,
     "-----BEGIN RSA PRIVATE KEY-----"],
)
def test_a_result_whose_prose_holds_a_token_is_invalid_and_nothing_is_posted(
    conn, github, repo, runner, settings, secret
):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])

    run_id = run_to_finish(
        conn, github, runner, settings,
        lambda run_id: explore_result(run_id, findings=f"The token is {secret} here."),
    )

    assert loop.get_run(conn, run_id)["outcome"] == "invalid"
    assert issue.posts == []


def test_a_failed_post_is_tried_again_on_the_next_pass(conn, fake, repo, runner, settings):
    issue = repo.add_issue(7, "T", labels=["agent:explore"])
    broken = {"on": False}

    def transport(request):
        if broken["on"] and request.method == "POST" and request.url.endswith("/comments"):
            return Response(502, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)
    orchestrator.run_once(conn, github, runner, settings)
    finish(runner, conn, 1, explore_result(1))
    broken["on"] = True
    orchestrator.run_once(conn, github, runner, settings)
    assert issue.posts == []

    broken["on"] = False
    orchestrator.run_once(conn, github, runner, settings)

    assert len(issue.posts) == 1


def test_one_repo_failing_doesnt_stop_the_others(conn, fake, github, runner, settings):
    fake.add_repo("octo/gadgets").files[".factory/config.yml"] = CONFIG
    issue = fake.repos["octo/gadgets"].add_issue(3, "T", labels=["agent:explore"])
    fake.repos[REPO].add_issue(7, "T", labels=["agent:explore"])
    enrolment.refresh(conn, github, factory_repo="octo/factory")

    def transport(request):
        if f"/repos/{REPO}/" in request.url:
            return Response(500, {}, b"")
        return fake(request)

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, transport, clock=fake.clock), transport)

    orchestrator.run_once(conn, github, runner, settings)

    assert [r["repo"] for r in runs(conn)] == ["octo/gadgets"]
    assert issue.labels == ["agent:in-progress"]
    assert [repo for repo, _ in state.last_polls(conn)] == ["octo/gadgets"]  # for `factory status`


def test_no_token_reaches_the_logs_over_a_whole_run(conn, github, fake, repo, runner, settings, caplog):
    caplog.set_level(logging.DEBUG)
    repo.add_issue(7, "T", labels=["agent:explore"])

    run_to_finish(conn, github, runner, settings, explore_result)

    assert fake.tokens
    assert all(token not in caplog.text for token in fake.tokens)
