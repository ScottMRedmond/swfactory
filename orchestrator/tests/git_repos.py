"""Real git repos for tests: a "GitHub" remote (a local bare repo) and a
runner-side clone that makes commits and bundles them like the run script does
(`git bundle create <file> <base>..refs/heads/<branch>`)."""

import subprocess
from pathlib import Path

ENV = {
    "GIT_AUTHOR_NAME": "factory[bot]", "GIT_AUTHOR_EMAIL": "bot@example.com",
    "GIT_COMMITTER_NAME": "factory[bot]", "GIT_COMMITTER_EMAIL": "bot@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class Remote:
    """A bare repo standing in for the target repo on GitHub, with one commit on main."""

    def __init__(self, root: Path):
        self.path = root / "remote.git"
        self.path.mkdir(parents=True)
        git(self.path, "init", "-q", "--bare", "-b", "main")
        # GitHub lets a client fetch any commit reachable from a ref by its sha.
        git(self.path, "config", "uploadpack.allowReachableSHA1InWant", "true")
        self.url = self.path.as_uri()
        self.work = root / "work"
        git(root, "clone", "-q", self.url, str(self.work))
        self.commit({"README.md": "hello\n"}, "Initial commit")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.base = self.head()

    def head(self, ref: str = "HEAD") -> str:
        return git(self.work, "rev-parse", ref)

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(self.work, "add", name)
        git(self.work, "commit", "-q", "-m", message)
        return self.head()

    def branch(self, name: str, base: str | None = None) -> None:
        git(self.work, "checkout", "-q", "-B", name, base or self.base)

    def bundle(self, branch: str, base: str | None = None) -> bytes:
        """`base..refs/heads/<branch>`, as the run script bundles it."""
        out = self.work.parent / "out.bundle"
        git(self.work, "bundle", "create", "-q", str(out), f"{base or self.base}..refs/heads/{branch}")
        return out.read_bytes()

    def remote_branch(self, name: str) -> str | None:
        found = git(self.path, "for-each-ref", "--format=%(objectname)", f"refs/heads/{name}")
        return found or None
