"""The Runner interface: the only way the orchestrator talks to runners.

The orchestrator never calls a runner's own APIs or builds its URLs. A runner
identifies a run by its own id (`runner_run_id`, stored on the row) together
with the repo the run happens in.
"""

import itertools
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class RunState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    MISSING = "missing"  # the runner has no such run


@dataclass(frozen=True)
class RunFiles:
    """What a finished run left behind. Any of it may be absent."""

    result_json: bytes | None
    bundle: bytes | None = None
    screenshots: dict[str, bytes] = field(default_factory=dict)


class Runner(Protocol):
    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        """Start a run and return the runner's own id for it."""

    def state(self, repo: str, runner_run_id: str) -> RunState: ...

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

    def dispatch(self, repo: str, dispatch: dict[str, Any]) -> str:
        runner_run_id = str(next(self._ids))
        self._runs[runner_run_id] = {"repo": repo, "state": RunState.RUNNING, "files": None}
        self.dispatched.append((runner_run_id, dispatch))
        return runner_run_id

    def state(self, repo: str, runner_run_id: str) -> RunState:
        run = self._runs.get(runner_run_id)
        if run is None or run["repo"] != repo:
            return RunState.MISSING
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

    def forget(self, runner_run_id: str) -> None:
        """The runner no longer knows the run, e.g. its files expired."""
        del self._runs[runner_run_id]
