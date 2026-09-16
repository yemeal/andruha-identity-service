"""Controlled HTTP peer for Identity integration tests; not a User Profile emulator."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


class ProfilePeer:
    def __init__(self) -> None:
        self.status = 204
        self.requests: list[tuple[str, dict[str, str]]] = []


@contextmanager
def serve_profile_peer(peer: ProfilePeer) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            peer.requests.append((self.path, payload))
            status = (
                peer.status
                if self.headers.get("X-Service-Token") == "integration-profile-token"
                else 401
            )
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
