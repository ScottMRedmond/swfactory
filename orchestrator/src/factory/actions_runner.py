"""The Runner interface on GitHub Actions (v1).

The only code that knows runs happen on Actions: it dispatches the enrolled
repo's caller workflow, reads the Actions run, downloads its artifact and
builds its URLs. See orchestrator.md#dispatching-and-collecting.
"""

import http.client
import io
import json
import logging
import zipfile
from typing import Any, Protocol

from factory.github import API, API_VERSION, USER_AGENT, GitHubError, Request, Response, Transport
from factory.runner import RunFiles, RunnerUnavailable, RunRefused, RunState

log = logging.getLogger(__name__)

WEB = "https://github.com"
CALLER_WORKFLOW = "factory.yml"
ARTIFACT = "factory-run"

TIMED_OUT_MESSAGE = "The job has exceeded the maximum execution time"
# From user reports, not GitHub's docs (orchestrator.md#dispatching-and-collecting).
BILLING_MESSAGE = (
    "The job was not started because recent account payments have failed "
    "or your spending limit needs to be increased"
)


class TokenSource(Protocol):
    def token(self, repo: str) -> str:
        """A token that may act on `repo` (in the factory, `github.AppAuth`)."""


class ActionsRunner:
    """Talks to Actions through a `github.Transport`, with a token per repo."""

    def __init__(self, transport: Transport, tokens: TokenSource) -> None:
        self._transport = transport
        self._tokens = tokens
        self._default_branches: dict[str, str] = {}
        self._logged_stepless_jobs: set[tuple[str, int]] = set()

    # --- the Runner interface ---

    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        body = {
            "ref": self._default_branch(repo),
            "inputs": {"dispatch": json.dumps(dispatch)},
            "return_run_details": True,
        }
        resp = self._api(
            repo, "POST", f"/repos/{repo}/actions/workflows/{CALLER_WORKFLOW}/dispatches", body
        )
        if resp.status != 200:
            raise _unavailable("dispatch was rejected", resp)
        run_id = _json(resp).get("workflow_run_id")
        if not isinstance(run_id, int):
            raise _unavailable("dispatch answered without a workflow_run_id", resp)
        return str(run_id)

    def state(self, repo: str, runner_run_id: str) -> RunState:
        resp = self._api(repo, "GET", f"/repos/{repo}/actions/runs/{runner_run_id}")
        if resp.status == 404:
            return RunState.MISSING
        if resp.status != 200:
            raise _unavailable(f"couldn't read run {runner_run_id}", resp)
        run = _json(resp)
        if run.get("status") != "completed":
            return RunState.RUNNING
        if run.get("conclusion") == "success":
            return RunState.FINISHED
        return self._judge_unsuccessful(repo, run)

    def files(self, repo: str, runner_run_id: str) -> RunFiles | None:
        resp = self._api(
            repo, "GET", f"/repos/{repo}/actions/runs/{runner_run_id}/artifacts?name={ARTIFACT}"
        )
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise _unavailable(f"couldn't list run {runner_run_id}'s artifacts", resp)
        artifacts = [
            a for a in _json(resp).get("artifacts", []) if a.get("name") == ARTIFACT and not a.get("expired")
        ]
        if not artifacts:
            return None
        archive = self._download(repo, artifacts[0]["archive_download_url"])
        return None if archive is None else _unzip(archive, runner_run_id)

    def log_link(self, repo: str, runner_run_id: str) -> str:
        return f"{WEB}/{repo}/actions/runs/{runner_run_id}"

    def cancel(self, repo: str, runner_run_id: str) -> None:
        resp = self._api(repo, "POST", f"/repos/{repo}/actions/runs/{runner_run_id}/cancel")
        # 409: the run has already completed; 404: Actions has no such run.
        if resp.status not in (202, 404, 409):
            raise _unavailable(f"couldn't cancel run {runner_run_id}", resp)

    # --- Actions details ---

    def _judge_unsuccessful(self, repo: str, run: dict[str, Any]) -> RunState:
        """A completed run that didn't succeed: timed out, refused, or just finished."""
        jobs_resp = self._api(
            repo, "GET", f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100"
        )
        if jobs_resp.status != 200:
            raise _unavailable(f"couldn't read run {run['id']}'s jobs", jobs_resp)
        jobs = _json(jobs_resp).get("jobs", [])
        timed_out = False
        for job in jobs:
            stepless = not job.get("steps")
            if stepless:
                self._log_stepless(repo, run, job)
            if not stepless and run.get("conclusion") != "cancelled":
                continue
            messages = self._annotations(repo, job["id"])
            if stepless and any(BILLING_MESSAGE in m for m in messages):
                raise RunRefused(
                    f"Actions refused run {run['id']} in {repo}: the spending limit is reached "
                    "or a payment failed"
                )
            if any(TIMED_OUT_MESSAGE in m for m in messages):
                timed_out = True
        return RunState.TIMED_OUT if timed_out else RunState.FINISHED

    def _annotations(self, repo: str, job_id: int) -> list[str]:
        # A job is a check run; its annotations carry the timeout and billing messages.
        resp = self._api(repo, "GET", f"/repos/{repo}/check-runs/{job_id}/annotations?per_page=100")
        if resp.status != 200:
            raise _unavailable(f"couldn't read job {job_id}'s annotations", resp)
        return [a.get("message") or "" for a in _json(resp, default=[])]

    def _log_stepless(self, repo: str, run: dict[str, Any], job: dict[str, Any]) -> None:
        # The billing refusal is known only from user reports, so keep the evidence.
        key = (repo, job.get("id"))
        if key in self._logged_stepless_jobs:
            return
        self._logged_stepless_jobs.add(key)
        log.warning(
            "Actions job ran no steps (run %s in %s). Run: %s Job: %s",
            run.get("id"), repo, json.dumps(run), json.dumps(job),
        )

    def _default_branch(self, repo: str) -> str:
        if repo not in self._default_branches:
            resp = self._api(repo, "GET", f"/repos/{repo}")
            if resp.status != 200:
                raise _unavailable(f"couldn't read {repo}", resp)
            self._default_branches[repo] = _json(resp)["default_branch"]
        return self._default_branches[repo]

    def _download(self, repo: str, url: str) -> bytes | None:
        """An artifact's zip, or None if it's gone. The redirect target gets no token."""
        resp = self._send("GET", url, repo)
        if resp.status in (404, 410):
            return None
        location = _header(resp, "Location")
        if resp.status in (301, 302, 303, 307, 308) and location:
            # A transport that doesn't follow redirects; the blob URL is pre-signed.
            resp = self._send("GET", location, None)
            if resp.status in (404, 410):
                return None
        if resp.status != 200:
            raise _unavailable("couldn't download the artifact", resp)
        return resp.body

    def _api(self, repo: str, method: str, path: str, body: Any = None) -> Response:
        return self._send(method, API + path, repo, body)

    def _send(self, method: str, url: str, repo: str | None, body: Any = None) -> Response:
        """One request: to the API with `repo`'s token, or with no token at all."""
        try:
            headers = {"User-Agent": USER_AGENT}
            if repo is not None:
                headers |= {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                    "Authorization": f"Bearer {self._tokens.token(repo)}",
                }
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            return self._transport(Request(method, url, headers, data))
        except (OSError, http.client.HTTPException, GitHubError) as e:
            # GitHubError: minting the installation token failed (its message holds no token).
            raise RunnerUnavailable(f"{method} {url.split('?')[0]}: {e}") from e


