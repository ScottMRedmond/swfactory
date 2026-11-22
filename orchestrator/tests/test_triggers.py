"""Checking trigger labels before anything is queued (orchestrator.md#triggers).

Every trigger label is checked; a rejected one is removed with a notice that
@mentions whoever added it and says what to do instead. GitHub is in-memory
(fake_github.py); assertions are on the fake's issues and on the runs table.
"""

import json

import pytest
from fake_github import APP_ID, APP_LOGIN, PRIVATE_KEY, FakeGitHub, enrol

from factory import db, enrolment, loop, orchestrator
from factory.github import AppAuth, GitHub

REPO = "octo/widgets"
NOTICE = "<!-- factory:notice -->"


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    enrol(fake.add_repo(REPO))
    return fake


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


@pytest.fixture
def github(fake):
    return GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)


@pytest.fixture
def settings():
    return orchestrator.Settings()


@pytest.fixture(autouse=True)
def enrolled(conn, github):
    enrolment.refresh(conn, github, factory_repo="octo/factory")


def poll(conn, github, settings):
    orchestrator.poll(conn, github, REPO, settings)


def runs(conn):
    return conn.execute("SELECT * FROM runs ORDER BY id").fetchall()


def notices(issue):
    return [c["body"] for c in issue.comments if NOTICE in c["body"]]


def test_triggered_by_is_whoever_last_added_the_label(conn, github, repo, settings):
    issue = repo.add_issue(7, "T")
    issue.label("agent:explore", by="bob", at="2026-11-20T09:00:00Z")
    issue.add_event("unlabeled", "bob", "2026-11-20T09:01:00Z", label={"name": "agent:explore"})
    issue.label("bug", by="carol", at="2026-11-20T09:30:00Z")
    issue.label("agent:explore", by="alice", at="2026-11-20T10:00:00Z")

    poll(conn, github, settings)

    [row] = runs(conn)
    assert row["triggered_by"] == "alice"


def test_a_trigger_label_the_app_added_itself_is_removed_without_a_run(conn, github, repo, settings):
    issue = repo.add_issue(7, "T", labels=["bug"]).label("agent:explore", by=APP_LOGIN)

    poll(conn, github, settings)

    assert runs(conn) == []
    assert issue.labels == ["bug"]
    assert issue.comments == []


@pytest.mark.parametrize("pull_request", [False, True], ids=["issue", "pr"])
def test_a_trigger_on_a_closed_issue_or_pr_is_removed_with_reopen_it_first(
    conn, github, repo, settings, pull_request
):
    label = "agent:review" if pull_request else "agent:explore"
    issue = repo.add_issue(7, "Old", state="closed", pull_request=pull_request).label(label, by="alice")

    orchestrator.handle_triggers(conn, github, REPO, [issue.json(REPO)], settings)

    assert runs(conn) == []
    assert issue.labels == []
    [notice] = notices(issue)
    assert notice.startswith(NOTICE + "\n@alice ")
    assert "Reopen it first." in notice


def test_conflicting_trigger_labels_are_all_removed_and_nothing_is_guessed(
    conn, github, repo, settings
):
    issue = repo.add_issue(7, "T", labels=["bug"])
    issue.label("agent:explore", by="alice").label("agent:implement", by="bob")

    poll(conn, github, settings)

    assert runs(conn) == []
    assert issue.labels == ["bug"]
    [notice] = notices(issue)
    assert notice.startswith(NOTICE + "\n@alice @bob ")
    assert "`agent:explore`" in notice and "`agent:implement`" in notice
    assert "`agent:review` goes on a pull request" in notice  # says which label goes where


@pytest.mark.parametrize(
    "label, pull_request, others, says",
    [
        ("agent:review", False, [], "`agent:review` goes on a pull request"),
        ("agent:implement", True, [], "`agent:implement` goes on a ticket"),
        ("agent:explore", True, [], "`agent:explore` goes on a ticket"),
        ("agent:implement-spec", True, [], "`agent:implement-spec` goes on a spec issue"),
        ("agent:implement-spec", False, ["spec:3"], "goes on the spec issue, #3"),
        ("agent:explore", False, ["spec:3"], "skip explore"),
        ("agent:explore", False, ["spec"], "explored as part of `agent:implement-spec`"),
        ("agent:implement", False, ["spec:3", "agent:built"], "fix it on the spec's pull request"),
    ],
    ids=["review-on-issue", "implement-on-pr", "explore-on-pr", "implement-spec-on-pr",
         "implement-spec-on-spec-ticket", "explore-on-spec-ticket", "explore-on-spec",
         "implement-on-built-ticket"],
)
def test_a_label_on_the_wrong_kind_of_issue_is_removed_with_a_notice_saying_where_it_goes(
    conn, github, repo, settings, label, pull_request, others, says
):
    issue = repo.add_issue(7, "T", labels=others, pull_request=pull_request).label(label, by="alice")

    poll(conn, github, settings)

    assert runs(conn) == []
    assert issue.labels == others
    [notice] = notices(issue)
    assert notice.startswith(NOTICE + "\n@alice ")
    assert says in notice


