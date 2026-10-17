from __future__ import annotations

import json
import re
import signal
import sys
import urllib.request
from pathlib import Path
from typing import Any, TextIO

from ..errors import NeaiError, error_envelope
from ..server import HOST, LocalJSONHandler, LocalServer
from .api import UI

DEFAULT_UI_PORT = 8700
PAGE = Path(__file__).with_name("static") / "index.html"
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
APP_ROUTE = re.compile(r"^/api/apps/([a-z][a-z0-9_-]{0,63})(?:/(call|start|stop|users|signup|login|logout|me))?$")


class UIServer(LocalServer):
    def __init__(self, port: int, ui: UI, quiet: bool = True):
        super().__init__(port, UIRequestHandler, quiet)
        self.ui = ui


class UIRequestHandler(LocalJSONHandler):
    server: UIServer

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        path = self.path.split("?")[0]
        if path == "/":
            return self._send_bytes(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                                    {"Content-Security-Policy": CSP, "Cache-Control": "no-store"})
        ui = self.server.ui
        if path == "/api/config":
            return self._respond(ui.config)
        if path == "/api/apps":
            return self._respond(ui.apps)
        m = APP_ROUTE.match(path)
        if m and not m.group(2):
            return self._respond(lambda: ui.app(m.group(1)))
        if m and m.group(2) == "users":
            return self._respond(lambda: ui.users(m.group(1)))
        self._not_found()

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        try:
            body = self._json_body()
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        ui = self.server.ui
        path = self.path.split("?")[0]
        if path == "/api/deploy":
            return self._respond(lambda: ui.deploy(body))
        if path == "/api/ai":
            return self._respond(lambda: ui.generate(body))
        if path == "/api/ai/ui":
            return self._respond(lambda: ui.generate_ui(body))
        m = APP_ROUTE.match(path)
        if m and m.group(2):
            name, action = m.groups()
            if action == "call":
                return self._respond(lambda: ui.call(name, body))
            if action == "users":
                return self._respond(lambda: ui.users(name, body))
            if action in ("signup", "login", "logout", "me"):
                return self._respond(lambda: ui.auth(name, action, body))
            if action == "start":
                return self._respond(lambda: ui.start(name))
            return self._respond(lambda: ui.stop(name))
        self._not_found()

    def _respond(self, fn: Any) -> None:
        try:
            result = fn()
        except NeaiError as err:
            return self._send(err.http_status, error_envelope(err))
        except Exception as e:
            err = NeaiError("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
            return self._send(500, error_envelope(err))
        status = 200 if result.get("ok") else NeaiError(result["error"]["code"], "").http_status
        # Responses can carry login tokens and app data: never cache them.
        self._send_bytes(status, json.dumps(result).encode(), "application/json", {"Cache-Control": "no-store"})

    def _not_found(self) -> None:
        err = NeaiError("ROUTE_NOT_FOUND", f"no route for {self.command} {self.path}")
        self._send(err.http_status, error_envelope(err))


def _running_ui(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/config", timeout=2) as resp:
            config = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) and config.get("ok") and "database" in config else None


def serve_ui(database: str | None, apps_dir: str, *, port: int = DEFAULT_UI_PORT,
             out: TextIO = sys.stdout, loaded_env: list[dict[str, Any]] | None = None) -> int:
    ui = UI(database, apps_dir)
    try:
        httpd = UIServer(port, ui)
    except OSError as e:
        running = _running_ui(port)
        if running:
            err = NeaiError("UI_ALREADY_RUNNING", f"neai ui is already running at http://{HOST}:{port}",
                            hint=f"just open http://{HOST}:{port}, or stop that one first (Ctrl+C in its terminal), "
                                 "or start another with --port",
                            details={"url": f"http://{HOST}:{port}", "database": running.get("database")})
        else:
            err = NeaiError("PORT_IN_USE", f"can't listen on {HOST}:{port}: {e.strerror}",
                            hint="another program is using this port; pick another with --port")
        out.write(json.dumps(error_envelope(err)) + "\n")
        return 1
    url = f"http://{HOST}:{httpd.server_address[1]}"

    def stop(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    out.write(json.dumps({"ok": True, "status": "serving", "url": url, "database": ui.config()["database"],
                          "apps_dir": str(ui.apps_dir), "ai": ui.config()["ai"], "env_files": loaded_env or [],
                          "message": f"open {url} in your browser; Ctrl+C to stop"}) + "\n")
    out.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        httpd.server_close()
        ui.close()
    return 0
