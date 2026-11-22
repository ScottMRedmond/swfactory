"""The real transport, against local HTTP servers."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from factory.github import Request, urllib_transport


def serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.headers.get("Authorization", "none").encode())


@pytest.fixture
def servers():
    blob = serve(Echo)

    class Redirect(Echo):
        def do_GET(self):
            if self.path == "/elsewhere":
                target = f"http://localhost:{blob.server_port}/blob"
            elif self.path == "/here":
                target = "/same"
            else:
                return super().do_GET()
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

    api = serve(Redirect)
    yield f"http://127.0.0.1:{api.server_port}"
    api.shutdown()
    blob.shutdown()


def test_a_redirect_to_another_host_drops_the_token(servers):
    # The artifacts API redirects to pre-signed blob storage, which mustn't see the token.
    resp = urllib_transport(Request("GET", servers + "/elsewhere", {"Authorization": "Bearer t"}))
    assert (resp.status, resp.body) == (200, b"none")


def test_a_redirect_on_the_same_host_keeps_the_token(servers):
    resp = urllib_transport(Request("GET", servers + "/here", {"Authorization": "Bearer t"}))
    assert (resp.status, resp.body) == (200, b"Bearer t")
