from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, TextIO

from .deploy import deploy
from .errors import NeaiError, error_envelope
from .loader import UI_INDEX, UI_TYPES
from .runtime import Runtime
from .store import now

HEARTBEAT_SECONDS = 5
APP_UI = Path(__file__).with_name("appui")
# App UIs are untrusted (often AI-written). Each app has its own origin (its own port), and its
# pages may only load their own files and talk to their own server.
APP_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'self'; frame-ancestors 'none'")

HOST = "127.0.0.1"
DEFAULT_PORT = 8710
MAX_BODY_BYTES = 1024 * 1024
CALL_KEYS = {"args"}
AUTH_KEYS = {"username", "password"}


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
        self.instance_id = str(uuid.uuid4())
        self.url = f"http://{HOST}:{self.server_address[1]}"
        self._last_beat = 0.0

    def register(self) -> None:
        self.runtime.store.register_instance(self.instance_id, self.url)
        self._last_beat = time.monotonic()

    def service_actions(self) -> None:
        # Called by serve_forever between requests: tell the database this instance is alive.
        if time.monotonic() - self._last_beat < HEARTBEAT_SECONDS:
            return
        self._last_beat = time.monotonic()
        try:
            self.runtime.store.heartbeat(self.instance_id, self.url)
        except NeaiError:
            pass  # the database is away; the next heartbeat re-registers this instance

    def unregister(self) -> None:
        try:
            self.runtime.store.remove_instance(self.instance_id)
        except NeaiError:
            pass


