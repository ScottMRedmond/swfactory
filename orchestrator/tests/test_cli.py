"""`factory runs`: the run history, read straight from SQLite."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from factory import cli, db, loop
from factory.runner import FakeRunner, RunFiles

DISPATCH = {
    "contract_version": 1,
    "repo": "octo/widgets",
    "task": "smoke",
    "base_sha": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "enrolment_sha": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "timeout_minutes": 20,
}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "factory.db"


@pytest.fixture
def conn(path):
    return db.connect(path)


def run_cli(capsys, path, *args):
    code = cli.main(["--db", str(path), *args])
    out, err = capsys.readouterr()
    return code, out, err


def test_runs_lists_the_last_20_rows_newest_first(conn, path, capsys):
    ids = [loop.enqueue(conn, repo="octo/widgets", task="smoke", dispatch=DISPATCH) for _ in range(25)]

    code, out, _ = run_cli(capsys, path, "runs")

    assert code == 0
    lines = out.splitlines()[1:]  # after the header
    assert [int(line.split()[0]) for line in lines] == ids[::-1][:20]


def test_runs_shows_each_rows_state(conn, path, capsys):
    run_id = loop.enqueue(conn, repo="octo/widgets", task="smoke", ticket="7", dispatch=DISPATCH)

    _, out, _ = run_cli(capsys, path, "runs")

    header, line = out.splitlines()
    assert header.split()[:6] == ["ID", "REPO", "TASK", "TICKET", "ATTEMPT", "STATUS"]
    assert line.split()[:6] == [str(run_id), "octo/widgets", "smoke", "7", "1", "queued"]


def test_runs_id_shows_one_row_with_its_dispatch_and_result(conn, path, capsys):
    runner = FakeRunner()
    run_id = loop.enqueue(conn, repo="octo/widgets", task="smoke", dispatch=DISPATCH)
    loop.tick(conn, runner, limit=2)
    result = {"contract_version": 1, "run_id": run_id, "task": "smoke", "outcome": "succeeded", "output": {}}
    runner.finish(loop.get_run(conn, run_id)["runner_run_id"], RunFiles(json.dumps(result).encode()))
    loop.tick(conn, runner, limit=2)

    code, out, _ = run_cli(capsys, path, "runs", str(run_id))

    assert code == 0
    assert "status: done" in out
    assert "outcome: succeeded" in out
    assert '"timeout_minutes": 20' in out  # the dispatch
    assert '"outcome": "succeeded"' in out  # the result


def test_runs_id_for_an_unknown_run_fails(conn, path, capsys):
    code, _, err = run_cli(capsys, path, "runs", "99")
    assert code == 1
    assert "no run 99" in err


def test_runs_doesnt_write_to_the_database(path, capsys):
    code, _, err = run_cli(capsys, path, "runs")
    assert code == 1
    assert not path.exists()


def listed_ids(out):
    return [int(line.split()[0]) for line in out.splitlines()[1:]]


def test_runs_filters_by_repo_ticket_task_and_outcome(world):
    a = world.finish(world.queue(repo="octo/widgets", task="implement", ticket="7"), "implement.valid.json")
    b = world.finish(world.queue(repo="octo/widgets", task="implement", ticket="8"), "implement.valid-check-failed.json")
    c = world.queue(repo="octo/gadgets", task="explore", ticket="7")

    assert listed_ids(world.cli("runs", "--repo", "octo/widgets")[1]) == [b, a]
    assert listed_ids(world.cli("runs", "--ticket", "7")[1]) == [c, a]
    assert listed_ids(world.cli("runs", "--task", "explore")[1]) == [c]
    assert listed_ids(world.cli("runs", "--outcome", "failed")[1]) == [b]
    assert listed_ids(world.cli("runs", "--repo", "octo/widgets", "--ticket", "7")[1]) == [a]


def test_runs_since_keeps_rows_created_within_the_period(world):
    old = world.queue()
    world.backdate(old, created=timedelta(days=3))
    new = world.queue()

    assert listed_ids(world.cli("runs", "--since", "2d")[1]) == [new]
    assert listed_ids(world.cli("runs", "--since", "4d")[1]) == [new, old]
    two_days_ago = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    assert listed_ids(world.cli("runs", "--since", two_days_ago)[1]) == [new]


def test_runs_rejects_a_since_it_cant_read(world):
    with pytest.raises(SystemExit):
        world.cli("runs", "--since", "last tuesday")


def test_runs_json_gives_the_rows_with_dispatch_and_result_parsed(world):
    run_id = world.finish(world.queue(), "implement.valid.json")

    code, out, _ = world.cli("runs", "--json")

    assert code == 0
    [row] = json.loads(out)
    assert row["id"] == run_id
    assert row["outcome"] == "succeeded"
    assert row["dispatch"]["run_id"] == run_id
    assert row["result"]["task"] == "implement"


def test_runs_id_shows_the_retry_chain_log_link_and_check_tail(world):
    first = world.finish(world.queue(), "implement.valid-check-failed.json")
    second = world.finish(world.queue(attempt=2, retry_of=first), "implement.valid.json")

    for run_id in (first, second):
        code, out, _ = world.cli("runs", str(run_id))
        assert code == 0
        assert f"retry chain: {first} (failed) -> {second} (succeeded)" in out
    _, out, _ = world.cli("runs", str(first))
    rid = loop.get_run(world.conn, first)["runner_run_id"]
    assert f"log: https://runner.invalid/octo/widgets/runs/{rid}" in out
    assert "check output (last lines):\nFAILED tests/test_search.py::test_accents" in out


def test_runs_id_json_gives_the_row_and_its_chain(world):
    first = world.finish(world.queue(), "implement.valid-check-failed.json")
    second = world.queue(attempt=2, retry_of=first)

    code, out, _ = world.cli("runs", str(second), "--json")

    assert code == 0
    row = json.loads(out)
    assert row["id"] == second
    assert [r["id"] for r in row["retry_chain"]] == [first, second]
