"""Review's findings inline, findings-only reviews and fork PRs (orchestrator.md#review).

Findings on a changed line go inline as one `COMMENT` review; the rest go into
the verdict comment. A PR the factory didn't open gets a findings-only review
that leaves the PR alone, and `agent:review` on a fork PR queues nothing."""

from fake_github import APP_ID, APP_LOGIN, PRIVATE_KEY, user_id

from factory import loop, orchestrator, review
from factory.brakes import Limits
from factory.github import AppAuth, GitHub, Response
from test_implement_verdicts import (  # noqa: F401 (fixtures)
    REPO, conn, enrolled, fake, github, remote, repo, runner, runs, settings,
)
from test_review_verdicts import finish_review, review_dispatched, review_result, verdicts

# src/search.py: lines 1-2 are context, 3-4 added, 5 context.
SEARCH_PATCH = "@@ -1,3 +1,5 @@\n import re\n \n+def fold(s):\n+    return s\n # end\n"


def finding(path="src/search.py", line=3, title="Name the helper", axis="standards"):
    return {"axis": axis, "title": title, "path": path, "line": line,
            "body": f"Something to say about {path}:{line}."}


def app_reviews(pr):
    return [r for r in pr["reviews"] if r["user"]["login"] == APP_LOGIN]


# --- inline findings ------------------------------------------------------------------------


