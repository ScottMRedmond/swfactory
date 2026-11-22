"""An in-memory GitHub REST API, plugged in as the HTTP transport.

It answers the calls the orchestrator makes as a GitHub App, enforcing what
real GitHub does: App endpoints need a JWT signed with the App's key,
everything else needs an installation token that hasn't expired and was
scoped to that repo. Every request is recorded in `.requests`.

    gh = FakeGitHub(clock=clock)
    repo = gh.add_repo("octo/widgets")
    repo.add_issue(7, "Title", "Body", labels=["agent:explore"])
    repo.files[".factory/config.yml"] = "setup: uv sync\ncheck: pytest\n"
    issue = repo.add_issue(7, "Title", "Body", labels=["agent:explore"])
    repo.permissions["alice"] = "write"     # anyone else has "none"
    issue.add_comment("alice", "A comment")  # the App comments as APP_LOGIN
    client = GitHub(AppAuth(APP_ID, PRIVATE_KEY, gh, clock=clock), gh)
"""

import base64
import fnmatch
import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from factory.github import Request, Response

APP_ID = "5877206"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_KEY = _KEY.public_key()

APP_SLUG = "factory"
APP_LOGIN = f"{APP_SLUG}[bot]"

TOKEN_LIFETIME = 3600


_USER_IDS: dict[str, int] = {}


def user_id(login: str) -> int:
    """The numeric GitHub user id the fake gives `login` (stable within a test run)."""
    return _USER_IDS.setdefault(login, 4_000_000 + len(_USER_IDS))


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Issue:
    number: int
    title: str
    body: str | None
    labels: list[str]
    state: str = "open"
    pull_request: bool = False
    comments: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    _ids = itertools.count(1_000_000)

    def add_comment(self, login: str, body: str, created_at: str = "2026-11-20T10:00:00Z") -> dict:
        comment = {"id": next(self._ids), "body": body, "user": {"login": login},
                   "created_at": created_at}
        self.comments.append(comment)
        return comment

    def add_event(self, event: str, login: str, created_at: str, **kw) -> dict:
        """An issue event such as `closed` or `reopened`, by `login`."""
        data = {"id": next(self._ids), "event": event,
                "actor": {"login": login, "id": user_id(login)},
                "created_at": created_at, **kw}
        self.events.append(data)
        return data

    def label(self, name: str, by: str, at: str = "2026-11-20T10:00:00Z") -> "Issue":
        """`by` adds the label `name`, recorded as a `labeled` event like GitHub does."""
        if name not in self.labels:
            self.labels.append(name)
        self.add_event("labeled", by, at, label={"name": name, "color": "1d76db"})
        return self

    def json(self, repo: str) -> dict[str, Any]:
        data = {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": [{"name": n} for n in self.labels],
            "html_url": f"https://github.com/{repo}/issues/{self.number}",
            "user": {"login": "someone"},
            "updated_at": _iso(self.updated_at),
        }
        if self.pull_request:
            data["pull_request"] = {"url": "..."}
        return data


@dataclass
class Repo:
    full_name: str
    installation_id: int
    default_branch: str = "main"
    head_sha: str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    issues: dict[int, Issue] = field(default_factory=dict)
    # Files on the default branch's head, by path.
    files: dict[str, str] = field(default_factory=dict)
    # Labels by name: {"name", "color", "description"}.
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    installed: bool = True
    permissions: dict[str, str] = field(default_factory=dict)
    # Active ruleset rules by fnmatch branch pattern, e.g. {"factory/*": [{"type": "creation"}]}.
    rules: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    clock: Any = field(default=lambda: 1_800_000_000.0, repr=False)

    def add_issue(self, number, title, body="", labels=(), **kw) -> Issue:
        kw.setdefault("updated_at", self.clock())
        issue = Issue(number, title, body, list(labels), **kw)
        self.issues[number] = issue
        return issue


