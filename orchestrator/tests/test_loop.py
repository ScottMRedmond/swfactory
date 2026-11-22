"""The core loop: queued runs are dispatched to a runner, finished ones are
collected and their result.json validated."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory import db, loop
from factory.runner import FakeRunner, RunFiles

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def example(kind, name):
    return json.loads((EXAMPLES / kind / name).read_text())


SMOKE_DISPATCH = example("dispatch", "smoke.valid.json")


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "factory.db")


@pytest.fixture
def runner():
    return FakeRunner()


def enqueue_smoke(conn, repo="octo/widgets"):
    dispatch = {k: v for k, v in SMOKE_DISPATCH.items() if k != "run_id"}
    dispatch["repo"] = repo
    return loop.enqueue(conn, repo=repo, task="smoke", dispatch=dispatch)


def smoke_result(run_id, **changes):
    return {**example("result", "smoke.valid.json"), "run_id": run_id, **changes}


def row(conn, run_id):
    return loop.get_run(conn, run_id)


def finish(runner, conn, run_id, result):
    runner.finish(row(conn, run_id)["runner_run_id"], RunFiles(result_json=json.dumps(result).encode()))


# --- dispatching ------------------------------------------------------------


def test_a_queued_run_is_dispatched_and_its_runner_id_stored(conn, runner):
    run_id = enqueue_smoke(conn)

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert r["status"] == "dispatched"
    assert r["runner_run_id"] == runner.dispatched[0][0]
    assert r["dispatched_at"] is not None


def test_the_dispatch_carries_the_row_id_as_run_id(conn, runner):
    run_id = enqueue_smoke(conn)

    loop.tick(conn, runner, limit=2)

    sent = runner.dispatched[0][1]
    assert sent["run_id"] == run_id
    assert sent == {**SMOKE_DISPATCH, "run_id": run_id}
    assert json.loads(row(conn, run_id)["dispatch"]) == sent


def test_oldest_queued_rows_go_first_up_to_the_limit(conn, runner):
    ids = [enqueue_smoke(conn) for _ in range(3)]

    loop.tick(conn, runner, limit=2)

    assert [d[1]["run_id"] for d in runner.dispatched] == ids[:2]
    assert row(conn, ids[2])["status"] == "queued"


def test_runs_in_flight_count_against_the_limit(conn, runner):
    first = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=1)
    second = enqueue_smoke(conn)

    loop.tick(conn, runner, limit=1)

    assert row(conn, second)["status"] == "queued"
    finish(runner, conn, first, smoke_result(first))
    loop.tick(conn, runner, limit=1)
    assert row(conn, second)["status"] == "dispatched"


def test_a_row_someone_else_dispatched_isnt_dispatched_again(conn, runner):
    run_id = enqueue_smoke(conn)
    stale = row(conn, run_id)
    loop.tick(conn, runner, limit=2)

    assert loop.dispatch_run(conn, runner, stale) is False

    assert len(runner.dispatched) == 1


def test_a_claim_left_by_a_process_that_died_mid_dispatch_is_queued_again(conn, runner):
    run_id = enqueue_smoke(conn)
    claimed_at = (datetime.now(UTC) - timedelta(minutes=11)).isoformat(timespec="seconds")
    conn.execute("UPDATE runs SET status = 'dispatched', dispatched_at = ? WHERE id = ?",
                 (claimed_at, run_id))

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["runner_run_id"] == runner.dispatched[0][0]


# --- collecting -------------------------------------------------------------


def test_a_finished_run_is_collected_with_its_result(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    finish(runner, conn, run_id, smoke_result(run_id))

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"]) == ("done", "succeeded")
    assert json.loads(r["result"]) == smoke_result(run_id)
    assert r["finished_at"] is not None


def test_a_failed_result_is_recorded_failed(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    failed = {**example("result", "smoke.valid-setup-failed.json"), "run_id": run_id}
    finish(runner, conn, run_id, failed)

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["outcome"] == "failed"
    assert json.loads(row(conn, run_id)["result"]) == failed


def test_a_running_run_is_left_alone(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["status"] == "dispatched"


def test_a_run_finished_long_ago_is_still_collected(conn, runner, tmp_path):
    # The Mac mini slept; on waking the orchestrator starts afresh and collects.
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    finish(runner, conn, run_id, smoke_result(run_id))
    conn.close()

    conn = db.connect(tmp_path / "factory.db")
    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["outcome"] == "succeeded"


def test_collecting_frees_a_slot_in_the_same_loop(conn, runner):
    first = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=1)
    second = enqueue_smoke(conn)
    finish(runner, conn, first, smoke_result(first))

    loop.tick(conn, runner, limit=1)

    assert row(conn, first)["status"] == "done"
    assert row(conn, second)["status"] == "dispatched"


def test_a_missing_run_is_failed(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    runner.forget(row(conn, run_id)["runner_run_id"])

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"], r["result"]) == ("done", "failed", None)


def test_an_unavailable_runner_leaves_queued_rows_queued(conn, runner):
    run_id = enqueue_smoke(conn)
    runner.unavailable = True

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["runner_run_id"]) == ("queued", None)
    runner.unavailable = False
    loop.tick(conn, runner, limit=2)
    assert row(conn, run_id)["status"] == "dispatched"


def test_an_unavailable_runner_leaves_dispatched_rows_as_they_are(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    finish(runner, conn, run_id, smoke_result(run_id))
    runner.unavailable = True

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["status"] == "dispatched"
    runner.unavailable = False
    loop.tick(conn, runner, limit=2)
    assert row(conn, run_id)["outcome"] == "succeeded"


def test_a_run_the_runner_refused_goes_back_to_the_queue(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=1)
    first = row(conn, run_id)["runner_run_id"]
    runner.refuse(first)

    loop.tick(conn, runner, limit=1)

    r = row(conn, run_id)
    # Dispatching then backs off (see test_attempts.py), so it isn't refused every tick.
    assert (r["status"], r["attempt"], r["outcome"], r["runner_run_id"]) == (
        "queued", 1, None, None)
    assert len(runner.dispatched) == 1


def test_a_run_the_runner_timed_out_is_timed_out(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    runner.time_out(row(conn, run_id)["runner_run_id"])

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"], r["result"]) == ("done", "timed_out", None)


def test_a_finished_run_without_files_is_failed(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    runner.finish(row(conn, run_id)["runner_run_id"], None)

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"], r["result"]) == ("done", "failed", None)


def test_a_finished_run_without_a_result_json_is_failed(conn, runner):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    runner.finish(row(conn, run_id)["runner_run_id"], RunFiles(result_json=None, bundle=b"x"))

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["outcome"] == "failed"


# --- validating -------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(lambda rid: smoke_result(rid, contract_version=2), id="unknown version"),
        pytest.param(lambda rid: smoke_result(rid, output={"x": 1}), id="fails its schema"),
        pytest.param(lambda rid: smoke_result(rid + 1), id="another run's result"),
        pytest.param(
            lambda rid: {**example("result", "explore.valid.json"), "run_id": rid},
            id="another task's result",
        ),
    ],
)
def test_an_invalid_result_is_recorded_invalid(conn, runner, result):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    finish(runner, conn, run_id, result(run_id))

    loop.tick(conn, runner, limit=2)

    r = row(conn, run_id)
    assert (r["status"], r["outcome"]) == ("done", "invalid")
    assert json.loads(r["result"]) == result(run_id)


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[1, 2]"])
def test_a_result_that_isnt_a_json_object_is_invalid(conn, runner, raw):
    run_id = enqueue_smoke(conn)
    loop.tick(conn, runner, limit=2)
    runner.finish(row(conn, run_id)["runner_run_id"], RunFiles(result_json=raw))

    loop.tick(conn, runner, limit=2)

    assert row(conn, run_id)["outcome"] == "invalid"
