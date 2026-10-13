from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..client import call_server
from ..deploy import deploy
from ..errors import NeaiError
from ..inspection import inspect_app
from ..loader import HANDLERS_FILE, MANIFEST_FILE
from ..paths import app_dir, check_app_name, list_apps, read_server_info, resolve_home
from ..scaffold import skeleton_files
from ..schema import IDENT_RE
from ..store import Store
from . import ai, codecheck

START_TIMEOUT_SECONDS = 20


class UI:
    def __init__(self, home: str | os.PathLike[str] | None, apps_dir: str | os.PathLike[str]):
        self.home = resolve_home(home)
        self.apps_dir = Path(apps_dir).resolve()
        self.children: dict[str, subprocess.Popen] = {}


    def config(self) -> dict[str, Any]:
        app_json, handlers_py = skeleton_files("my_app")
        return {"ok": True, "home": str(self.home), "apps_dir": str(self.apps_dir), "ai": ai.settings(),
                "template": {"app_json": app_json, "handlers_py": handlers_py}}

    def apps(self) -> dict[str, Any]:
        out = []
        for name in list_apps(self.home):
            store = Store(app_dir(self.home, name))
            try:
                active = store.active_version()
                info = store.get_version(active) if active else None
            finally:
                store.close()
            out.append({"name": name, "version": active, "running": read_server_info(self.home, name) is not None,
                        "description": (info or {}).get("manifest", {}).get("description")})
        return {"ok": True, "apps": out}

    def app(self, name: str) -> dict[str, Any]:
        report = inspect_app(self.home, check_app_name(name), records=50, logs=30)
        report["files"] = self._active_files(name)
        return report


    def call(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        info = read_server_info(self.home, check_app_name(name))
        if not info:
            raise NeaiError("SERVER_NOT_RUNNING", f"'{name}' isn't running", hint="start its server first")
        handler = body.get("handler")
        if not isinstance(handler, str) or not IDENT_RE.match(handler):
            raise NeaiError("UNKNOWN_HANDLER", f"invalid handler name {handler!r}")
        return call_server(info["url"], handler, body.get("caller"), body.get("args") or {})

    def start(self, name: str) -> dict[str, Any]:
        check_app_name(name)
        if info := read_server_info(self.home, name):
            return {"ok": True, "status": "running", **info}
        source = self._source_dir(name)
        if not source or not source.is_dir():
            raise NeaiError("SOURCE_MISSING", f"the folder '{name}' was deployed from no longer exists ({source})",
                            hint="deploy it again from the editor or with `neai deploy <dir>`")
        proc = subprocess.Popen(
            [sys.executable, "-m", "neai", "run", str(source), "--port", "0", "--quiet", "--home", str(self.home)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            msg = json.loads(line)
            seen.append(msg)
            if msg.get("status") == "serving":
                self.children[name] = proc
                return {**msg, "ok": True, "status": "started", "deploy": seen[0]}
        if proc.poll() is None:
            proc.terminate()
        last_error = next((m["error"] for m in reversed(seen) if isinstance(m.get("error"), dict)), None)
        raise NeaiError(last_error["code"] if last_error else "SERVER_START_FAILED",
                        last_error["message"] if last_error else "the server didn't start",
                        hint=(last_error or {}).get("hint"), details={"output": seen})

    def stop(self, name: str) -> dict[str, Any]:
        check_app_name(name)
        proc = self.children.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        elif info := read_server_info(self.home, name):
            os.kill(info["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 5
            while read_server_info(self.home, name) and time.monotonic() < deadline:
                time.sleep(0.05)
        return {"ok": True, "status": "stopped", "app": name}

    def close(self) -> None:
        for name in list(self.children):
            self.stop(name)


    def deploy(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode")
        app_text, code = body.get("app_json"), body.get("handlers_py")
        if mode not in ("new", "change") or not isinstance(app_text, str) or not isinstance(code, str):
            raise NeaiError("BAD_REQUEST", 'send {"mode": "new"|"change", "app_json": str, "handlers_py": str}')
        overwrite = body.get("overwrite") is True
        name = _manifest_name(app_text)
        if mode == "change" and name != body.get("app"):
            raise NeaiError("NAME_MISMATCH", f"this app is '{body.get('app')}', but app.json says '{name}'",
                            hint="keep the name to change this app, or use New app to create another one")
        if problems := codecheck.check(code):
            raise NeaiError("UNSAFE_CODE", f"handlers.py failed the safety check ({len(problems)} problem(s))",
                            hint="remove the flagged lines; handlers only need ctx.db and the standard library",
                            details={"problems": problems})

        exists = (app_dir(self.home, name) / "app.db").exists()
        if mode == "new" and exists and not overwrite:
            raise NeaiError("APP_EXISTS", f"an app named '{name}' already exists",
                            hint="confirm to replace its code (its data is kept), or pick another name")
        target = (self._source_dir(name) if exists else None) or self.apps_dir / name
        files = {MANIFEST_FILE: app_text, HANDLERS_FILE: code}
        if not overwrite and _has_undeployed_changes(target, files, self._active_files(name) if exists else None):
            raise NeaiError("SOURCE_CHANGED", f"{target} has changes that were never deployed",
                            hint="confirm to overwrite them with the editor's version", details={"dir": str(target)})
        target.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (target / fname).write_text(text)
        result = deploy(self.home, target)
        result["dir"] = str(target)
        return result

    def generate(self, body: dict[str, Any]) -> dict[str, Any]:
        current = body.get("files")
        if current is not None and not (isinstance(current, dict) and all(isinstance(current.get(k), str)
                                                                            for k in ("app_json", "handlers_py"))):
            raise NeaiError("BAD_REQUEST", "files must be {app_json: str, handlers_py: str}")
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        draft = ai.generate(body.get("request"), current=current, error=error)
        draft["review"] = codecheck.check(draft["handlers_py"])
        return {"ok": True, **draft}


    def _source_dir(self, name: str) -> Path | None:
        d = app_dir(self.home, name)
        if not (d / "app.db").exists():
            return None
        store = Store(d)
        try:
            source = store.get_meta("source_dir")
        finally:
            store.close()
        return Path(source) if source else None

    def _active_files(self, name: str) -> dict[str, str] | None:
        d = app_dir(self.home, name)
        store = Store(d)
        try:
            active = store.active_version()
        finally:
            store.close()
        if not active:
            return None
        vdir = d / "versions" / str(active)
        return {"app_json": (vdir / MANIFEST_FILE).read_text(), "handlers_py": (vdir / HANDLERS_FILE).read_text()}


def _manifest_name(app_text: str) -> str:
    try:
        raw = json.loads(app_text)
    except ValueError as e:
        raise NeaiError("MANIFEST_INVALID", f"app.json is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
                        hint="app.json must be plain JSON: no comments, no trailing commas",
                        details={"line": e.lineno, "column": e.colno}) from None
    name = raw.get("name") if isinstance(raw, dict) else None
    return check_app_name(name)


def _has_undeployed_changes(target: Path, incoming: dict[str, str], active: dict[str, str] | None) -> bool:
    existing = {f: (target / f).read_text() for f in incoming if (target / f).is_file()}
    if not existing:
        return False
    if existing == incoming:
        return False
    if active and existing == {MANIFEST_FILE: active["app_json"], HANDLERS_FILE: active["handlers_py"]}:
        return False
    return True