def _header(resp: Response, name: str) -> str | None:
    return next((v for k, v in resp.headers.items() if k.lower() == name.lower()), None)


def _unavailable(what: str, resp: Response) -> RunnerUnavailable:
    data = _json(resp, default={}) if resp.body else {}
    message = data.get("message", "") if isinstance(data, dict) else ""
    return RunnerUnavailable(f"{what}: HTTP {resp.status} {message}".rstrip())


def _json(resp: Response, default: Any = None) -> Any:
    try:
        return json.loads(resp.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        if default is not None:
            return default
        raise RunnerUnavailable(f"HTTP {resp.status} answer isn't JSON") from None


def _unzip(archive: bytes, runner_run_id: str) -> RunFiles | None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            entries = {name: z.read(name) for name in z.namelist() if not name.endswith("/")}
    except (zipfile.BadZipFile, OSError) as e:
        log.warning("run %s's artifact isn't a readable zip: %s", runner_run_id, e)
        return None
    bundles = [name for name in entries if "/" not in name and name.endswith(".bundle")]
    return RunFiles(
        result_json=entries.get("result.json"),
        bundle=entries[bundles[0]] if bundles else None,
        screenshots={
            name.removeprefix("evidence/"): data
            for name, data in entries.items()
            if name.startswith("evidence/") and name.endswith(".png")
        },
    )
