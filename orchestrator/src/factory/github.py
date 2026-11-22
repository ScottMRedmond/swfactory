"""GitHub, as the factory's GitHub App.

Every call goes through a `Transport`: a function from `Request` to
`Response`. `urllib_transport` talks to api.github.com; tests plug in an
in-memory GitHub instead.

Tokens are short-lived installation tokens scoped to one repo. They are never
logged, and never appear in an exception's message or a repr.
"""

import base64
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import jwt

API = "https://api.github.com"
WEB = "https://github.com"  # where git reaches repos
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


class _TokenStaysHome(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but don't carry the token to another host.

    The artifacts API redirects to pre-signed blob storage; urllib would
    otherwise forward the Authorization header there.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_TokenStaysHome)


def urllib_transport(request: Request, timeout: float = 30) -> Response:
    req = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method=request.method
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers.items()), e.read())


class GitHubError(Exception):
    """GitHub answered with an error. The message never holds a token."""

    def __init__(self, request: Request, response: Response):
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.headers.items()}
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


def _send(
    transport: Transport,
    token: str,
    method: str,
    url: str,
    body: Any = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    data = None
    headers = {**_headers(token), **(extra_headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = Request(method, url, headers, data)
    response = transport(request)
    if response.status >= 400:
        raise GitHubError(request, response)
    return response


def _paged(transport: Transport, token: str, url: str, key: str | None = None) -> list[Any]:
    """Every item of a paginated list, following the `Link: rel="next"` header."""
    items: list[Any] = []
    url = f"{url}?per_page=100"
    while True:
        response = _send(transport, token, "GET", url)
        data = response.json()
        items += data[key] if key else data
        match = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
        if match is None:
            return items
        url = match.group(1)


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

    def forget(self) -> None:
        """Drop every cached token, so the next call for each repo fetches a fresh one."""
        self._tokens.clear()

    def _jwt(self) -> str:
        now = int(self._clock())
        # iat is backdated a minute for clock drift; GitHub allows at most 10 minutes.
        claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def app(self) -> dict[str, Any]:
        """The App itself (`id`, `slug`, ...), fetched with the App's JWT."""
        return _send(self._transport, self._jwt(), "GET", f"{API}/app").json()

    def installation(self, repo: str) -> dict[str, Any] | None:
        """The App's installation on `repo`, or None if it isn't installed there."""
        try:
            return _send(self._transport, self._jwt(), "GET", f"{API}/repos/{repo}/installation").json()
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

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

    def installed_repos(self) -> list[str]:
        """Every repo the App is installed on, across all its installations.

        Listing an installation's repos takes a token for the whole
        installation; it's asked for with only `metadata: read`, used for the
        listing and dropped.
        """
        app_jwt = self._jwt()
        repos: list[str] = []
        for installation in _paged(self._transport, app_jwt, f"{API}/app/installations"):
            data = _send(
                self._transport,
                app_jwt,
                "POST",
                f"{API}/app/installations/{installation['id']}/access_tokens",
                {"permissions": {"metadata": "read"}},
            ).json()
            for page in _paged(
                self._transport, data["token"], f"{API}/installation/repositories", key="repositories"
            ):
                repos.append(page["full_name"])
        return repos


CLOSING_ISSUES = """query ClosingIssues($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { closingIssuesReferences(first: 50) { nodes { number } } }
  }
}"""
MARK_READY = """mutation MarkReady($id: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } }
}"""
MINIMIZE_COMMENT = """mutation MinimizeComment($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { minimizedComment { isMinimized } }
}"""


class GitHub:
    """The REST calls the orchestrator makes, each with a token for the repo it touches."""

    def __init__(
        self,
        auth: AppAuth,
        transport: Transport,
        git_url: Callable[[str], str] = lambda repo: f"{WEB}/{repo}.git",
    ):
        self._auth = auth
        self._transport = transport
        self._git_url = git_url
        self._app_login: str | None = None

    def __repr__(self) -> str:
        return f"GitHub({self._auth!r})"

    def forget_tokens(self) -> None:
        self._auth.forget()

    def _call(
        self,
        repo: str,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        **query: Any,
    ) -> Response:
        url = f"{API}/repos/{repo}{path}"
        if query:
            url += "?" + urlencode(query)
        return _send(self._transport, self._auth.token(repo), method, url, body, headers)

    def _pages(self, repo: str, first: Response, **query: Any) -> list[dict[str, Any]]:
        """The first page's items plus those of every page after it."""
        items: list[dict[str, Any]] = list(first.json())
        response, page = first, 1
        while 'rel="next"' in response.headers.get("Link", ""):
            page += 1
            response = self._call(repo, "GET", "/issues", **query, page=page)
            items += response.json()
        return items

    def default_branch(self, repo: str) -> str:
        return self._call(repo, "GET", "").json()["default_branch"]

    def default_branch_head(self, repo: str) -> str:
        """The commit at the head of the repo's default branch."""
        branch = self.default_branch(repo)
        return self._call(repo, "GET", f"/branches/{quote(branch, safe='')}").json()["commit"]["sha"]

    def _each(self, repo: str, path: str, **query: Any) -> Iterator[dict[str, Any]]:
        """Every item of the paged list at `path`, fetched a page at a time as they're consumed."""
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
        return list(self._each(repo, "/issues", state="open", labels=label))

    def open_issues_newest_first(self, repo: str) -> Iterator[dict[str, Any]]:
        """Open issues and PRs, newest first, fetched a page at a time as consumed."""
        return self._each(repo, "/issues", state="open", sort="created", direction="desc")

    def issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        """An issue's or PR's comments, oldest first."""
        return list(self._each(repo, f"/issues/{number}/comments"))

    def issue_events(self, repo: str, number: int) -> list[dict[str, Any]]:
        """An issue's events (`closed`, `reopened`, `labeled`, ...), oldest first."""
        return list(self._each(repo, f"/issues/{number}/events"))

    def permission(self, repo: str, login: str) -> str:
        """`admin`, `write` (maintain shows as write), `read` or `none`."""
        try:
            path = f"/collaborators/{quote(login, safe='')}/permission"
            return self._call(repo, "GET", path).json()["permission"]
        except GitHubError as e:
            if e.status == 404:
                return "none"
            raise
    def open_issues(self, repo: str) -> list[dict[str, Any]]:
        """Every open issue and PR (a PR has a `pull_request` key)."""
        query = {"state": "open", "per_page": 100}
        return self._pages(repo, self._call(repo, "GET", "/issues", **query), **query)

    def issues_since(
        self, repo: str, since: str, etag: str | None
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Issues and PRs, open or closed, updated at or after `since` (ISO 8601).

        Sent with `etag` as If-None-Match: if nothing changed GitHub answers
        304, which doesn't count against the rate limit, and this returns
        `(None, etag)`. Otherwise it returns the issues and the new ETag.
        """
        query = {"state": "all", "since": since, "sort": "updated",
                 "per_page": 100}
        headers = {"If-None-Match": etag} if etag else None
        first = self._call(repo, "GET", "/issues", headers=headers, **query)
        if first.status == 304:
            return None, etag
        new_etag = next((v for k, v in first.headers.items() if k.lower() == "etag"), None)
        return self._pages(repo, first, **query), new_etag

    def issues_with_label(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Issues and PRs carrying `label`, open or closed."""
        return list(self._each(repo, "/issues", state="all", labels=label))

    def create_issue(
        self, repo: str, title: str, body: str, labels: list[str],
        assignees: tuple[str, ...] = (),
    ) -> int:
        """Open an issue; returns its number. If GitHub won't take the assignees
        (a 422, e.g. one without access to the repo), it's opened without them."""
        data: dict[str, Any] = {"title": title, "body": body, "labels": labels}
        if assignees:
            try:
                return int(self._call(repo, "POST", "/issues",
                                      {**data, "assignees": list(assignees)}).json()["number"])
            except GitHubError as e:
                if e.status != 422:
                    raise
        return int(self._call(repo, "POST", "/issues", data).json()["number"])

    def close_issue(self, repo: str, number: int) -> None:
        """Close an issue as completed."""
        self._call(repo, "PATCH", f"/issues/{number}",
                   {"state": "closed", "state_reason": "completed"})

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return self._call(repo, "GET", f"/issues/{number}").json()

    def remove_label(self, repo: str, number: int, label: str) -> None:
        """Take a label off an issue or PR. A label that's already gone is fine."""
        try:
            self._call(repo, "DELETE", f"/issues/{number}/labels/{quote(label, safe='')}")
        except GitHubError as e:
            if e.status != 404:
                raise

    def post_comment(self, repo: str, number: int, body: str) -> int:
        """Comment on an issue or PR; returns the new comment's id."""
        return int(self._call(repo, "POST", f"/issues/{number}/comments", {"body": body}).json()["id"])

    def edit_comment(self, repo: str, comment_id: int, body: str) -> None:
        """Replace a comment's body. Raises `GitHubError` (404) if it's gone."""
        self._call(repo, "PATCH", f"/issues/comments/{comment_id}", {"body": body})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        """Add labels to an issue or PR, keeping the ones it has."""
        self._call(repo, "POST", f"/issues/{number}/labels", {"labels": labels})

    def close_as_duplicate(self, repo: str, number: int) -> None:
        """Close an issue with the reason `duplicate`, or `not_planned` if GitHub
        doesn't take that reason (it was added in 2025)."""
        try:
            self._call(repo, "PATCH", f"/issues/{number}",
                       {"state": "closed", "state_reason": "duplicate"})
        except GitHubError as e:
            if e.status != 422:
                raise
            self._call(repo, "PATCH", f"/issues/{number}",
                       {"state": "closed", "state_reason": "not_planned"})

    def open_draft_pull(self, repo: str, *, head: str, base: str, title: str, body: str) -> int:
        """Open a draft PR from branch `head` into `base` and return its number.
        If `head` already has an open PR (opened before a crash), that one's."""
        try:
            return self._call(repo, "POST", "/pulls", {"title": title, "head": head, "base": base,
                                                      "body": body, "draft": True}).json()["number"]
        except GitHubError as e:
            if e.status != 422:
                raise
            owner = repo.split("/")[0]
            found = self._call(repo, "GET", "/pulls", state="open", head=f"{owner}:{head}").json()
            if not found:
                raise
            return found[0]["number"]

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        """A PR as the pulls API gives it (`node_id`, `draft`, `head`, `base`, ...)."""
        return self._call(repo, "GET", f"/pulls/{number}").json()

    def pull_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        """The files PR `number` changes, each with its `patch` (absent for a
        binary or very large file)."""
        return list(self._each(repo, f"/pulls/{number}/files"))

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        """The reviews posted on PR `number`."""
        return list(self._each(repo, f"/pulls/{number}/reviews"))

    def comment_review(
        self, repo: str, number: int, *, body: str, comments: list[dict[str, Any]],
        commit_id: str | None = None,
    ) -> bool:
        """Post one review with the `COMMENT` event (never approving or asking
        for changes) holding inline `comments` ({path, line, side, body}).
        False if GitHub rejects it (422: e.g. a comment isn't on the diff)."""
        review: dict[str, Any] = {"event": "COMMENT", "body": body, "comments": comments}
        if commit_id:
            review["commit_id"] = commit_id
        try:
            self._call(repo, "POST", f"/pulls/{number}/reviews", review)
        except GitHubError as e:
            if e.status != 422:
                raise
            return False
        return True

    def request_review(self, repo: str, number: int, login: str) -> bool:
        """Ask `login` to review PR `number`. False if GitHub won't (422: e.g.
        they're not a collaborator, or they opened it)."""
        try:
            self._call(repo, "POST", f"/pulls/{number}/requested_reviewers",
                       {"reviewers": [login]})
        except GitHubError as e:
            if e.status != 422:
                raise
            return False
        return True

    def graphql(self, repo: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """A GraphQL call with `repo`'s token; returns `data`. GraphQL reports
        errors in a 200 answer, which are raised as `GitHubError` too."""
        response = _send(self._transport, self._auth.token(repo), "POST", f"{API}/graphql",
                         {"query": query, "variables": variables})
        data = response.json()
        if data.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in data["errors"])
            raise GitHubError(Request("POST", f"{API}/graphql", {}, None),
                              Response(422, {}, json.dumps({"message": messages}).encode()))
        return data["data"]

    def closing_issues(self, repo: str, number: int) -> list[int]:
        """The issues PR `number` closes when merged (GitHub's closing references)."""
        owner, name = repo.split("/")
        data = self.graphql(repo, CLOSING_ISSUES, {"owner": owner, "name": name, "number": number})
        refs = data["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        return [n["number"] for n in refs]

    def mark_ready(self, repo: str, number: int) -> None:
        """Take PR `number` out of draft. A PR that's already ready is fine."""
        pull = self.pull(repo, number)
        if pull.get("draft"):
            self.graphql(repo, MARK_READY, {"id": pull["node_id"]})

    def minimize_comment(self, repo: str, node_id: str) -> None:
        """Hide a comment as outdated."""
        self.graphql(repo, MINIMIZE_COMMENT, {"id": node_id})

    def git_remote(self, repo: str) -> tuple[str, dict[str, str]]:
        """The repo's URL for git, and the environment that makes git push and
        fetch as the App. The token travels in the environment, never in an
        argument, so it can't show up in a process list or an error message."""
        basic = base64.b64encode(f"x-access-token:{self._auth.token(repo)}".encode()).decode()
        return self._git_url(repo), {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{WEB}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }

    def installed_repos(self) -> list[str]:
        """Every repo the App is installed on."""
        return self._auth.installed_repos()

    def app_installed(self, repo: str) -> bool:
        """Whether the App is installed on `repo` (with access to it)."""
        return self._auth.installation(repo) is not None

    def branch_rules(self, repo: str, branch: str) -> list[dict[str, Any]]:
        """The active ruleset rules that apply to a branch name (which needn't
        exist), each `{type, parameters?, ruleset_source, ruleset_id, ...}`."""
        url = f"{API}/repos/{repo}/rules/branches/{quote(branch, safe='')}"
        return _paged(self._transport, self._auth.token(repo), url)

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        """A file's text at `ref` (a branch or commit), or None if there's no such file."""
        try:
            data = self._call(repo, "GET", f"/contents/{quote(path)}", ref=ref).json()
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        return base64.b64decode(data["content"]).decode()

    def labels(self, repo: str) -> set[str]:
        """The names of the repo's labels."""
        url = f"{API}/repos/{repo}/labels"
        return {label["name"] for label in _paged(self._transport, self._auth.token(repo), url)}

    def create_label(self, repo: str, name: str, color: str, description: str) -> None:
        """Create a label. One that already exists is left as it is."""
        try:
            self._call(repo, "POST", "/labels",
                       {"name": name, "color": color, "description": description})
        except GitHubError as e:
            if e.status != 422:
                raise
