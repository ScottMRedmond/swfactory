"""The Actions runner adapter, against a recorded fake of GitHub's REST API.

The responses below follow the shapes GitHub documents for the Actions API
(and, for dispatch, the `return_run_details` answer recorded in the spike, #2).
"""

import io
import json
import zipfile

import pytest

from factory.actions_runner import ActionsRunner
from factory.github import GitHubError, Request
from factory.github import Response as HttpResponse
from factory.runner import RunFiles, RunnerUnavailable, RunRefused, RunState

REPO = "octo/widgets"
DISPATCH = {"run_id": 7, "task": "smoke", "repo": REPO}
BILLING = (
    "The job was not started because recent account payments have failed or your "
    "spending limit needs to be increased. Please check the 'Billing & plans' section "
    "of your settings"
)
TIMED_OUT = "The job has exceeded the maximum execution time of 30m0s"


def ok(body=None, status=200, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
    return HttpResponse(status=status, headers=headers or {}, body=raw)


class FakeGitHub:
    """Answers requests from a table of (method, url) -> response, and records them."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def on(self, method, path, response):
        url = path if path.startswith("https://") else "https://api.github.com" + path
        self.routes[(method, url)] = response
        return self

    def __call__(self, req):
        method, url = req.method, req.url
        self.requests.append({"method": method, "url": url, "headers": req.headers, "body": req.body})
        response = self.routes.get((method, url))
        if response is None:
            return ok({"message": "Not Found"}, status=404)
        if isinstance(response, Exception):
            raise response
        return response

    def sent(self, method, url_suffix):
        return [r for r in self.requests if r["method"] == method and r["url"].endswith(url_suffix)]


class Tokens:
    def __init__(self):
        self.asked = []

    def token(self, repo):
        self.asked.append(repo)
        return f"tok-{repo}"


@pytest.fixture
def github():
    return FakeGitHub().on("GET", f"/repos/{REPO}", ok({"default_branch": "trunk"}))


@pytest.fixture
def runner(github):
    return ActionsRunner(github, Tokens())


def run_json(status="completed", conclusion="success", run_id=555):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }


def job(job_id=91, steps=None, conclusion="success"):
    if steps is None:
        steps = [{"name": "Set up job", "status": "completed", "conclusion": "success", "number": 1}]
    return {"id": job_id, "name": "run / run", "status": "completed", "conclusion": conclusion, "steps": steps}


def finished_run(github, run_id=555, conclusion="success", jobs=None, annotations=None):
    github.on("GET", f"/repos/{REPO}/actions/runs/{run_id}", ok(run_json(conclusion=conclusion, run_id=run_id)))
    jobs = jobs if jobs is not None else [job()]
    github.on(
        "GET",
        f"/repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
        ok({"total_count": len(jobs), "jobs": jobs}),
    )
    for j in jobs:
        github.on(
            "GET",
            f"/repos/{REPO}/check-runs/{j['id']}/annotations?per_page=100",
            ok([{"message": m, "annotation_level": "failure"} for m in (annotations or {}).get(j["id"], [])]),
        )


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def artifact_on(github, run_id=555, files=None, expired=False, artifacts=None):
    if artifacts is None:
        artifacts = [
            {
                "id": 31,
                "name": "factory-run",
                "expired": expired,
                "archive_download_url": f"https://api.github.com/repos/{REPO}/actions/artifacts/31/zip",
            }
        ]
    github.on(
        "GET",
        f"/repos/{REPO}/actions/runs/{run_id}/artifacts?name=factory-run",
        ok({"total_count": len(artifacts), "artifacts": artifacts}),
    )
    blob = "https://blob.example/factory-run.zip?sig=abc"
    github.on("GET", f"/repos/{REPO}/actions/artifacts/31/zip", ok(b"", status=302, headers={"Location": blob}))
    github.on("GET", blob, ok(zip_of(files or {})))


# --- dispatch -----------------------------------------------------------------


def test_dispatch_starts_the_caller_workflow_and_returns_the_actions_run_id(github, runner):
    github.on(
        "POST",
        f"/repos/{REPO}/actions/workflows/factory.yml/dispatches",
        ok({"workflow_run_id": 555, "run_url": "https://api.github.com/...", "html_url": "https://github.com/..."}),
    )

    assert runner.dispatch(REPO, DISPATCH) == "555"

    [sent] = github.sent("POST", "/actions/workflows/factory.yml/dispatches")
    assert json.loads(sent["body"]) == {
        "ref": "trunk",
        "inputs": {"dispatch": json.dumps(DISPATCH)},
        "return_run_details": True,
    }
    assert sent["headers"]["X-GitHub-Api-Version"] == "2026-03-10"
    assert sent["headers"]["Authorization"] == f"Bearer tok-{REPO}"
    assert sent["headers"]["Accept"] == "application/vnd.github+json"


def test_the_default_branch_is_looked_up_once_per_repo(github, runner):
    github.on("POST", f"/repos/{REPO}/actions/workflows/factory.yml/dispatches", ok({"workflow_run_id": 1}))
    runner.dispatch(REPO, DISPATCH)
    runner.dispatch(REPO, DISPATCH)
    assert len(github.sent("GET", f"/repos/{REPO}")) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500, 502, 503])
def test_a_rejected_dispatch_is_runner_unavailable(github, runner, status):
    github.on(
        "POST",
        f"/repos/{REPO}/actions/workflows/factory.yml/dispatches",
        ok({"message": "nope"}, status=status),
    )
    with pytest.raises(RunnerUnavailable):
        runner.dispatch(REPO, DISPATCH)


def test_an_outage_during_dispatch_is_runner_unavailable(github, runner):
    github.on("GET", f"/repos/{REPO}", ConnectionError("connection refused"))
    with pytest.raises(RunnerUnavailable):
        runner.dispatch(REPO, DISPATCH)


def test_a_dispatch_answer_without_a_run_id_is_runner_unavailable(github, runner):
    github.on("POST", f"/repos/{REPO}/actions/workflows/factory.yml/dispatches", ok(b"", status=204))
    with pytest.raises(RunnerUnavailable):
        runner.dispatch(REPO, DISPATCH)


# --- state --------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_a_run_not_yet_completed_is_running(github, runner, status):
    github.on("GET", f"/repos/{REPO}/actions/runs/555", ok(run_json(status=status, conclusion=None)))
    assert runner.state(REPO, "555") is RunState.RUNNING


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
def test_a_completed_run_is_finished(github, runner, conclusion):
    finished_run(github, conclusion=conclusion)
    assert runner.state(REPO, "555") is RunState.FINISHED


def test_a_run_actions_doesnt_know_is_missing(github, runner):
    assert runner.state(REPO, "555") is RunState.MISSING


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_an_unanswered_state_request_is_runner_unavailable(github, runner, status):
    github.on("GET", f"/repos/{REPO}/actions/runs/555", ok({"message": "x"}, status=status))
    with pytest.raises(RunnerUnavailable):
        runner.state(REPO, "555")


def test_an_outage_during_state_is_runner_unavailable_not_missing(github, runner):
    github.on("GET", f"/repos/{REPO}/actions/runs/555", TimeoutError("timed out"))
    with pytest.raises(RunnerUnavailable):
        runner.state(REPO, "555")


def test_a_run_cancelled_by_the_job_timeout_is_timed_out(github, runner):
    finished_run(github, conclusion="cancelled", annotations={91: [TIMED_OUT]})
    assert runner.state(REPO, "555") is RunState.TIMED_OUT


def test_a_run_refused_for_billing_is_reported_as_refused(github, runner, caplog):
    finished_run(github, conclusion="failure", jobs=[job(steps=[], conclusion="failure")], annotations={91: [BILLING]})

    with pytest.raises(RunRefused):
        runner.state(REPO, "555")
    assert issubclass(RunRefused, RunnerUnavailable)


def test_the_first_job_that_ran_no_steps_is_logged_in_full_once(github, runner, caplog):
    finished_run(github, conclusion="failure", jobs=[job(steps=[], conclusion="failure")])

    with caplog.at_level("WARNING"):
        assert runner.state(REPO, "555") is RunState.FINISHED
        runner.state(REPO, "555")
    logged = [r for r in caplog.records if "ran no steps" in r.getMessage()]
    assert len(logged) == 1
    assert '"conclusion": "failure"' in logged[0].getMessage()
    assert '"steps": []' in logged[0].getMessage()


# --- files --------------------------------------------------------------------


def test_files_are_the_runs_artifact(github, runner):
    finished_run(github)
    artifact_on(
        github,
        files={
            "result.json": b'{"run_id": 7}',
            "run.bundle": b"BUNDLE",
            "evidence/home.png": b"PNG1",
            "evidence/after.png": b"PNG2",
        },
    )

    assert runner.files(REPO, "555") == RunFiles(
        result_json=b'{"run_id": 7}',
        bundle=b"BUNDLE",
        screenshots={"home.png": b"PNG1", "after.png": b"PNG2"},
    )


def test_the_artifact_download_follows_the_redirect_without_the_token(github, runner):
    finished_run(github)
    artifact_on(github, files={"result.json": b"{}"})

    runner.files(REPO, "555")

    [blob] = [r for r in github.requests if r["url"].startswith("https://blob.example/")]
    assert "Authorization" not in blob["headers"]


def test_a_run_with_only_a_result_has_no_bundle_or_screenshots(github, runner):
    finished_run(github)
    artifact_on(github, files={"result.json": b"{}"})
    assert runner.files(REPO, "555") == RunFiles(result_json=b"{}")


def test_a_run_without_an_artifact_has_no_files(github, runner):
    finished_run(github)
    artifact_on(github, artifacts=[])
    assert runner.files(REPO, "555") is None


def test_an_expired_artifact_is_no_files(github, runner):
    finished_run(github)
    artifact_on(github, expired=True, files={"result.json": b"{}"})
    assert runner.files(REPO, "555") is None


def test_an_artifact_gone_by_download_time_is_no_files(github, runner):
    finished_run(github)
    artifact_on(github, files={"result.json": b"{}"})
    github.on("GET", f"/repos/{REPO}/actions/artifacts/31/zip", ok({"message": "Artifact has expired"}, status=410))
    assert runner.files(REPO, "555") is None


def test_an_outage_while_fetching_files_is_runner_unavailable(github, runner):
    finished_run(github)
    artifact_on(github, files={"result.json": b"{}"})
    github.on("GET", "https://blob.example/factory-run.zip?sig=abc", ok(b"", status=503))
    with pytest.raises(RunnerUnavailable):
        runner.files(REPO, "555")


def test_a_corrupt_artifact_has_no_files(github, runner):
    finished_run(github)
    artifact_on(github, files={"result.json": b"{}"})
    github.on("GET", "https://blob.example/factory-run.zip?sig=abc", ok(b"not a zip"))
    assert runner.files(REPO, "555") is None


# --- log link and cancel ------------------------------------------------------


def test_the_log_link_is_the_actions_run_page(runner):
    assert runner.log_link(REPO, "555") == f"https://github.com/{REPO}/actions/runs/555"


def test_cancel_asks_actions_to_cancel_the_run(github, runner):
    github.on("POST", f"/repos/{REPO}/actions/runs/555/cancel", ok({}, status=202))
    runner.cancel(REPO, "555")
    assert len(github.sent("POST", "/actions/runs/555/cancel")) == 1


@pytest.mark.parametrize("status", [404, 409])
def test_cancelling_a_finished_or_unknown_run_is_a_no_op(github, runner, status):
    github.on("POST", f"/repos/{REPO}/actions/runs/555/cancel", ok({"message": "x"}, status=status))
    runner.cancel(REPO, "555")


def test_cancel_during_an_outage_is_runner_unavailable(github, runner):
    github.on("POST", f"/repos/{REPO}/actions/runs/555/cancel", ok({}, status=502))
    with pytest.raises(RunnerUnavailable):
        runner.cancel(REPO, "555")


def test_tokens_are_asked_for_the_runs_repo(github):
    tokens = Tokens()
    runner = ActionsRunner(github, tokens)
    github.on("POST", f"/repos/{REPO}/actions/runs/555/cancel", ok({}, status=202))
    runner.cancel(REPO, "555")
    assert tokens.asked == [REPO]


def test_failing_to_get_a_token_is_runner_unavailable(github):
    class NoTokens:
        def token(self, repo):
            raise GitHubError(Request("POST", "https://api.github.com/app", {}), HttpResponse(502, {}, b""))

    with pytest.raises(RunnerUnavailable):
        ActionsRunner(github, NoTokens()).state(REPO, "555")


def test_nothing_outside_the_adapter_knows_its_actions():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "factory"
    leaks = [
        p.name
        for p in src.glob("*.py")
        if p.name != "actions_runner.py"
        and any(s in p.read_text() for s in ("/actions/", "/artifacts", "github.com/"))
    ]
    assert leaks == []
