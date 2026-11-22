"""The standard-library HTTP client, against a local server."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from factory.github_http import UrllibHttp


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
        elif self.path == "/gone":
            self.send_response(410)
            self.end_headers()
            self.wfile.write(b'{"message": "gone"}')
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.headers.get("Authorization", "").encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_sends_headers_and_body(server):
    http = UrllibHttp()
    assert http.request("GET", server + "/", headers={"Authorization": "Bearer x"}).body == b"Bearer x"
    assert http.request("POST", server + "/", headers={}, body=b"hello").body == b"hello"


def test_error_statuses_are_answers_not_exceptions(server):
    resp = UrllibHttp().request("GET", server + "/gone", headers={})
    assert (resp.status, resp.body) == (410, b'{"message": "gone"}')


def test_redirects_are_returned_not_followed(server):
    resp = UrllibHttp().request("GET", server + "/redirect", headers={"Authorization": "Bearer x"})
    assert resp.status == 302
    assert resp.header("location") == "/elsewhere"


def test_no_answer_raises_oserror():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(OSError):
        UrllibHttp(timeout=2).request("GET", f"http://127.0.0.1:{port}/", headers={})
