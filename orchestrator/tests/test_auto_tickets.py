"""Auto-tickets handling: a result becomes issues on the tracker
(orchestrator.md#auto-tickets). GitHub is in-memory (fake_github.py), the runner
is the FakeRunner; the run is queued in an implement-spec workflow by hand,
as the workflow's start (spec explore) isn't what's under test here."""

import json
import re
from pathlib import Path

import pytest
from fake_github import APP_ID, PRIVATE_KEY, FakeGitHub

from factory import db, enrolment, loop, orchestrator
from factory.github import AppAuth, GitHub, Response
from factory.runner import FakeRunner, RunFiles
from factory.workflows import base

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
REPO = "octo/widgets"
SPEC = 140


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def fake():
    fake = FakeGitHub()
    fake.add_repo(REPO).files[".factory/config.yml"] = "setup: uv sync\ncheck: uv run pytest\n"
    return fake


@pytest.fixture
def repo(fake):
    return fake.repos[REPO]


@pytest.fixture
def spec(repo):
    return repo.add_issue(SPEC, "Spec: saved searches", "Users can save searches.",
                          labels=["spec", "agent:in-progress", "bug"])


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


def ticket(title, blocked_by=(), **kw):
    return {"title": title, "what_to_build": f"Build {title.lower()}.",
            "acceptance_criteria": [f"{title} works", f"{title} is tested"],
            "blocked_by": list(blocked_by), **kw}


def decomposed(*tickets):
    return {"verdict": "decomposed", "tickets": list(tickets)}


def queue_auto_tickets(conn):
    """An auto-tickets run on the spec, in its implement-spec workflow."""
    workflow_id = base.start(conn, repo=REPO, kind="implement-spec", subject=str(SPEC))
    dispatch = json.loads((EXAMPLES / "dispatch" / "auto-tickets.valid.json").read_text())
    del dispatch["run_id"]
    dispatch.update(repo=REPO, ticket=str(SPEC))
    return loop.enqueue(conn, repo=REPO, task="auto-tickets", ticket=str(SPEC),
                        dispatch=dispatch, workflow_id=workflow_id)


def finish(conn, runner, run_id, output):
    """Dispatch the queue, finish run `run_id` with `output` and collect it."""
    loop.tick(conn, runner, limit=9)
    row = loop.get_run(conn, run_id)
    result = {"contract_version": 1, "run_id": run_id, "task": "auto-tickets",
              "outcome": "succeeded", "output": output}
    runner.finish(row["runner_run_id"], RunFiles(result_json=json.dumps(result).encode()))
    loop.tick(conn, runner, limit=9)
    return loop.get_run(conn, run_id)


def handle(conn, github, runner, settings):
    orchestrator.handle_finished(conn, github, runner, settings)


def created(repo):
    """The issues the factory made, oldest first."""
    return [i for n, i in sorted(repo.issues.items()) if n != SPEC]


def section(body, heading):
    m = re.search(rf"^## {heading}\n\n(.*?)(?=\n## |\Z)", body, re.S | re.M)
    assert m, f"no ## {heading} in:\n{body}"
    return m.group(1).strip()


def workflow_status(conn):
    return conn.execute("SELECT status FROM workflows").fetchone()["status"]


# --- decomposed ---------------------------------------------------------------


def test_decomposed_creates_one_issue_per_ticket_in_order_with_the_spec_label_and_parent(
    conn, github, repo, spec, runner, settings
):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("Store saved searches"),
                                            ticket("Show saved searches", [0])))

    handle(conn, github, runner, settings)

    first, second = created(repo)
    assert [first.title, second.title] == ["Store saved searches", "Show saved searches"]
    assert first.number < second.number
    assert first.labels == second.labels == [f"spec:{SPEC}"]
    assert section(first.body, "Parent") == f"#{SPEC}"
    assert section(first.body, "What to build") == "Build store saved searches."
    assert section(first.body, "Acceptance criteria") == (
        "- [ ] Store saved searches works\n- [ ] Store saved searches is tested")
    assert section(first.body, "Blocked by") == "None (can start immediately)"
    assert f"<!-- factory:auto-tickets run={run_id} index=0 -->" in first.body
    assert f"<!-- factory:auto-tickets run={run_id} index=1 -->" in second.body


def test_decomposed_posts_a_notice_on_the_spec_listing_the_tickets_and_keeps_the_workflow_going(
    conn, github, repo, spec, runner, settings
):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A"), ticket("B", [0])))

    handle(conn, github, runner, settings)

    first, second = created(repo)
    [notice] = spec.comments
    assert "<!-- factory:notice -->" in notice["body"]
    assert f"#{first.number}" in notice["body"] and f"#{second.number}" in notice["body"]
    assert spec.labels == ["spec", "agent:in-progress", "bug"]
    assert workflow_status(conn) == "active"
    assert loop.get_run(conn, run_id)["handled_at"] is not None