def test_findings_on_changed_lines_go_inline_as_one_comment_review_and_the_rest_in_the_verdict(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    pr["files"] = [{"filename": "src/search.py", "patch": SEARCH_PATCH}]
    on_diff = finding(line=4, title="Name the helper")
    context = finding(line=2, title="Blank line on context")  # in the hunk, not changed
    elsewhere = finding(path="src/other.py", line=1, title="Other file", axis="spec")

    finish_review(conn, github, runner, settings, row,
                  review_result(row, comments=[on_diff, context, elsewhere]))

    [inline] = app_reviews(pr)
    assert inline["event"] == "COMMENT"
    assert [(c["path"], c["line"], c["side"]) for c in inline["comments"]] == [
        ("src/search.py", 4, "RIGHT")]
    assert "Name the helper" in inline["comments"][0]["body"]
    assert "Something to say about src/search.py:4." in inline["comments"][0]["body"]
    [verdict] = verdicts(repo.issues[pr["number"]])
    body = verdict["body"]
    assert "Blank line on context" in body and "Other file" in body
    assert "Name the helper" not in body
    assert "1 finding is inline" in body
    assert pr["draft"] is False


def test_no_inline_review_when_no_finding_is_on_a_changed_line(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    pr["files"] = [{"filename": "src/search.py", "patch": SEARCH_PATCH}]

    finish_review(conn, github, runner, settings, row,
                  review_result(row, comments=[finding(path="README.md", line=1)]))

    assert app_reviews(pr) == []
    [verdict] = verdicts(repo.issues[pr["number"]])
    assert "README.md:1" in verdict["body"] and "inline" not in verdict["body"]


def test_at_most_25_findings_go_inline_and_any_beyond_into_the_verdict():
    # The output's own maxItems is 25, so only `place` can be handed more.
    patch = "@@ -0,0 +1,30 @@\n" + "".join(f"+line {n}\n" for n in range(1, 31))
    files = [{"filename": "src/big.py", "patch": patch}]
    comments = [finding(path="src/big.py", line=n, title=f"Finding {n}") for n in range(1, 28)]
    comments.insert(3, finding(path="src/gone.py", line=1, title="Off the diff"))

    inline, rest = review.place(comments, files)

    assert [c["line"] for c in inline] == list(range(1, 26))
    assert [c["title"] for c in rest] == ["Off the diff", "Finding 26", "Finding 27"]


def test_a_review_github_refuses_puts_every_finding_in_the_verdict_and_the_run_still_counts(
    conn, github, fake, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    pr["files"] = [{"filename": "src/search.py", "patch": SEARCH_PATCH}]

    def refusing(req):  # e.g. the branch moved between reading the diff and posting
        if req.method == "POST" and req.url.endswith("/reviews"):
            return Response(422, {}, b'{"message": "line must be part of the diff"}')
        return fake(req)

    refused = GitHub(AppAuth(APP_ID, PRIVATE_KEY, refusing, clock=fake.clock), refusing,
                     git_url=lambda _: remote.url)
    run = finish_review(conn, refused, runner, settings, row,
                        review_result(row, comments=[finding(line=4, title="Name the helper")]))

    assert run["outcome"] == "succeeded"
    assert app_reviews(pr) == []
    [verdict] = verdicts(repo.issues[pr["number"]])
    assert "Name the helper" in verdict["body"] and "inline" not in verdict["body"]
    assert pr["draft"] is False


def test_inline_findings_are_posted_once_even_if_the_orchestrator_stops_after_posting_them(
    conn, github, repo, remote, runner, settings
):
    _, pr, row = review_dispatched(conn, github, repo, remote, runner, settings)
    pr["files"] = [{"filename": "src/search.py", "patch": SEARCH_PATCH}]
    finish_review(conn, github, runner, settings, row,
                  review_result(row, comments=[finding(line=3)]))
    conn.execute("UPDATE runs SET handled_at = NULL WHERE id = ?", (row["id"],))

    orchestrator.run_once(conn, github, runner, settings)

    assert len(app_reviews(pr)) == 1
    [verdict] = verdicts(repo.issues[pr["number"]])
    assert "1 finding is inline" in verdict["body"]


# --- findings-only: a PR the factory didn't open ----------------------------------------------

HEAD = "a" * 40
BASE = "b" * 40


def human_pr(fake, repo, *, closes=(3,), files=None):
    """Alice's own PR #40, which closes #3, labelled `agent:review` by alice."""
    repo.add_issue(3, "Search ignores accents", "## Acceptance criteria\n\n- [ ] folds")
    pr = fake.add_pull(REPO, 40, "Tidy search", "alice/tidy", author="alice", body="Tidies it.",
                       closes=list(closes), head_sha=HEAD, base_sha=BASE,
                       files=files or [{"filename": "src/search.py", "patch": SEARCH_PATCH}])
    repo.issues[40].label("agent:review", by="alice")
    return pr


def findings_only_dispatched(conn, github, fake, repo, runner, settings, **kw):
    pr = human_pr(fake, repo, **kw)
    orchestrator.run_once(conn, github, runner, settings)
    [row] = runs(conn)
    assert row["status"] == "dispatched"
    return pr, row


def test_agent_review_on_a_pr_the_factory_didnt_open_queues_a_findings_only_review(
    conn, github, fake, repo, runner, settings
):
    pr = human_pr(fake, repo)

    orchestrator.run_once(conn, github, runner, settings)

    [row] = runs(conn)
    assert (row["task"], row["ticket"], row["status"], row["triggered_by"]) == (
        "review", "40", "dispatched", "alice")
    sent = runner.dispatched[-1][1]
    assert sent["findings_only"] is True
    assert sent["pr_number"] == 40
    assert (sent["branch"], sent["base_sha"], sent["pr_base_sha"]) == ("alice/tidy", HEAD, BASE)
    assert sent["triggered_by"] == {"login": "alice", "id": user_id("alice")}
    assert sent["ticket_markdown"].startswith("# PR #40: Tidy search\n\nTidies it.")
    assert "## Reviewed against: #3 Search ignores accents" in sent["ticket_markdown"]
    pr_issue = repo.issues[40]
    assert "agent:review" not in pr_issue.labels
    assert "agent:in-progress" in pr_issue.labels
    assert pr["draft"] is False


def test_a_findings_only_review_starts_from_the_prs_head_at_dispatch_time(
    conn, github, fake, repo, runner, settings
):
    pr = human_pr(fake, repo)
    paused = orchestrator.Settings(git_dir=settings.git_dir,
                                   limits=Limits(max_in_flight=0))
    orchestrator.run_once(conn, github, runner, paused)
    [row] = runs(conn)
    assert row["status"] == "queued"
    pr["head_sha"] = "c" * 40  # alice pushed while it waited

    orchestrator.run_once(conn, github, runner, settings)

    assert runner.dispatched[-1][1]["base_sha"] == "c" * 40


def test_a_findings_only_result_posts_findings_and_verdict_and_leaves_the_pr_alone(
    conn, github, fake, repo, runner, settings
):
    pr, row = findings_only_dispatched(conn, github, fake, repo, runner, settings)
    pr["draft"] = True
    comments = [finding(line=3, title="Name the helper"),
                finding(path="src/other.py", line=9, title="Would change other.py", axis="spec")]

    run = finish_review(conn, github, runner, settings, row, review_result(
        row, standards="changes-requested", comments=comments))

    assert run["outcome"] == "succeeded"
    [inline] = app_reviews(pr)
    assert inline["event"] == "COMMENT" and inline["commit_id"] == HEAD
    assert [c["line"] for c in inline["comments"]] == [3]
    pr_issue = repo.issues[40]
    [verdict] = verdicts(pr_issue)
    assert "Would change other.py" in verdict["body"]
    assert "### Standards: `changes-requested`" in verdict["body"]
    assert pr["draft"] is True
    assert pr["requested_reviewers"] == []
    assert pr_issue.labels == []
    [workflow] = conn.execute("SELECT * FROM workflows").fetchall()
    assert (workflow["kind"], workflow["subject"], workflow["status"]) == (
        "review-pr", "40", "done")


def test_a_findings_only_review_that_leaves_commits_is_invalid(
    conn, github, fake, repo, remote, runner, settings
):
    pr, row = findings_only_dispatched(conn, github, fake, repo, runner, settings)
    remote.branch("alice/tidy")
    remote.commit({"src/search.py": "fold()\n"}, "review: standards: a fix it mustn't make")
    bundle = remote.bundle("alice/tidy")

    run = finish_review(conn, github, runner, settings, row, review_result(row), bundle)

    assert run["outcome"] == "invalid"
    assert "findings-only" in run["validation_error"]
    assert remote.remote_branch("alice/tidy") is None


def test_a_findings_only_review_out_of_attempts_blocks_the_pr(
    conn, github, fake, repo, runner, settings
):
    _, row = findings_only_dispatched(conn, github, fake, repo, runner, settings)
    failed = {"contract_version": 1, "run_id": row["id"], "task": "review", "outcome": "failed",
              "failure": "agent_error"}
    finish_review(conn, github, runner, settings, row, failed)
    orchestrator.run_once(conn, github, runner, settings)
    [again] = [r for r in runs(conn) if r["id"] > row["id"]]
    assert again["attempt"] == 2 and again["status"] == "dispatched"

    finish_review(conn, github, runner, settings, again, {**failed, "run_id": again["id"]})

    pr_issue = repo.issues[40]
    assert "agent:blocked" in pr_issue.labels and "agent:in-progress" not in pr_issue.labels
    assert "@alice" in pr_issue.posts[-1]["body"] and "agent:review" in pr_issue.posts[-1]["body"]
    assert conn.execute("SELECT status FROM workflows").fetchone()["status"] == "stopped"


def test_closing_the_pr_cancels_its_findings_only_review(conn, github, fake, repo, runner, settings):
    _, row = findings_only_dispatched(conn, github, fake, repo, runner, settings)
    repo.issues[40].state = "closed"
    repo.pulls[40]["state"] = "closed"

    orchestrator.run_once(conn, github, runner, settings)
    orchestrator.run_once(conn, github, runner, settings)

    assert loop.get_run(conn, row["id"])["outcome"] == "cancelled"
    assert conn.execute("SELECT status FROM workflows").fetchone()["status"] == "cancelled"
    assert "agent:in-progress" not in repo.issues[40].labels


# --- fork PRs ----------------------------------------------------------------------------------


def test_agent_review_on_a_fork_pr_posts_a_notice_and_queues_nothing(
    conn, github, fake, repo, runner, settings
):
    fake.add_pull(REPO, 40, "Tidy search", "tidy", author="mallory", head_repo="mallory/widgets")
    pr_issue = repo.issues[40]
    pr_issue.label("agent:review", by="alice")

    orchestrator.run_once(conn, github, runner, settings)

    assert runs(conn) == []
    [notice] = pr_issue.posts
    assert "<!-- factory:notice -->" in notice["body"]
    assert "@alice" in notice["body"] and "fork" in notice["body"]
    assert pr_issue.labels == []
