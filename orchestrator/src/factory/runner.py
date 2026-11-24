"""The Runner interface: the only way the orchestrator talks to runners.

The orchestrator never calls a runner's own APIs or builds its URLs. A runner
identifies a run by its own id (`runner_run_id`, stored on the row) together
with the repo the run happens in.
"""

import itertools
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class RunState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    MISSING = "missing"  # the runner has no such run
    # Finished because the runner enforced the dispatch's timeout itself. Not a
    # human cancelling it: the outcome is `timed_out`, and the run left no files.
    TIMED_OUT = "timed_out"


class RunnerUnavailable(Exception):
    """The runner can't be reached or refuses a request, whatever the cause.

    Never a verdict on the run: a run that wasn't dispatched stays `queued`, and
    a `dispatched` run stays as it is until the runner answers. Different from
    `RunState.MISSING`, where the runner answers that it has no such run.
    """


class RunRefused(RunnerUnavailable):
    """The runner refused a run it had accepted (on Actions: the spending limit).

    The run will never happen, so it goes back to `queued` without counting an
    attempt.
    """


@dataclass(frozen=True)
class RunFiles:
    """What a finished run left behind. Any of it may be absent."""

    result_json: bytes | None
    bundle: bytes | None = None
    screenshots: dict[str, bytes] = field(default_factory=dict)


class Runner(Protocol):
    """Every method may raise `RunnerUnavailable`."""

    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        """Start a run and return the runner's own id for it."""

    def state(self, repo: str, runner_run_id: str) -> RunState:
        """Raises `RunRefused` for a run the runner refused to start."""

    def files(self, repo: str, runner_run_id: str) -> RunFiles | None:
        """A finished run's files, or None if it left none."""

    def log_link(self, repo: str, runner_run_id: str) -> str: ...

    def cancel(self, repo: str, runner_run_id: str) -> None: ...


class FakeRunner:
    """An in-memory Runner for tests. Runs stay running until told otherwise."""

    def __init__(self) -> None:
        self._ids = itertools.count(1001)
        self._runs: dict[str, dict[str, Any]] = {}
        self.dispatched: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []
        self.unavailable = False  # test control: every call raises RunnerUnavailable

    def _answer(self) -> None:
        if self.unavailable:
            raise RunnerUnavailable("the fake runner is unavailable")

    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        self._answer()
        runner_run_id = str(next(self._ids))
        self._runs[runner_run_id] = {"repo": repo, "state": RunState.RUNNING, "files": None}
        self.dispatched.append((runner_run_id, dispatch))
        return runner_run_id

    def state(self, repo: str, runner_run_id: str) -> RunState:
        self._answer()
        run = self._runs.get(runner_run_id)
        if run is None or run["repo"] != repo:
            return RunState.MISSING
        if run.get("refused"):
            raise RunRefused(f"the fake runner refused run {runner_run_id}")
        return run["state"]

    def files(self, repo: str, runner_run_id: str) -> RunFiles | None:
        if self.state(repo, runner_run_id) is not RunState.FINISHED:
            return None
        return self._runs[runner_run_id]["files"]

    def log_link(self, repo: str, runner_run_id: str) -> str:
        return f"https://runner.invalid/{repo}/runs/{runner_run_id}"

    def cancel(self, repo: str, runner_run_id: str) -> None:
        if self.state(repo, runner_run_id) is RunState.RUNNING:
            self.cancelled.append(runner_run_id)
            self._runs[runner_run_id]["state"] = RunState.FINISHED

    # --- test controls ---

    def finish(self, runner_run_id: str, files: RunFiles | None) -> None:
        self._runs[runner_run_id].update(state=RunState.FINISHED, files=files)

    def time_out(self, runner_run_id: str) -> None:
        """The runner ended the run at the dispatch's timeout."""
        self._runs[runner_run_id].update(state=RunState.TIMED_OUT, files=None)

    def refuse(self, runner_run_id: str) -> None:
        """The runner won't start the run it accepted (on Actions: the spending limit)."""
        self._runs[runner_run_id].update(refused=True)

    def forget(self, runner_run_id: str) -> None:
        """The runner no longer knows the run, e.g. its files expired."""
        del self._runs[runner_run_id]


CANNED_EXPLORE_OUTPUT = {
    "verdict": "needs-a-human",
    "summary": "Canned findings from the factory's fake runner; no agent ran.",
    "findings": "The fake runner returns this text for every explore run.",
    "plan": [],
    "files": [],
    "questions": ["This is a demo: is the comment under the App's identity?"],
}


class CannedRunner(FakeRunner):
    """A FakeRunner that finishes every explore run at once with a canned output.

    For demonstrating the orchestrator against real GitHub before a real
    runner exists (`factory serve --fake-runner`).
    """

    def __init__(self, explore_output: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.explore_output = explore_output or CANNED_EXPLORE_OUTPUT

    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        runner_run_id = super().dispatch(repo, dispatch)
        result = {
            "contract_version": 1,
            "run_id": dispatch["run_id"],
            "task": dispatch["task"],
            "outcome": "succeeded",
            "output": self.explore_output if dispatch["task"] == "explore" else {},
        }
        self.finish(runner_run_id, RunFiles(result_json=json.dumps(result).encode()))
        return runner_run_id
