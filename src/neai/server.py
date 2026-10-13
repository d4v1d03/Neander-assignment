from __future__ import annotations

import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, TextIO

from .deploy import deploy
from .errors import NeaiError, error_envelope
from .paths import read_server_info, resolve_home, server_file
from .runtime import Runtime
from .store import now

HOST = "127.0.0.1"
DEFAULT_PORT = 8710
MAX_BODY_BYTES = 1024 * 1024
BODY_KEYS = {"caller", "args"}


class LocalServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, port: int, handler: type[BaseHTTPRequestHandler], quiet: bool = False):
        super().__init__((HOST, port), handler)
        self.quiet = quiet
        actual = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}", "127.0.0.1", "localhost"}


class LocalJSONHandler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "neai"
    sys_version = ""
    # Single-threaded server: a client that connects and then stalls must not block everyone.
    timeout = 10

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        if host in self.server.allowed_hosts:
            return True
        err = NeaiError("FORBIDDEN_HOST", f"Host header {host!r} is not allowed",
                        hint="the server only answers requests addressed to localhost / 127.0.0.1")
        self._send(err.http_status, error_envelope(err))
        return False

    def _json_body(self, allowed_keys: set[str] | None = None, example: str = "{...}") -> dict[str, Any]:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise NeaiError("UNSUPPORTED_MEDIA_TYPE", "POST bodies must be sent as application/json",
                            hint="add the header: Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise NeaiError("BAD_REQUEST", "invalid Content-Length header") from None
        if length > MAX_BODY_BYTES:
            raise NeaiError("BODY_TOO_LARGE", f"request body is larger than {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            raise NeaiError("BAD_REQUEST", "request body is not valid JSON", hint=f"send {example}") from None
        if not isinstance(body, dict):
            raise NeaiError("BAD_REQUEST", "request body must be a JSON object", hint=f"send {example}")
        unknown = sorted(set(body) - allowed_keys) if allowed_keys is not None else []
        if unknown:
            raise NeaiError("BAD_REQUEST", f"unknown key(s) in request body: {', '.join(unknown)}",
                            hint=f"allowed keys: {', '.join(sorted(allowed_keys))}")
        return body

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        self._send_bytes(status, json.dumps(payload).encode(), "application/json")

    def _send_bytes(self, status: int, data: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        if not self.server.quiet:
            sys.stderr.write(json.dumps({"at": now(), "request": format % args}) + "\n")


class AppServer(LocalServer):
    def __init__(self, port: int, runtime: Runtime, quiet: bool = False):
        super().__init__(port, RequestHandler, quiet)
        self.runtime = runtime


class RequestHandler(LocalJSONHandler):
    server: AppServer

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        if self.path == "/_neai/health":
            loaded = None
            try:
                loaded = self.server.runtime.refresh()
            except NeaiError as err:
                return self._send(err.http_status, error_envelope(err))
            return self._send(200, {"ok": True, "app": self.server.runtime.name,
                                    "version": loaded.version if loaded else None})
        self._not_found()

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        if not self.path.startswith("/call/"):
            return self._not_found()
        handler = self.path[len("/call/"):]
        try:
            body = self._json_body(BODY_KEYS, example='{"caller": "<name>", "args": {...}}')
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        response = self.server.runtime.call(handler, body.get("caller"), body.get("args"))
        status = 200 if response["ok"] else NeaiError(response["error"]["code"], "").http_status
        self._send(status, response)

    def _not_found(self) -> None:
        err = NeaiError("ROUTE_NOT_FOUND", f"no route for {self.command} {self.path}",
                        hint="use POST /call/<handler> or GET /_neai/health")
        self._send(err.http_status, error_envelope(err))


def run(home: str | os.PathLike[str] | None, source_dir: str | os.PathLike[str], *, port: int = DEFAULT_PORT,
        out: TextIO = sys.stdout, quiet: bool = False) -> int:
    home = resolve_home(home)
    result = deploy(home, source_dir)
    _emit(out, result)
    name = result.get("app")
    if not result["ok"] and not result.get("active_version"):
        return 1

    runtime = Runtime(home, name)
    try:
        if read_server_info(home, name):
            _emit(out, error_envelope(NeaiError(
                "SERVER_ALREADY_RUNNING", f"a server for '{name}' is already running",
                hint="reuse it with `neai call`, or stop it first (Ctrl+C in its terminal, or kill <pid>)",
                details=read_server_info(home, name))))
            return 1
        try:
            loaded = runtime.refresh()
        except NeaiError as err:
            _emit(out, error_envelope(err))
            return 1
        try:
            httpd = AppServer(port, runtime, quiet=quiet)
        except OSError as e:
            _emit(out, error_envelope(NeaiError("PORT_IN_USE", f"can't listen on {HOST}:{port}: {e.strerror}",
                                                hint="pick another port with --port, or --port 0 for any free port")))
            return 1
        actual_port = httpd.server_address[1]
        url = f"http://{HOST}:{actual_port}"
        info = {"pid": os.getpid(), "port": actual_port, "url": url, "started_at": now()}
        sfile = server_file(home, name)
        sfile.write_text(json.dumps(info))
        sfile.chmod(0o600)
        runtime.store.add_logs([{"event": "server", "version": loaded.version, "message": f"serving on {url}"}])

        def stop(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt

        previous_sigterm = signal.signal(signal.SIGTERM, stop)
        _emit(out, {"ok": True, "status": "serving", "app": name, "version": loaded.version, "url": url,
                    "pid": os.getpid(), "fallback": not result["ok"],
                    "message": "serving the last valid version because the new definition was rejected"
                    if not result["ok"] else f"serving version {loaded.version}"})
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            httpd.server_close()
            _remove_if_ours(sfile)
            runtime.store.add_logs([{"event": "server", "message": "server stopped"}])
        return 0
    finally:
        runtime.close()


def _remove_if_ours(path: Path) -> None:
    try:
        if json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def _emit(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload) + "\n")
    out.flush()