class RequestHandler(LocalJSONHandler):
    server: AppServer

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path == "/_neai/sdk.js":
            return self._page((APP_UI / "sdk.js").read_bytes(), UI_TYPES[".js"])
        if path == "/_neai/ui":
            return self._page((APP_UI / "generated.html").read_bytes(), UI_TYPES[".html"])
        if path == "/_neai/contract":
            try:
                return self._reply(self.server.runtime.contract())
            except NeaiError as err:
                return self._send(err.http_status, error_envelope(err))
        if self.path == "/_neai/health":
            loaded = None
            try:
                loaded = self.server.runtime.refresh()
            except NeaiError as err:
                return self._send(err.http_status, error_envelope(err))
            return self._send(200, {"ok": True, "app": self.server.runtime.name,
                                    "version": loaded.version if loaded else None,
                                    "instance": self.server.instance_id, "pid": os.getpid()})
        if self.path == "/auth/me":
            try:
                token = self._bearer()
            except NeaiError as err:
                return self._send(err.http_status, error_envelope(err))
            return self._reply(self.server.runtime.auth("me", token=token), private=True)
        if not path.startswith(("/_neai/", "/auth/", "/call/")):
            return self._ui_file(path)
        self._not_found()

    def _ui_file(self, path: str) -> None:
        try:
            loaded = self.server.runtime.refresh()
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        files = loaded.files if loaded else {}
        if path == "/":
            if UI_INDEX in files:
                return self._page(files[UI_INDEX].encode(), UI_TYPES[".html"])
            return self._page((APP_UI / "generated.html").read_bytes(), UI_TYPES[".html"])
        # An exact lookup in the version's files: no filesystem path, so nothing like ../ can escape.
        key = "ui" + path
        if key in files:
            return self._page(files[key].encode(), UI_TYPES.get(Path(key).suffix.lower(), "text/plain; charset=utf-8"))
        self._not_found()

    def _page(self, data: bytes, content_type: str) -> None:
        self._send_bytes(200, data, content_type, {"Content-Security-Policy": APP_CSP, "Cache-Control": "no-cache",
                                                   "Referrer-Policy": "no-referrer"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        if self.path in ("/auth/signup", "/auth/login", "/auth/logout"):
            return self._auth(self.path[len("/auth/"):])
        if not self.path.startswith("/call/"):
            return self._not_found()
        handler = self.path[len("/call/"):]
        try:
            body = self._json_body(CALL_KEYS | {"caller"}, example='{"args": {...}}')
            if "caller" in body:
                raise NeaiError(
                    "BAD_REQUEST", 'the request body can\'t name the caller; it comes from your login token',
                    hint="log in with POST /auth/login (or `neai login <app> <username>`), then send "
                         "Authorization: Bearer <token> and a body of {\"args\": {...}}")
            token = self._bearer()
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        self._reply(self.server.runtime.call(handler, args=body.get("args"), token=token))

    def _auth(self, action: str) -> None:
        try:
            body = self._json_body(AUTH_KEYS, example='{"username": "alice", "password": "..."}') \
                if action != "logout" else {}
            token = self._bearer()
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        self._reply(self.server.runtime.auth(action, body, token=token), private=True)

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise NeaiError("AUTH_INVALID", "the Authorization header must be: Bearer <token>")
        return token.strip()

    def _reply(self, response: dict[str, Any], private: bool = False) -> None:
        status = 200 if response["ok"] else NeaiError(response["error"]["code"], "").http_status
        headers = {"Cache-Control": "no-store"} if private else None
        self._send_bytes(status, json.dumps(response).encode(), "application/json", headers)

    def _not_found(self) -> None:
        err = NeaiError("ROUTE_NOT_FOUND", f"no route for {self.command} {self.path}",
                        hint="the app's UI is at GET /; the API is POST /call/<handler>, "
                             "POST /auth/signup|login|logout, GET /auth/me, GET /_neai/contract and GET /_neai/health")
        self._send(err.http_status, error_envelope(err))


def run(database: str | None, source_dir: str | os.PathLike[str], *, port: int = DEFAULT_PORT,
        out: TextIO = sys.stdout, quiet: bool = False) -> int:
    """Deploy the folder if it changed, then serve the app (`neai run <dir>`)."""
    result = deploy(database, source_dir)
    _emit(out, result)
    if not result["ok"] and not result.get("active_version"):
        return 1
    return serve(database, result["app"], port=port, out=out, quiet=quiet, fallback=not result["ok"])


def serve(database: str | None, name: str, *, port: int = DEFAULT_PORT, out: TextIO = sys.stdout,
          quiet: bool = False, fallback: bool = False) -> int:
    """Serve the app's active version, loaded from the database (`neai serve <app>`).

    Nothing is read from or written to local files, so any number of copies can run, on any
    machine that reaches the database.
    """
    try:
        runtime = Runtime(database, name)
    except NeaiError as err:
        _emit(out, error_envelope(err))
        return 1
    try:
        try:
            loaded = runtime.refresh()
        except NeaiError as err:
            _emit(out, error_envelope(err))
            return 1
        if loaded is None:
            _emit(out, error_envelope(NeaiError("NO_ACTIVE_VERSION", f"app '{name}' has no active version",
                                                hint="deploy a valid definition with `neai deploy <dir>`")))
            return 1
        try:
            httpd = AppServer(port, runtime, quiet=quiet)
        except OSError as e:
            _emit(out, error_envelope(NeaiError("PORT_IN_USE", f"can't listen on {HOST}:{port}: {e.strerror}",
                                                hint="pick another port with --port, or --port 0 for any free port")))
            return 1
        try:
            httpd.register()
            runtime.store.add_logs([{"event": "server", "version": loaded.version,
                                     "message": f"serving on {httpd.url}"}])
        except NeaiError as err:
            httpd.server_close()
            _emit(out, error_envelope(err))
            return 1

        def stop(signum: int, frame: Any) -> None:
            raise KeyboardInterrupt

        previous_sigterm = signal.signal(signal.SIGTERM, stop)
        _emit(out, {"ok": True, "status": "serving", "app": name, "version": loaded.version, "url": httpd.url,
                    "pid": os.getpid(), "instance": httpd.instance_id, "fallback": fallback,
                    "message": "serving the last valid version because the new definition was rejected"
                    if fallback else f"serving version {loaded.version}"})
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            httpd.server_close()
            httpd.unregister()
            try:
                runtime.store.add_logs([{"event": "server", "message": f"server on {httpd.url} stopped"}])
            except NeaiError:
                pass
        return 0
    finally:
        runtime.close()


def _emit(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload) + "\n")
    out.flush()
