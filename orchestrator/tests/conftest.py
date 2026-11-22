"""Shared helpers: drive runs through the real loop with a fake runner."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory import cli, db, loop
from factory.runner import FakeRunner, RunFiles

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def example(kind: str, name: str) -> dict:
    return json.loads((EXAMPLES / kind / name).read_text())


class World:
    """A database, a fake runner and a CLI pointed at both."""

    def __init__(self, path: Path, capsys):
        self.path = path
        self.conn = db.connect(path)
        self.runner = FakeRunner()
        self._capsys = capsys

    def queue(self, *, repo="octo/widgets", task="implement", ticket="7", timeout=45, **kwargs):
        dispatch = example("dispatch", f"{task}.valid.json")
        dispatch.pop("run_id", None)
        dispatch.update(repo=repo, timeout_minutes=timeout)
        return loop.enqueue(conn=self.conn, repo=repo, task=task, ticket=ticket, dispatch=dispatch, **kwargs)

    def dispatch(self, run_id: int, *, ago: timedelta = timedelta(0)) -> None:
        """Dispatch the queue (in-flight limit 99) and backdate `run_id`'s dispatch."""
        loop.dispatch(self.conn, self.runner, limit=99)
        when = datetime.now(UTC) - ago
        self._set(run_id, dispatched_at=when)

    def finish(self, run_id: int, result: dict | str, *, minutes: float = 10, ago: timedelta = timedelta(0)):
        """Run `run_id` to the end with `result` (an example name or a dict), taking `minutes`."""
        if isinstance(result, str):
            result = example("result", result)
        result = {**result, "run_id": run_id}
        if loop.get_run(self.conn, run_id)["status"] == "queued":
            self.dispatch(run_id)
        rid = loop.get_run(self.conn, run_id)["runner_run_id"]
        self.runner.finish(rid, RunFiles(json.dumps(result).encode()))
        loop.collect(self.conn, self.runner)
        finished = datetime.now(UTC) - ago
        self._set(run_id, dispatched_at=finished - timedelta(minutes=minutes), finished_at=finished)
        return run_id

    def backdate(self, run_id: int, *, created: timedelta) -> None:
        self._set(run_id, created_at=datetime.now(UTC) - created)

    def _set(self, run_id: int, **columns: datetime) -> None:
        # Test setup only: the loop stamps times with the real clock.
        for column, when in columns.items():
            self.conn.execute(
                f"UPDATE runs SET {column} = ? WHERE id = ?",
                (when.isoformat(timespec="seconds"), run_id),
            )

    def cli(self, *args: str) -> tuple[int, str, str]:
        code = cli.main(["--db", str(self.path), *args])
        out, err = self._capsys.readouterr()
        return code, out, err


@pytest.fixture
def world(tmp_path, capsys):
    w = World(tmp_path / "factory.db", capsys)
    yield w
    w.conn.close()
