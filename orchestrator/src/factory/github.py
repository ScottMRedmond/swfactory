"""GitHub, as the factory's GitHub App.

Every call goes through a `Transport`: a function from `Request` to
`Response`. `urllib_transport` talks to api.github.com; tests plug in an
in-memory GitHub instead.

Tokens are short-lived installation tokens scoped to one repo. They are never
logged, and never appear in an exception's message or a repr.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import jwt

API = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "swfactory-orchestrator"

# Fetch a new token this long before the old one expires (they last an hour).
TOKEN_MARGIN = 5 * 60


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(repr=False)
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


Transport = Callable[[Request], Response]


def urllib_transport(request: Request, timeout: float = 30) -> Response:
    req = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method=request.method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers.items()), e.read())


class GitHubError(Exception):
    """GitHub answered with an error. The message never holds a token."""

    def __init__(self, request: Request, response: Response):
        self.status = response.status
        try:
            message = (response.json() or {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        super().__init__(f"{request.method} {request.url.split('?')[0]}: {response.status} {message}")


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }


def _send(transport: Transport, token: str, method: str, url: str, body: Any = None) -> Response:
    data = None
    headers = _headers(token)
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = Request(method, url, headers, data)
    response = transport(request)
    if response.status >= 400:
        raise GitHubError(request, response)
    return response


class AppAuth:
    """Installation tokens for the App, one per repo, cached until near expiry."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        transport: Transport,
        clock: Callable[[], float] = time.time,
    ):
        self._app_id = str(app_id)
        self._key = private_key
        self._transport = transport
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}

    def __repr__(self) -> str:
        return f"AppAuth(app_id={self._app_id})"

    def _jwt(self) -> str:
        now = int(self._clock())
        # iat is backdated a minute for clock drift; GitHub allows at most 10 minutes.
        claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def app(self) -> dict[str, Any]:
        """The App itself (`id`, `slug`, ...), fetched with the App's JWT."""
        return _send(self._transport, self._jwt(), "GET", f"{API}/app").json()

    def token(self, repo: str) -> str:
        cached = self._tokens.get(repo)
        if cached and cached[1] - TOKEN_MARGIN > self._clock():
            return cached[0]
        app_jwt = self._jwt()
        installation = _send(self._transport, app_jwt, "GET", f"{API}/repos/{repo}/installation").json()
        data = _send(
            self._transport,
            app_jwt,
            "POST",
            f"{API}/app/installations/{installation['id']}/access_tokens",
            {"repositories": [repo.split("/", 1)[1]]},
        ).json()
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[repo] = (data["token"], expires)
        return data["token"]


class GitHub:
    """The REST calls the orchestrator makes, each with a token for the repo it touches."""

    def __init__(self, auth: AppAuth, transport: Transport):
        self._auth = auth
        self._transport = transport
        self._app_login: str | None = None

    def __repr__(self) -> str:
        return f"GitHub({self._auth!r})"

    def _call(self, repo: str, method: str, path: str, body: Any = None, **query: Any) -> Response:
        url = f"{API}/repos/{repo}{path}"
        if query:
            url += "?" + urlencode(query)
        return _send(self._transport, self._auth.token(repo), method, url, body)

    def default_branch_head(self, repo: str) -> str:
        """The commit at the head of the repo's default branch."""
        branch = self._call(repo, "GET", "").json()["default_branch"]
        return self._call(repo, "GET", f"/branches/{quote(branch, safe='')}").json()["commit"]["sha"]

    def _pages(self, repo: str, path: str, **query: Any) -> Iterator[dict[str, Any]]:
        """Every item of a paged list, fetched a page at a time as they're consumed."""
        page = 1
        while True:
            response = self._call(repo, "GET", path, per_page=100, page=page, **query)
            yield from response.json()
            if 'rel="next"' not in response.headers.get("Link", ""):
                return
            page += 1

    def app_login(self) -> str:
        """The login the App comments and acts as, e.g. `factory[bot]`."""
        if self._app_login is None:
            self._app_login = f"{self._auth.app()['slug']}[bot]"
        return self._app_login

    def open_issues_with_label(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Open issues and PRs carrying `label` (a PR has a `pull_request` key)."""
        return list(self._pages(repo, "/issues", state="open", labels=label))

    def open_issues_newest_first(self, repo: str) -> Iterator[dict[str, Any]]:
        """Open issues and PRs, newest first, fetched a page at a time as consumed."""
        return self._pages(repo, "/issues", state="open", sort="created", direction="desc")

    def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """An issue's or PR's comments, oldest first."""
        return list(self._pages(repo, f"/issues/{number}/comments"))

    def issue_events(self, repo: str, number: int) -> list[dict[str, Any]]:
        """An issue's events (`closed`, `reopened`, `labeled`, ...), oldest first."""
        return list(self._pages(repo, f"/issues/{number}/events"))

    def permission(self, repo: str, login: str) -> str:
        """`admin`, `write` (maintain shows as write), `read` or `none`."""
        try:
            path = f"/collaborators/{quote(login, safe='')}/permission"
            return self._call(repo, "GET", path).json()["permission"]
        except GitHubError as e:
            if e.status == 404:
                return "none"
            raise

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._call(repo, "GET", f"/issues/{number}").json()

    def remove_label(self, repo: str, number: int, label: str) -> None:
        """Take a label off an issue or PR. A label that's already gone is fine."""
        try:
            self._call(repo, "DELETE", f"/issues/{number}/labels/{quote(label, safe='')}")
        except GitHubError as e:
            if e.status != 404:
                raise

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self._call(repo, "POST", f"/issues/{number}/comments", {"body": body})