def test_blocked_by_indices_become_issue_numbers_written_as_text(
    conn, github, repo, spec, runner, settings
):
    repo.add_issue(150, "Someone else's issue")  # numbers don't follow the list
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A"), ticket("B", [0]), ticket("C", [0, 1])))

    handle(conn, github, runner, settings)

    a, b, c = [i for i in created(repo) if i.number != 150]
    assert section(b.body, "Blocked by") == f"- #{a.number}"
    assert section(c.body, "Blocked by") == f"- #{a.number}\n- #{b.number}"
    # Text only: the factory never makes GitHub's built-in blocking links.
    assert not [r for r in github._transport.requests if "dependencies" in r.url]


def test_related_issues_stay_plain_references(conn, github, repo, spec, runner, settings):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A", related=["123", "125"])))

    handle(conn, github, runner, settings)

    [a] = created(repo)
    assert section(a.body, "What to build") == (
        "Build a.\n\nRelated: #123\n\nRelated: #125")
    assert "Closes" not in a.body and "Fixes" not in a.body


def test_acceptance_criteria_are_never_ticked(conn, github, repo, spec, runner, settings):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A"), ticket("B", [0])))

    handle(conn, github, runner, settings)

    for issue in created(repo):
        assert "- [x]" not in issue.body.lower()
        assert section(issue.body, "Acceptance criteria").count("- [ ] ") == 2
    assert not [r for r in github._transport.requests if r.method == "PATCH"]


def test_after_a_crash_halfway_it_finishes_from_the_stored_result_and_skips_made_tickets(
    conn, github, fake, repo, spec, runner, settings
):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A"), ticket("B", [0]), ticket("C", [1])))
    posts = 0

    def dies_on_the_second_ticket(request):
        nonlocal posts
        if request.method == "POST" and request.url.endswith(f"/repos/{REPO}/issues"):
            posts += 1
            if posts == 2:
                raise OSError("the Mac mini lost power")
        return fake(request)

    crashing = GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock),
                      dies_on_the_second_ticket)
    handle(conn, crashing, runner, settings)
    assert [i.title for i in created(repo)] == ["A"]
    assert loop.get_run(conn, run_id)["handled_at"] is None
    # Someone closes the half-made spec's first ticket meanwhile: it still counts as made.
    created(repo)[0].state = "closed"

    handle(conn, github, runner, settings)  # a restart: a fresh pass over the stored result

    a, b, c = created(repo)
    assert [a.title, b.title, c.title] == ["A", "B", "C"]
    assert section(b.body, "Blocked by") == f"- #{a.number}"
    assert section(c.body, "Blocked by") == f"- #{b.number}"
    assert len(spec.comments) == 1
    assert loop.get_run(conn, run_id)["handled_at"] is not None


def test_another_runs_tickets_are_not_mistaken_for_this_runs(
    conn, github, repo, spec, runner, settings
):
    repo.add_issue(141, "From an earlier breakdown",
                   "<!-- factory:auto-tickets run=99 index=0 -->\n## Parent\n\n#140\n",
                   labels=[f"spec:{SPEC}"], state="closed")
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, decomposed(ticket("A")))

    handle(conn, github, runner, settings)

    assert [i.title for i in created(repo)] == ["From an earlier breakdown", "A"]


# --- needs-a-human -------------------------------------------------------------


def test_needs_a_human_creates_no_issues_posts_the_comment_and_labels_the_spec(
    conn, github, repo, spec, runner, settings
):
    run_id = queue_auto_tickets(conn)
    finish(conn, runner, run_id, {"verdict": "needs-a-human",
                                  "comment": "This needs about 20 tickets: split it in two."})

    handle(conn, github, runner, settings)

    assert created(repo) == []
    [comment] = spec.comments
    assert "This needs about 20 tickets: split it in two." in comment["body"]
    assert "`needs-a-human`" in comment["body"]
    assert spec.labels == ["spec", "bug", "needs-a-human"]
    assert workflow_status(conn) == "stopped"


# --- blockers first ------------------------------------------------------------


@pytest.mark.parametrize("blocked_by", [[1], [0]], ids=["a later ticket", "itself"])
def test_a_ticket_blocked_by_anything_but_an_earlier_ticket_makes_the_result_invalid(
    conn, github, repo, spec, runner, settings, blocked_by
):
    run_id = queue_auto_tickets(conn)
    output = decomposed(ticket("A", blocked_by), ticket("B"))

    assert finish(conn, runner, run_id, output)["outcome"] == "invalid"

    handle(conn, github, runner, settings)
    assert created(repo) == []


def test_a_cancelled_auto_tickets_run_cancels_the_workflow(
    conn, github, repo, spec, runner, settings
):
    run_id = queue_auto_tickets(conn)
    loop.cancel(conn, runner, loop.get_run(conn, run_id))

    handle(conn, github, runner, settings)

    assert workflow_status(conn) == "cancelled"
    assert "agent:in-progress" not in spec.labels
    assert created(repo) == []
