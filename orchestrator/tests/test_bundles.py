"""Bundle checks (software-factory.md#the-runner-contract) and pushing a
checked bundle, against real git repos: a bare repo stands in for GitHub."""

import pytest
from git_repos import Remote, git

from factory import bundles
from factory.bundles import BundleError

REPO = "octo/widgets"


@pytest.fixture
def remote(tmp_path):
    return Remote(tmp_path / "github")


@pytest.fixture
def store(tmp_path, remote):
    return bundles.Store(tmp_path / "git", remote=lambda repo: (remote.url, {}))


def built(store, remote, bundle, *, task="implement", verdict="built", run_id=5, base=None):
    return store.check(REPO, run_id, bundle, task=task, verdict=verdict,
                       base_sha=base or remote.base)


def test_a_linear_bundle_on_base_sha_passes_and_reports_its_commits_and_files(store, remote):
    remote.branch("factory/7-5")
    remote.commit({"src/search.py": "x\n"}, "Fold accents")
    remote.commit({"tests/test_search.py": "y\n"}, "Test accents")

    checked = built(store, remote, remote.bundle("factory/7-5"))

    assert [line.split(" ", 1)[1] for line in checked.log] == ["Test accents", "Fold accents"]
    assert checked.files == ["src/search.py", "tests/test_search.py"]


def test_a_checked_bundle_is_pushed_to_its_branch_without_force(store, remote):
    remote.branch("factory/7-5")
    tip = remote.commit({"a.txt": "a\n"}, "A")
    built(store, remote, remote.bundle("factory/7-5"))

    store.push(REPO, 5, "factory/7-5")
    store.push(REPO, 5, "factory/7-5")  # again, e.g. after a crash: nothing to do

    assert remote.remote_branch("factory/7-5") == tip


def test_push_never_overwrites_a_branch_that_has_moved(store, remote):
    remote.branch("factory/7-5")
    remote.commit({"a.txt": "a\n"}, "A")
    built(store, remote, remote.bundle("factory/7-5"))
    remote.branch("other")
    other = remote.commit({"b.txt": "b\n"}, "B")
    git(remote.work, "push", "-q", "origin", "other:factory/7-5")

    with pytest.raises(bundles.GitError):
        store.push(REPO, 5, "factory/7-5")
    assert remote.remote_branch("factory/7-5") == other


def test_checked_bundles_can_be_read_back_by_run(store, remote):
    remote.branch("factory/7-5")
    remote.commit({"a.txt": "a\n"}, "A")
    first = built(store, remote, remote.bundle("factory/7-5"))

    assert store.checked(REPO, 5, remote.base) == first
    assert store.checked(REPO, 6, remote.base) is None


# --- what makes a result invalid ------------------------------------------------


def test_built_without_a_bundle_is_invalid(store, remote):
    with pytest.raises(BundleError, match="no commits"):
        built(store, remote, None)


def test_needs_a_human_without_a_bundle_is_fine(store, remote):
    assert built(store, remote, None, verdict="needs-a-human") is None


@pytest.mark.parametrize("task", ["explore", "auto-tickets"])
def test_any_bundle_from_explore_or_auto_tickets_is_invalid(store, remote, task):
    remote.branch("factory/7-5")
    remote.commit({"a.txt": "a\n"}, "A")

    with pytest.raises(BundleError, match=f"{task} leaves no bundle"):
        built(store, remote, remote.bundle("factory/7-5"), task=task, verdict="easy-call")


def test_a_bundle_on_another_base_is_invalid(store, remote):
    remote.commit({"main.txt": "m\n"}, "Main moves")
    git(remote.work, "push", "-q", "origin", "HEAD:main")
    moved = remote.head()
    remote.branch("factory/7-5", moved)
    remote.commit({"a.txt": "a\n"}, "A")

    with pytest.raises(BundleError, match="prerequisite"):
        built(store, remote, remote.bundle("factory/7-5", moved))


def test_a_bundle_with_more_than_one_ref_is_invalid(store, remote):
    remote.branch("one")
    remote.commit({"a.txt": "a\n"}, "A")
    remote.branch("two")
    remote.commit({"b.txt": "b\n"}, "B")
    out = remote.work.parent / "two.bundle"
    git(remote.work, "bundle", "create", "-q", str(out), f"{remote.base}..refs/heads/one",
        f"{remote.base}..refs/heads/two")

    with pytest.raises(BundleError, match="exactly one ref"):
        built(store, remote, out.read_bytes())


def test_a_merge_commit_is_invalid(store, remote):
    remote.branch("side")
    remote.commit({"b.txt": "b\n"}, "B")
    remote.branch("factory/7-5")
    remote.commit({"a.txt": "a\n"}, "A")
    git(remote.work, "merge", "-q", "--no-ff", "-m", "Merge side", "side")

    with pytest.raises(BundleError, match="merge commit"):
        built(store, remote, remote.bundle("factory/7-5"))


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".factory/config.yml"])
def test_touching_a_protected_path_is_invalid_even_if_a_later_commit_undoes_it(store, remote, path):
    remote.branch("factory/7-5")
    remote.commit({path: "evil\n"}, "Sneak")
    git(remote.work, "rm", "-q", path)
    git(remote.work, "commit", "-q", "-m", "Hide")

    with pytest.raises(BundleError, match="protected"):
        built(store, remote, remote.bundle("factory/7-5"))


def test_a_bundle_over_50_mb_is_invalid(store, remote):
    with pytest.raises(BundleError, match="50 MB"):
        built(store, remote, b"# v2 git bundle\n" + b"x" * (50 * 1024 * 1024))


def test_garbage_is_invalid(store, remote):
    with pytest.raises(BundleError):
        built(store, remote, b"not a bundle")