class FakeGitHub:
    def __init__(self, clock=lambda: 1_800_000_000.0):
        self.clock = clock
        self.repos: dict[str, Repo] = {}
        self.requests: list[Request] = []
        self.tokens: dict[str, dict[str, Any]] = {}
        self.fail_next: list[Response] = []
        self._ids = itertools.count(1)

    def add_repo(self, full_name: str, installation_id: int = 100, **kw) -> Repo:
        repo = Repo(full_name, installation_id=installation_id, clock=self.clock, **kw)
        self.repos[full_name] = repo
        return repo

    def uninstall(self, full_name: str) -> None:
        """The App is removed from the repo: GitHub no longer lists or serves it."""
        self.repos[full_name].installed = False
        for token in self.tokens.values():
            if full_name in token["repos"]:
                token["repos"].remove(full_name)

    def _installed(self) -> list[Repo]:
        return [r for r in self.repos.values() if r.installed]

    # --- the transport -------------------------------------------------------

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if self.fail_next:
            return self.fail_next.pop(0)
        url = urlsplit(request.url)
        assert url.scheme == "https" and url.netloc == "api.github.com", request.url
        assert request.headers.get("Accept") == "application/vnd.github+json"
        assert request.headers.get("X-GitHub-Api-Version")
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = json.loads(request.body) if request.body else None
        self._request_headers = request.headers
        for method, pattern, handler, auth in self._routes():
            m = re.fullmatch(pattern, path)
            if m and method == request.method:
                args = [unquote(a) for a in m.groups()]
                if auth == "jwt":
                    error = self._check_jwt(request)
                elif auth == "installation":
                    error, args = self._check_installation_token(request)
                else:
                    error = self._check_token(request, f"{args[0]}/{args[1]}")
                if error:
                    return error
                return handler(*args, query=query, body=body)
        return _json(404, {"message": "Not Found"})

    def _routes(self):
        repo = r"/repos/([^/]+)/([^/]+)"
        return [
            ("GET", r"/app/installations", self._installations, "jwt"),
            ("GET", r"/installation/repositories", self._installation_repos, "installation"),
            ("GET", r"/app", self._app, "jwt"),
            ("GET", repo + "/installation", self._installation, "jwt"),
            ("POST", r"/app/installations/(\d+)/access_tokens", self._access_token, "jwt"),
            ("GET", repo, self._repo, "token"),
            ("GET", repo + r"/branches/([^/]+)", self._branch, "token"),
            ("GET", repo + "/issues", self._list_issues, "token"),
            ("GET", repo + r"/issues/(\d+)", self._get_issue, "token"),
            ("DELETE", repo + r"/issues/(\d+)/labels/(.+)", self._remove_label, "token"),
            ("POST", repo + r"/issues/(\d+)/comments", self._comment, "token"),
            ("GET", repo + r"/contents/(.+)", self._contents, "token"),
            ("GET", repo + "/labels", self._list_labels, "token"),
            ("POST", repo + "/labels", self._create_label, "token"),
            ("GET", repo + r"/issues/(\d+)/comments", self._list_comments, "token"),
            ("GET", repo + r"/issues/(\d+)/events", self._list_events, "token"),
            ("GET", repo + r"/collaborators/([^/]+)/permission", self._permission, "token"),
            ("GET", repo + r"/rules/branches/(.+)", self._branch_rules, "token"),
        ]

    def _check_jwt(self, request: Request) -> Response | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _json(401, {"message": "A JSON web token could not be decoded"})
        try:
            claims = jwt.decode(
                auth.removeprefix("Bearer "),
                PUBLIC_KEY,
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return _json(401, {"message": "A JSON web token could not be decoded"})
        now = self.clock()
        if str(claims["iss"]) != APP_ID or not claims["iat"] <= now < claims["exp"]:
            return _json(401, {"message": "'Expiration time' claim ('exp') is too far in the future"})
        if claims["exp"] - claims["iat"] > 600:
            return _json(401, {"message": "'Expiration time' claim ('exp') is too far in the future"})
        return None

    def _check_token(self, request: Request, repo: str) -> Response | None:
        auth = request.headers.get("Authorization", "")
        token = self.tokens.get(auth.removeprefix("Bearer "))
        if token is None or token["expires"] <= self.clock():
            return _json(401, {"message": "Bad credentials"})
        if repo not in token["repos"]:
            return _json(404, {"message": "Not Found"})
        return None

    def _check_installation_token(self, request: Request):
        auth = request.headers.get("Authorization", "")
        token = self.tokens.get(auth.removeprefix("Bearer "))
        if token is None or token["expires"] <= self.clock():
            return _json(401, {"message": "Bad credentials"}), []
        return None, [token]

    # --- handlers -------------------------------------------------------------

    def _installations(self, *, query, body):
        installed = self._installed()
        ids = sorted({r.installation_id for r in installed})
        data = [
            {"id": i, "account": {"login": next(r.full_name.split("/")[0] for r in installed
                                                if r.installation_id == i)}}
            for i in ids
        ]
        return _page("/app/installations", query, data)

    def _installation_repos(self, token, *, query, body):
        repos = [self.repos[n] for n in sorted(token["repos"]) if self.repos[n].installed]
        page = _page("/installation/repositories", query,
                     [{"full_name": r.full_name, "default_branch": r.default_branch} for r in repos])
        return _json(page.status, {"total_count": len(repos), "repositories": page.json()},
                     page.headers)
    def _app(self, *, query, body):
        return _json(200, {"id": int(APP_ID), "slug": APP_SLUG, "name": "Factory"})

    def _installation(self, owner, name, *, query, body):
        repo = self.repos.get(f"{owner}/{name}")
        if repo is None or not repo.installed:
            return _json(404, {"message": "Not Found"})
        return _json(200, {"id": repo.installation_id, "account": {"login": owner}})

    def _access_token(self, installation_id, *, query, body):
        installed = [r for r in self._installed() if r.installation_id == int(installation_id)]
        owner = installed[0].full_name.split("/")[0]
        names = (body or {}).get("repositories")
        repos = [r.full_name for r in installed if names is None or r.full_name.split("/")[1] in names]
        token = f"ghs_fake{next(self._ids):04d}{'x' * 28}"
        expires = self.clock() + TOKEN_LIFETIME
        self.tokens[token] = {"repos": repos, "expires": expires, "owner": owner,
                              "permissions": (body or {}).get("permissions")}
        return _json(
            201,
            {
                "token": token,
                "expires_at": _iso(expires),
                "repository_selection": "selected",
            },
        )

    def _repo(self, owner, name, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        return _json(200, {"full_name": repo.full_name, "default_branch": repo.default_branch})

    def _branch(self, owner, name, branch, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        if branch != repo.default_branch:
            return _json(404, {"message": "Branch not found"})
        return _json(200, {"name": branch, "commit": {"sha": repo.head_sha}})

    def _list_issues(self, owner, name, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        state = query.get("state", "open")
        wanted = [l for l in query.get("labels", "").split(",") if l]
        since = _parse_iso(query["since"]) if "since" in query else None
        if query.get("sort") == "updated":
            order = sorted(repo.issues.values(), key=lambda i: (-i.updated_at, -i.number))
        else:
            order = sorted(repo.issues.values(), key=lambda i: -i.number)
        issues = [
            i
            for i in order
            if (state == "all" or i.state == state)
            and all(l in i.labels for l in wanted)
            and (since is None or i.updated_at >= since)
        ]
        response = _page(f"/repos/{owner}/{name}/issues", query, [i.json(repo.full_name) for i in issues])
        # Like GitHub: the ETag is a hash of the body, and a matching If-None-Match gets a 304.
        etag = f'W/"{hashlib.sha256(response.body).hexdigest()[:32]}"'
        if self._request_headers.get("If-None-Match") == etag:
            return Response(304, {"ETag": etag}, b"")
        return Response(response.status, {**response.headers, "ETag": etag}, response.body)

    def _list_comments(self, owner, name, number, *, query, body):
        issue = self.repos[f"{owner}/{name}"].issues[int(number)]
        return _page(f"/repos/{owner}/{name}/issues/{number}/comments", query, issue.comments)

    def _list_events(self, owner, name, number, *, query, body):
        issue = self.repos[f"{owner}/{name}"].issues[int(number)]
        return _page(f"/repos/{owner}/{name}/issues/{number}/events", query, issue.events)

    def _permission(self, owner, name, login, *, query, body):
        permission = self.repos[f"{owner}/{name}"].permissions.get(login)
        if permission is None:
            return _json(404, {"message": f"{login} is not a user"})
        return _json(200, {"permission": permission, "user": {"login": login}})

    def _branch_rules(self, owner, name, branch, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        rules = [
            {"ruleset_source_type": "Repository", "ruleset_source": repo.full_name,
             "ruleset_id": 42, **rule}
            for pattern, listed in repo.rules.items() if fnmatch.fnmatchcase(branch, pattern)
            for rule in listed
        ]
        return _page(f"/repos/{owner}/{name}/rules/branches/{branch}", query, rules)

    def _get_issue(self, owner, name, number, *, query, body):
        issue = self.repos[f"{owner}/{name}"].issues.get(int(number))
        if issue is None:
            return _json(404, {"message": "Not Found"})
        return _json(200, issue.json(f"{owner}/{name}"))

    def _remove_label(self, owner, name, number, label, *, query, body):
        issue = self.repos[f"{owner}/{name}"].issues[int(number)]
        if label not in issue.labels:
            return _json(404, {"message": "Label does not exist"})
        issue.labels.remove(label)
        issue.updated_at = self.clock()
        return _json(200, [{"name": n} for n in issue.labels])

    def _comment(self, owner, name, number, *, query, body):
        issue = self.repos[f"{owner}/{name}"].issues[int(number)]
        comment = issue.add_comment(APP_LOGIN, body["body"], created_at=_iso(self.clock()))
        issue.updated_at = self.clock()
        return _json(201, comment)


    def _contents(self, owner, name, path, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        ref = query.get("ref", repo.default_branch)
        text = repo.files.get(path)
        if ref not in (repo.default_branch, repo.head_sha) or text is None:
            return _json(404, {"message": "Not Found"})
        return _json(200, {"type": "file", "path": path, "encoding": "base64",
                           "content": base64.encodebytes(text.encode()).decode()})

    def _list_labels(self, owner, name, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        return _page(f"/repos/{owner}/{name}/labels", query, list(repo.labels.values()))

    def _create_label(self, owner, name, *, query, body):
        repo = self.repos[f"{owner}/{name}"]
        if body["name"] in repo.labels:
            return _json(422, {"message": "Validation Failed",
                               "errors": [{"resource": "Label", "code": "already_exists"}]})
        repo.labels[body["name"]] = {"name": body["name"], "color": body.get("color", "ededed"),
                                     "description": body.get("description", "")}
        return _json(201, repo.labels[body["name"]])


def _page(path: str, query: dict[str, str], items: list[Any]) -> Response:
    """One page of `items`, with a `Link: rel="next"` header when there's more."""
    per_page = int(query.get("per_page", 30))
    page = int(query.get("page", 1))
    headers = {}
    if page * per_page < len(items):
        nxt = f"https://api.github.com{path}?" + "&".join(
            f"{k}={v}" for k, v in {**query, "page": page + 1}.items()
        )
        headers["Link"] = f'<{nxt}>; rel="next"'
    return _json(200, items[(page - 1) * per_page : page * per_page], headers)


ENROLMENT = "setup: uv sync\ncheck: uv run pytest\n"


def enrol(repo: Repo, config: str = ENROLMENT) -> Repo:
    """Put `.factory/config.yml` on the repo's default branch."""
    repo.files[".factory/config.yml"] = config
    return repo


def discover(conn, fake: "FakeGitHub") -> None:
    """Run enrolment discovery against `fake` with its own client (so it shares
    no cached tokens with the test's), then forget the requests it made."""
    from factory import enrolment
    from factory.github import AppAuth, GitHub

    github = GitHub(AppAuth(APP_ID, PRIVATE_KEY, fake, clock=fake.clock), fake)
    enrolment.refresh(conn, github, factory_repo="octo/factory")
    fake.requests.clear()


def _parse_iso(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _json(status: int, data: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(data).encode())
