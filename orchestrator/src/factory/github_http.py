"""The narrow HTTP seam the GitHub-facing code talks through.

`HttpClient` sends one request and returns whatever came back, any status
included; it raises `OSError` (or a subclass) only when there's no answer at
all. It never follows redirects, so a caller decides whether a redirect gets
the token. Tests replace it with a recorded fake.

`TokenSource` hands out a short-lived token for one repo (the GitHub App's
installation token, see orchestrator.md#identity). Nothing here makes one.
"""

import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


class HttpClient(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None = None
    ) -> HttpResponse: ...


class TokenSource(Protocol):
    def token(self, repo: str) -> str:
        """A token that may act on `repo` ("owner/name")."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrllibHttp:
    """An `HttpClient` on the standard library."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None = None
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            # Error statuses and the unfollowed redirects land here.
            with e:
                return HttpResponse(e.code, dict(e.headers.items()) if e.headers else {}, e.read())
