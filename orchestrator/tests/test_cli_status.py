"""`factory status`: the factory at a glance, read straight from SQLite."""

from datetime import UTC, datetime, timedelta

from factory import state


def ago(**kwargs) -> str:
    return (datetime.now(UTC) - timedelta(**kwargs)).isoformat(timespec="seconds")


def from_now(**kwargs) -> datetime:
    return datetime.now(UTC) + timedelta(**kwargs)


def usage(session=10, weekly=5, resets=None):
    resets = (resets or from_now(hours=2)).isoformat()
    return [
        {"kind": "session", "percent": session, "resets_at": resets},
        {"kind": "weekly_all", "percent": weekly, "resets_at": from_now(days=3).isoformat()},
    ]


def status(world):
    code, out, err = world.cli("status")
    assert code == 0, err
    return out


def section(out, title):
    """The lines under `title` up to the next blank line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    body = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        body.append(line)
    return lines[start], body


def test_a_fresh_factory(world):
    out = status(world)

    assert "paused: no" in out
    assert "usage: unknown (no reading yet)" in out
    assert "today: 0 of 40 runs dispatched" in out
    assert section(out, "in flight")[0] == "in flight: none"
    assert section(out, "queue")[0] == "queue: empty"
    assert section(out, "polls")[0] == "polls: none yet"
    assert section(out, "factory problems")[0] == "factory problems: none open"


def test_shows_the_pause_flag(world):
    state.set_paused(world.conn, True)
    assert "paused: yes" in status(world)
    state.set_paused(world.conn, False)
    assert "paused: no" in status(world)


def test_shows_the_usage_reading_and_its_age(world):
    state.record_usage(world.conn, usage(session=17, weekly=3), read_at=ago(minutes=4))

    assert "usage: 5-hour 17%, weekly 3% (read 4m ago)" in status(world)


def test_shows_an_unreadable_usage_reading(world):
    state.record_usage(world.conn, None, error="no usage_report", read_at=ago(minutes=2))

    assert "usage: unreadable: no usage_report (read 2m ago)" in status(world)


def test_counts_todays_dispatches_except_smoke(world):
    for _ in range(3):
        world.queue()
    world.queue(task="smoke", ticket=None)
    world.dispatch(1)

    assert "today: 3 of 40 runs dispatched" in status(world)


def test_lists_runs_in_flight(world):
    run_id = world.queue(ticket="12")
    world.dispatch(run_id, ago=timedelta(minutes=7))

    header, lines = section(status(world), "in flight")

    assert header == "in flight: 1"
    assert lines[1].split() == [str(run_id), "octo/widgets", "implement", "12", "1", "7m"]


def test_the_queue_says_why_each_run_is_held(world):
    state.record_usage(world.conn, usage(), read_at=ago(minutes=1))
    in_flight = world.queue(ticket="1")
    world.dispatch(in_flight)
    first = world.queue(ticket="2")
    second = world.queue(ticket="3")
    third = world.queue(ticket="4")

    header, lines = section(status(world), "queue")

    assert header == "queue: 3, oldest first"
    held = {int(line.split()[0]): line for line in lines[1:]}
    assert held[first].endswith("next to dispatch")
    assert held[second].endswith("waiting for a free slot")
    assert held[third].endswith("waiting for a free slot")


def test_a_paused_factory_holds_the_queue(world):
    state.record_usage(world.conn, usage(), read_at=ago(minutes=1))
    state.set_paused(world.conn, True)
    world.queue()

    _, lines = section(status(world), "queue")

    assert lines[1].endswith("factory paused")


def test_high_usage_holds_the_queue_until_its_reset(world):
    resets = datetime(2026, 11, 22, 15, 45, tzinfo=UTC)
    state.record_usage(world.conn, usage(session=64, resets=resets), read_at=ago(minutes=1))
    world.queue()

    _, lines = section(status(world), "queue")

    local = resets.astimezone().strftime("%-I:%M %p").lower()
    assert lines[1].endswith(f"Claude 5-hour usage at 64%, resumes about {local}")


def test_high_weekly_usage_holds_the_queue(world):
    state.record_usage(world.conn, usage(weekly=50), read_at=ago(minutes=1))
    world.queue()

    _, lines = section(status(world), "queue")

    assert "Claude weekly usage at 50%" in lines[1]


def test_unknown_or_stale_usage_holds_the_queue(world):
    world.queue()
    assert section(status(world), "queue")[1][1].endswith("Claude usage unknown")

    state.record_usage(world.conn, usage(), read_at=ago(minutes=61))
    assert section(status(world), "queue")[1][1].endswith("Claude usage unknown")


def test_the_daily_cap_holds_the_queue(world):
    state.record_usage(world.conn, usage(), read_at=ago(minutes=1))
    for _ in range(40):
        world.queue()
    world.dispatch(1)
    world.queue()

    out = status(world)

    assert "today: 40 of 40 runs dispatched (cap reached)" in out
    assert section(out, "queue")[1][1].endswith("daily cap reached")


def test_shows_the_last_successful_poll_per_repo(world):
    state.record_poll(world.conn, "octo/widgets", at=ago(minutes=3))
    state.record_poll(world.conn, "octo/gadgets", at=ago(seconds=20))
    state.record_poll(world.conn, "octo/widgets", at=ago(seconds=50))

    header, lines = section(status(world), "polls")

    assert header == "polls: last success per repo"
    assert [line.split() for line in lines] == [["octo/gadgets", "20s", "ago"], ["octo/widgets", "50s", "ago"]]


def test_shows_open_factory_problems(world):
    state.open_problem(world.conn, "usage_unreadable", "Claude usage unreadable for 15 minutes", issue="octo/factory#9")
    state.open_problem(world.conn, "claude_auth", "Claude rejected the token", repo="octo/widgets")
    state.open_problem(world.conn, "enrolment_invalid", "config.yml fails its schema", repo="octo/gadgets")
    state.close_problem(world.conn, "enrolment_invalid", repo="octo/gadgets")

    header, lines = section(status(world), "factory problems")

    assert header == "factory problems: 2 open"
    assert "usage_unreadable" in lines[0] and "octo/factory#9" in lines[0]
    assert "claude_auth" in lines[1] and "octo/widgets" in lines[1]


def test_reopening_an_open_problem_updates_it(world):
    state.open_problem(world.conn, "claude_auth", "first", repo="octo/widgets")
    state.open_problem(world.conn, "claude_auth", "second", repo="octo/widgets")

    _, lines = section(status(world), "factory problems")

    assert len(lines) == 1 and "second" in lines[0]
