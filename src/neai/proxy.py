from __future__ import annotations

import http.client
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO
from urllib.parse import urlsplit

from .database import Database
from .errors import NeaiError, error_envelope
from .server import HOST, MAX_BODY_BYTES
from .store import list_instances, now, require_app

DEFAULT_PROXY_PORT = 8800
REFRESH_SECONDS = 2.0
DOWN_SECONDS = 5.0
BACKEND_TIMEOUT_SECONDS = 30
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}


class Backends:
    """The app's live instances, read from the database, handed out round-robin."""

    def __init__(self, db: Database, app: str):
        self.db = db
        self.app = app
        self._lock = threading.Lock()
        self._urls: list[str] = []
        self._fetched = float("-inf")
        self._turn = 0
        self._down_until: dict[str, float] = {}

    def in_turn(self) -> list[str]:
        with self._lock:
            if time.monotonic() - self._fetched > REFRESH_SECONDS:
                try:
                    self._urls = [i["url"] for i in list_instances(self.db, self.app)]
                except NeaiError:
                    pass  # keep the last known list while the database is away
                self._fetched = time.monotonic()
            up = [u for u in self._urls if self._down_until.get(u, 0) <= time.monotonic()] or self._urls
            if not up:
                return []
            start = self._turn % len(up)
            self._turn += 1
            return up[start:] + up[:start]

    def mark_down(self, url: str) -> None:
        with self._lock:
            self._down_until[url] = time.monotonic() + DOWN_SECONDS


class ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, backends: Backends, quiet: bool = False):
        super().__init__((HOST, port), ProxyHandler)
        self.backends = backends
        self.quiet = quiet
        actual = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}", "127.0.0.1", "localhost"}


class ProxyHandler(BaseHTTPRequestHandler):
    server: ProxyServer
    server_version = "neai-proxy"
    sys_version = ""
    timeout = 10

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            return self._error(NeaiError("FORBIDDEN_HOST", "Host header is not allowed",
                                         hint="the proxy only answers requests addressed to localhost / 127.0.0.1"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(NeaiError("BAD_REQUEST", "invalid Content-Length header"))
        if length > MAX_BODY_BYTES:
            return self._error(NeaiError("BODY_TOO_LARGE", f"request body is larger than {MAX_BODY_BYTES} bytes"))
        body = self.rfile.read(length) if length > 0 else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}

        for url in self.server.backends.in_turn():
            parts = urlsplit(url)
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=BACKEND_TIMEOUT_SECONDS)
            try:
                try:
                    conn.connect()
                except OSError:
                    # Nothing was sent, so trying the next instance can't run a write twice.
                    self.server.backends.mark_down(url)
                    continue
                try:
                    conn.request(self.command, self.path, body=body, headers={**headers, "Host": parts.netloc})
                    resp = conn.getresponse()
                    data = resp.read()
                except (OSError, http.client.HTTPException) as e:
                    return self._error(NeaiError("INSTANCE_FAILED", f"the instance at {url} failed mid-request: {e}",
                                                 hint="it may have been stopped; the request was not retried, "
                                                      "because it might already have run"))
            finally:
                conn.close()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Neai-Instance", url)
            self.end_headers()
            self.wfile.write(data)
            return
        self._error(NeaiError("NO_INSTANCE", f"no running instance of '{self.server.backends.app}'",
                              hint=f"start one with `neai serve {self.server.backends.app} --port 0`"))

    def _error(self, err: NeaiError) -> None:
        data = json.dumps(error_envelope(err)).encode()
        self.send_response(err.http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        if not self.server.quiet:
            sys.stderr.write(json.dumps({"at": now(), "request": format % args}) + "\n")


def run_proxy(database: str | None, app: str, *, port: int = DEFAULT_PROXY_PORT, out: TextIO = sys.stdout,
              quiet: bool = False) -> int:
    db = Database(database)
    try:
        try:
            require_app(db, app)
            backends = Backends(db, app)
            instances = backends.in_turn()
            httpd = ProxyServer(port, backends, quiet=quiet)
        except NeaiError as err:
            out.write(json.dumps(error_envelope(err)) + "\n")
            return 1
        except OSError as e:
            err = NeaiError("PORT_IN_USE", f"can't listen on {HOST}:{port}: {e.strerror}", hint="pick another --port")
            out.write(json.dumps(error_envelope(err)) + "\n")
            return 1

        def stop(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, stop)
        url = f"http://{HOST}:{httpd.server_address[1]}"
        out.write(json.dumps({"ok": True, "status": "proxying", "app": app, "url": url, "instances": instances,
                              "message": f"{url} spreads requests over every running instance of {app}"}) + "\n")
        out.flush()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
            httpd.server_close()
        return 0
    finally:
        db.close()
