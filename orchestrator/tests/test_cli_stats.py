"""`factory stats`: per task and repo, what the timeouts, models and thresholds are tuned with."""

import json
from datetime import timedelta


def seed(world):
    """implement on octo/widgets: ticket 7 failed then passed, 8 and 9 passed (9 near its timeout)."""
    first = world.finish(world.queue(ticket="7"), "implement.valid-check-failed.json", minutes=10)
    world.finish(world.queue(ticket="7", attempt=2, retry_of=first), "implement.valid.json", minutes=20)
    world.finish(world.queue(ticket="8"), "implement.valid.json", minutes=30)
    world.finish(world.queue(ticket="9"), "implement.valid.json", minutes=42)
    world.queue(task="explore", repo="octo/gadgets", ticket="3", timeout=20)


def stats(world, *args):
    code, out, _ = world.cli("stats", "--json", *args)
    assert code == 0
    return {(s["task"], s["repo"]): s for s in json.loads(out)}


def test_stats_per_task_and_repo(world):
    seed(world)

    s = stats(world)[("implement", "octo/widgets")]

    assert s["runs"] == 4
    assert s["outcomes"] == {"succeeded": 3, "failed": 1}
    assert s["failures"] == {"check_failed": 1}
    assert s["median_minutes"] == 25
    assert s["p90_minutes"] == 42
    assert s["near_timeout"] == 1
    assert (s["needed_second_attempt"], s["first_attempts"]) == (1, 3)


def test_stats_counts_runs_that_havent_finished(world):
    seed(world)

    s = stats(world)[("explore", "octo/gadgets")]

    assert s["runs"] == 1
    assert s["outcomes"] == {}
    assert s["median_minutes"] is None


def test_stats_since_leaves_out_older_runs(world):
    seed(world)
    old = world.finish(world.queue(task="review"), "review.valid.json")
    world.backdate(old, created=timedelta(days=40))

    assert ("review", "octo/widgets") not in stats(world, "--since", "30d")
    assert ("review", "octo/widgets") in stats(world)


def test_stats_prints_a_readable_table(world):
    seed(world)

    code, out, _ = world.cli("stats")

    assert code == 0
    header, *lines = out.splitlines()
    assert header.split()[:3] == ["TASK", "REPO", "RUNS"]
    [implement] = [line for line in lines if line.startswith("implement")]
    for cell in ("succeeded 3, failed 1", "check_failed 1", "25m", "42m", "33% (1/3)"):
        assert cell in implement
