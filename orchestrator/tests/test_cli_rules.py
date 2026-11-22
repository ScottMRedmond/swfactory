"""`factory rules`: every review's missing_rules, grouped by repo."""

from datetime import timedelta

from conftest import example


def review(*rules):
    result = example("result", "review.valid.json")
    result["output"]["missing_rules"] = list(rules)
    return result


def test_rules_groups_missing_rules_by_repo_with_the_runs_that_wanted_them(world):
    a = world.finish(world.queue(task="review", repo="octo/widgets"), review("Name tests after behaviour"))
    world.finish(world.queue(task="implement", repo="octo/widgets"), "implement.valid.json")
    b = world.finish(world.queue(task="review", repo="octo/gadgets"), review("No print debugging"))
    c = world.finish(
        world.queue(task="review", repo="octo/widgets"),
        review("Name tests after behaviour", "Public functions have return types"),
    )

    code, out, _ = world.cli("rules")

    assert code == 0
    assert out.splitlines() == [
        "octo/gadgets",
        f"  No print debugging  (run {b})",
        "",
        "octo/widgets",
        f"  Name tests after behaviour  (runs {a}, {c})",
        f"  Public functions have return types  (run {c})",
    ]


def test_rules_since_leaves_out_older_reviews(world):
    old = world.finish(world.queue(task="review"), review("Old rule"))
    world.backdate(old, created=timedelta(days=40))
    new = world.finish(world.queue(task="review"), review("New rule"))

    _, out, _ = world.cli("rules", "--since", "30d")

    assert out.splitlines() == ["octo/widgets", f"  New rule  (run {new})"]


def test_rules_with_no_reviews_says_so(world):
    code, out, _ = world.cli("rules")
    assert code == 0
    assert out.strip() == "No review has reported a missing rule."