@pytest.mark.parametrize(
    "tasks, label, pull_request, others, says",
    [
        ("[explore, implement]", "agent:review", True, [],
         "`agent:review` isn't enabled for this repo"),
        ("[explore]", "agent:explore", False, [],  # explore alone is fine...
         None),
        ("[explore, review]", "agent:implement", False, [],
         "`agent:implement` isn't enabled for this repo"),
        ("[review]", "agent:explore", False, [],
         "`agent:explore` isn't enabled for this repo"),
        ("[explore, implement, review]", "agent:implement-spec", False, ["spec"],
         "it needs `auto-tickets`, which isn't enabled for this repo"),
    ],
    ids=["review", "explore-enabled", "implement", "explore", "implement-spec-needs-auto-tickets"],
)
def test_a_trigger_for_a_task_the_enrolment_doesnt_enable_is_removed_with_a_notice(
    conn, fake, github, repo, settings, tasks, label, pull_request, others, says
):
    enrol(repo, f"setup: uv sync\ncheck: uv run pytest\ntasks: {tasks}\n")
    enrolment.refresh(conn, github, factory_repo="octo/factory")
    issue = repo.add_issue(7, "T", labels=others, pull_request=pull_request).label(label, by="alice")

    poll(conn, github, settings)

    assert issue.labels == others
    if says is None:
        assert len(runs(conn)) == 1 and notices(issue) == []
        return
    assert runs(conn) == []
    [notice] = notices(issue)
    assert notice.startswith(NOTICE + "\n@alice ")
    assert says in notice


# --- busy issues ----------------------------------------------------------------

BUSY = "A run is already in progress; re-add the label once it finishes."


def test_a_label_re_added_while_its_run_is_active_is_removed_with_a_notice(conn, github, repo, settings):
    issue = repo.add_issue(7, "T").label("agent:explore", by="alice", at="2026-01-01T00:00:00Z")
    poll(conn, github, settings)
    issue.label("agent:explore", by="bob", at="2099-01-01T00:00:00Z")  # after the row was queued

    poll(conn, github, settings)

    assert len(runs(conn)) == 1
    assert issue.labels == []
    [notice] = notices(issue)
    assert notice == f"{NOTICE}\n@bob {BUSY}\n"


def test_a_label_left_over_from_a_crash_is_removed_quietly(conn, github, repo, settings):
    issue = repo.add_issue(7, "T").label("agent:explore", by="alice", at="2026-01-01T00:00:00Z")
    poll(conn, github, settings)
    issue.labels.append("agent:explore")  # as if the orchestrator died before removing it

    poll(conn, github, settings)

    assert len(runs(conn)) == 1
    assert issue.labels == []
    assert notices(issue) == []


def test_any_other_trigger_on_a_busy_issue_is_removed_with_a_notice(conn, github, repo, settings):
    issue = repo.add_issue(7, "T").label("agent:explore", by="alice", at="2026-01-01T00:00:00Z")
    poll(conn, github, settings)
    issue.label("agent:implement", by="alice", at="2026-01-01T00:00:00Z")

    poll(conn, github, settings)

    assert [r["task"] for r in runs(conn)] == ["explore"]
    assert issue.labels == []
    assert notices(issue) == [f"{NOTICE}\n@alice {BUSY}\n"]


def test_a_pr_with_an_active_run_on_it_is_busy(conn, github, repo, settings):
    loop.enqueue(conn, repo=REPO, task="review", ticket="7",
                 dispatch={"task": "review", "pr_number": 9})
    pr = repo.add_issue(9, "PR", pull_request=True).label("agent:review", by="alice",
                                                          at="2099-01-01T00:00:00Z")

    poll(conn, github, settings)

    assert len(runs(conn)) == 1
    assert pr.labels == []
    assert notices(pr) == [f"{NOTICE}\n@alice {BUSY}\n"]


def test_a_finished_run_leaves_the_issue_free(conn, github, repo, settings):
    issue = repo.add_issue(7, "T").label("agent:explore", by="alice", at="2026-01-01T00:00:00Z")
    poll(conn, github, settings)
    conn.execute("UPDATE runs SET status = 'done', outcome = 'succeeded'")
    issue.label("agent:explore", by="alice", at="2099-01-01T00:00:00Z")

    poll(conn, github, settings)

    assert [r["status"] for r in runs(conn)] == ["done", "queued"]
    assert runs(conn)[1]["triggered_by"] == "alice"
    assert notices(issue) == []
