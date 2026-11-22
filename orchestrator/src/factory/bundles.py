"""Bundles: checking a run's git bundle can be trusted, then pushing it.

A run's commits come back as a git bundle (`<base_sha>..refs/heads/<branch>`).
Before anything is pushed, the orchestrator checks its shape
(software-factory.md#the-runner-contract): exactly one ref whose prerequisite
is `base_sha`, no merge commits, at least one commit when implement says
`built`, none at all from explore or auto-tickets, at most 50 MB, and nothing
touching `.github/workflows/` or `.factory/`. A violation is a `BundleError`,
which makes the result `invalid`.

The checks need git objects, so a `Store` keeps a bare repo per target repo
under its root. It fetches `base_sha` from GitHub (shallow), fetches the
bundle into `refs/factory/runs/<run id>`, and later pushes that ref to the
branch the orchestrator named, never with force. The bundle itself isn't kept.
"""

import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from factory.contract import ContractError

MAX_BYTES = 50 * 1024 * 1024
PROTECTED = (".github/workflows/", ".factory/")
# Tasks whose runs never commit (software-factory.md#the-runner-contract).
NO_BUNDLE = ("explore", "auto-tickets", "smoke")

# (url, extra environment) for git to reach a repo on GitHub as the App.
Remote = Callable[[str], tuple[str, dict[str, str]]]


class BundleError(ContractError):
    """The bundle breaks the runner contract: the result is `invalid`."""


class GitError(OSError):
    """git failed for a reason that isn't the bundle's fault (e.g. the network).
    An OSError, so the caller tries again later."""

    stderr: str = ""


class Moved(GitError):
    """The branch has commits the push doesn't have: it was refused, not forced."""


@dataclass(frozen=True)
class Checked:
    """A bundle that passed the checks, as fetched into the store."""

    log: list[str]  # `git log --oneline base..tip`, newest first
    files: list[str]  # every path any commit touched, sorted


class Store:
    def __init__(self, root: Path, remote: Remote):
        self.root = Path(root)
        self._remote = remote

    def check(
        self, repo: str, run_id: int, bundle: bytes | None, *, task: str, verdict: str | None,
        base_sha: str, pr_base_sha: str | None = None, test_paths: Sequence[str] = (),
        findings_only: bool = False,
    ) -> Checked | None:
        """Check run `run_id`'s bundle and keep its commits for `push`.
        None if there's no bundle (and none was needed). A review's fixes may
        touch only the PR's own files (`pr_base_sha`..`base_sha`) and `test_paths`;
        a findings-only review makes none."""
        if bundle is None:
            if task == "implement" and verdict == "built":
                raise BundleError("implement says built but left no commits (no bundle)")
            return None
        if task in NO_BUNDLE:
            raise BundleError(f"{task} leaves no bundle, but this run left one")
        if findings_only:
            raise BundleError("a findings-only review leaves no bundle, but this run left one")
        if len(bundle) > MAX_BYTES:
            raise BundleError(f"the bundle is {len(bundle):,} bytes; the most is 50 MB")
        refs, prerequisites = _header(bundle)
        if len(refs) != 1:
            raise BundleError(f"the bundle must hold exactly one ref; it holds {len(refs)}")
        if prerequisites != [base_sha]:
            raise BundleError(
                f"the bundle's prerequisite must be base_sha {base_sha}; it has "
                f"{', '.join(prerequisites) or 'none'}"
            )
        git_dir = self._repo_dir(repo)
        self._fetch_base(repo, git_dir, base_sha)
        ref = _run_ref(run_id)
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".bundle") as f:
            f.write(bundle)
            f.flush()
            try:
                _git(git_dir, "fetch", "-q", "--no-tags", f.name, f"+{refs[0]}:{ref}")
            except GitError as e:
                raise BundleError(f"the bundle can't be read: {e}") from e
        try:
            checked = self._judge(git_dir, ref, base_sha, task, verdict)
            if task == "review" and pr_base_sha is not None:
                self._judge_reach(repo, git_dir, checked, base_sha, pr_base_sha, test_paths)
            return checked
        except BundleError:
            _git(git_dir, "update-ref", "-d", ref)
            raise

    def checked(self, repo: str, run_id: int, base_sha: str) -> Checked | None:
        """What `check` found for run `run_id`, or None if the store doesn't have it."""
        git_dir = self._repo_dir(repo)
        if not _git(git_dir, "for-each-ref", _run_ref(run_id)):
            return None
        return _inspect(git_dir, _run_ref(run_id), base_sha)

    def push(self, repo: str, run_id: int, branch: str) -> None:
        """Push run `run_id`'s checked commits to `branch`, never with force:
        pushing again is a no-op, and a branch that moved elsewhere is refused
        with `Moved`."""
        url, env = self._remote(repo)
        try:
            _git(self._repo_dir(repo), "push", "-q", url,
                 f"{_run_ref(run_id)}:refs/heads/{branch}", env=env)
        except GitError as e:
            if "[rejected]" in e.stderr:
                raise Moved(f"{branch} has moved: {e}") from e
            raise

    def branch_head(self, repo: str, branch: str) -> str | None:
        """The commit at the head of `branch` on GitHub, or None if there's no such branch."""
        url, env = self._remote(repo)
        out = _git(self._repo_dir(repo), "ls-remote", url, f"refs/heads/{branch}", env=env)
        return out.split()[0] if out else None

    def _repo_dir(self, repo: str) -> Path:
        git_dir = self.root / f"{repo}.git"
        if not (git_dir / "HEAD").exists():
            git_dir.mkdir(parents=True, exist_ok=True)
            _git(git_dir, "init", "-q", "--bare")
        return git_dir

    def _fetch_base(self, repo: str, git_dir: Path, base_sha: str) -> None:
        try:
            _git(git_dir, "cat-file", "-e", f"{base_sha}^{{commit}}")
            return
        except GitError:
            pass
        url, env = self._remote(repo)
        _git(git_dir, "fetch", "-q", "--no-tags", "--depth=1", url, base_sha, env=env)

    def _judge(
        self, git_dir: Path, ref: str, base_sha: str, task: str, verdict: str | None
    ) -> Checked:
        merges = _git(git_dir, "rev-list", "--merges", f"{base_sha}..{ref}")
        if merges:
            raise BundleError(f"the bundle has a merge commit ({merges.split()[0]})")
        checked = _inspect(git_dir, ref, base_sha)
        if not checked.log and task == "implement" and verdict == "built":
            raise BundleError("implement says built but the bundle has no commits")
        touched = [p for p in checked.files if p.startswith(PROTECTED)]
        if touched:
            raise BundleError(f"the bundle touches protected paths: {', '.join(touched)}")
        return checked

    def _judge_reach(
        self, repo: str, git_dir: Path, checked: Checked, base_sha: str, pr_base_sha: str,
        test_paths: Sequence[str],
    ) -> None:
        """Review's reach: its fixes stay inside the PR's own files and the
        repo's test paths (software-factory.md#the-runner-contract)."""
        self._fetch_base(repo, git_dir, pr_base_sha)
        names = _git(git_dir, "diff", "--name-only", "--no-renames", "-z", pr_base_sha, base_sha)
        pr_files = {n for n in names.split("\0") if n}
        outside = [p for p in checked.files if p not in pr_files and not matches(p, test_paths)]
        if outside:
            raise BundleError("review's fixes touch files outside the PR's own files and "
                              f"test_paths: {', '.join(outside)}")


def _inspect(git_dir: Path, ref: str, base_sha: str) -> Checked:
    log = _git(git_dir, "log", "--oneline", "--no-decorate", f"{base_sha}..{ref}")
    names = _git(git_dir, "log", "--format=", "--name-only", "--no-renames", "-z",
                 f"{base_sha}..{ref}")
    files = sorted({n.strip("\n") for n in names.split("\0") if n.strip("\n")})
    return Checked(log=log.splitlines(), files=files)


def _header(bundle: bytes) -> tuple[list[str], list[str]]:
    """The refs and prerequisite commits a bundle's header lists."""
    head, sep, _ = bundle.partition(b"\n\n")
    lines = head.decode("utf-8", "replace").split("\n")
    if not sep or lines[0] not in ("# v2 git bundle", "# v3 git bundle"):
        raise BundleError("not a git bundle")
    refs, prerequisites = [], []
    for line in lines[1:]:
        if line.startswith("@"):
            continue
        if line.startswith("-"):
            prerequisites.append(line[1:].split(" ", 1)[0])
        else:
            sha, _, name = line.partition(" ")
            if not name:
                raise BundleError(f"a bad line in the bundle's header: {line!r}")
            refs.append(name)
    return refs, prerequisites


def _run_ref(run_id: int) -> str:
    return f"refs/factory/runs/{run_id}"


def _git(git_dir: Path, *args: str, env: dict[str, str] | None = None) -> str:

    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    done = subprocess.run(["git", f"--git-dir={git_dir}", *args], env=full_env,
                          capture_output=True, text=True)
    if done.returncode != 0:
        error = GitError(f"git {args[0]} failed: {done.stderr.strip()[-500:]}")
        error.stderr = done.stderr
        raise error
    return done.stdout.strip("\n")


def matches(path: str, globs: Sequence[str]) -> bool:
    """Whether `path` matches any of `globs`, where `**` spans directories
    (as in `tests/**` or `**/*.test.ts`)."""

    p = PurePosixPath(path)
    return any(p.full_match(g) for g in globs)
