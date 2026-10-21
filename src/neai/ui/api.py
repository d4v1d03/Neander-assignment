from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..auth import user_admin
from ..client import auth_request, call_server
from ..database import Database, safe_url
from ..deploy import deploy
from ..errors import NeaiError
from ..inspection import inspect_app
from ..loader import HANDLERS_FILE, MANIFEST_FILE, OPTIONAL_FILES, definition_path_ok, folder_files
from ..workflow import WORKFLOWS_CODE, WORKFLOWS_FILE
from ..paths import check_app_name
from ..scaffold import TESTS_TEMPLATE, WORKFLOWS_TEMPLATE, skeleton_files
from ..schema import IDENT_RE, parse_manifest
from ..store import Store, list_apps, list_instances, require_app
from . import ai, codecheck

START_TIMEOUT_SECONDS = 20
AI_CONCURRENCY = 2
AI_WAIT_SECONDS = 90


class UI:
    def __init__(self, database: str | None, apps_dir: str | os.PathLike[str]):
        self.db = Database(database)
        self.apps_dir = Path(apps_dir).resolve()
        # The dashboard serves requests on several threads at once (two tabs, or a page loading
        # while DeepSeek works), so everything below that they share is guarded.
        self._lock = threading.Lock()  # guards the two dicts below
        self.children: dict[str, subprocess.Popen] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        # A few DeepSeek calls at once; more wait their turn (each can take a minute or two).
        self.ai_slots = max(1, int(os.environ.get("NEAI_AI_CONCURRENCY", AI_CONCURRENCY)))
        self._ai = threading.BoundedSemaphore(self.ai_slots)

    def _lock_for(self, kind: str, name: str) -> threading.Lock:
        """One lock per app and kind of work ("server": start/stop, "folder": save + deploy).

        Per app, so working on one app never waits for another. The two kinds are never held
        together, so they can't deadlock.
        """
        with self._lock:
            return self._locks.setdefault((kind, name), threading.Lock())


    def config(self) -> dict[str, Any]:
        app_json, handlers_py = skeleton_files("my_app")
        return {"ok": True, "database": safe_url(self.db.url), "apps_dir": str(self.apps_dir), "ai": ai.settings(),
                "template": {MANIFEST_FILE: app_json, HANDLERS_FILE: handlers_py, WORKFLOWS_FILE: WORKFLOWS_TEMPLATE,
                             "tests.json": TESTS_TEMPLATE}}

    def apps(self) -> dict[str, Any]:
        running = {i["app"] for i in list_instances(self.db)}
        out = []
        for name in list_apps(self.db):
            store = Store(self.db, name)
            active = store.active_version()
            info = store.get_version(active) if active else None
            out.append({"name": name, "version": active, "running": name in running,
                        "description": (info or {}).get("manifest", {}).get("description")})
        return {"ok": True, "apps": out}

    def app(self, name: str) -> dict[str, Any]:
        report = inspect_app(self.db.url, check_app_name(name), records=50, logs=30)
        # An app that never went live has no version to show, so the editor opens its folder's files instead.
        report["files"] = self._active_files(name) or self._draft_files(name)
        return report


    def call(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._url(name)
        handler = body.get("handler")
        if not isinstance(handler, str) or not IDENT_RE.match(handler):
            raise NeaiError("UNKNOWN_HANDLER", f"invalid handler name {handler!r}")
        # The dashboard calls as a real user too: it sends that user's login token, never a name.
        return call_server(url, handler, body.get("args") or {}, token=body.get("token"))

    def auth(self, name: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Sign-in goes to the app's own server, exactly as the app's real users do it. The page
        # keeps the token in memory only; the dashboard never stores it.
        url = self._url(name)
        if action in ("logout", "me"):
            return auth_request(url, action, token=body.get("token"))
        return auth_request(url, action, body={"username": body.get("username"), "password": body.get("password")})

    def users(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        store = require_app(self.db, check_app_name(name))
        active = store.active_version()
        manifest = parse_manifest(store.get_version(active)["manifest"]) if active else None
        if body is None:
            return user_admin(store, manifest, "list")
        roles = body.get("roles") or []
        if not isinstance(roles, list):
            raise NeaiError("BAD_REQUEST", "roles must be a list of role names")
        return user_admin(store, manifest, body.get("action"), username=body.get("username"),
                          role=body.get("role"), roles=roles, password=body.get("password"))

    def _url(self, name: str) -> str:
        instances = list_instances(self.db, check_app_name(name))
        if not instances:
            raise NeaiError("SERVER_NOT_RUNNING", f"'{name}' isn't running", hint="start its server first")
        return instances[0]["url"]

    def start(self, name: str) -> dict[str, Any]:
        check_app_name(name)
        # Check-then-start under the lock: two clicks at once must not start two servers.
        with self._lock_for("server", name):
            return self._start(name)

    def _start(self, name: str) -> dict[str, Any]:
        if instances := list_instances(self.db, name):
            return {"ok": True, "status": "running", **instances[0]}
        # The URL goes through the environment, not argv, so a password in it isn't visible in `ps`.
        proc = subprocess.Popen(
            [sys.executable, "-m", "neai", "serve", name, "--port", "0", "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "NEAI_DATABASE_URL": self.db.url},
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
                with self._lock:
                    self.children[name] = proc
                return {**msg, "ok": True, "status": "started"}
        if proc.poll() is None:
            proc.terminate()
        last_error = next((m["error"] for m in reversed(seen) if isinstance(m.get("error"), dict)), None)
        raise NeaiError(last_error["code"] if last_error else "SERVER_START_FAILED",
                        last_error["message"] if last_error else "the server didn't start",
                        hint=(last_error or {}).get("hint"), details={"output": seen})

    def stop(self, name: str) -> dict[str, Any]:
        check_app_name(name)
        with self._lock_for("server", name):
            return self._stop(name)

    def _stop(self, name: str) -> dict[str, Any]:
        with self._lock:
            proc = self.children.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        here = socket.gethostname()
        for instance in list_instances(self.db, name):
            if instance["host"] == here and instance["pid"]:
                try:
                    os.kill(instance["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while list_instances(self.db, name) and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"ok": True, "status": "stopped", "app": name}

    def close(self) -> None:
        with self._lock:
            names = list(self.children)
        for name in names:
            self.stop(name)
        self.db.close()


    def deploy(self, body: dict[str, Any]) -> dict[str, Any]:
        """Save the editor's files to the app's folder (the one `neai deploy` uses), then deploy it."""
        mode = body.get("mode")
        files = _clean_files(body.get("files"))
        if mode not in ("new", "change"):
            raise NeaiError("BAD_REQUEST", 'send {"mode": "new"|"change", "files": {"app.json": ..., ...}}')
        overwrite = body.get("overwrite") is True
        name = _manifest_name(files[MANIFEST_FILE])
        # Check the folder, write it, deploy it: all under the app's lock, so two tabs saving the
        # same app can't interleave their files (Postgres already orders the deploys themselves).
        with self._lock_for("folder", name):
            return self._deploy(name, mode, files, overwrite, body)

    def _deploy(self, name: str, mode: str, files: dict[str, str], overwrite: bool,
                body: dict[str, Any]) -> dict[str, Any]:
        if mode == "change" and name != body.get("app"):
            raise NeaiError("NAME_MISMATCH", f"this app is '{body.get('app')}', but app.json says '{name}'",
                            hint="keep the name to change this app, or use New app to create another one")
        for fname in (HANDLERS_FILE, WORKFLOWS_CODE):
            if fname in files and (problems := codecheck.check(files[fname])):
                raise NeaiError("UNSAFE_CODE", f"{fname} failed the safety check ({len(problems)} problem(s))",
                                hint="remove the flagged lines; app code only needs ctx and the standard library",
                                details={"file": fname, "problems": problems})

        exists = Store(self.db, name).exists()
        # An app whose deploys were all rejected never went live: making it again isn't replacing anything.
        live = exists and Store(self.db, name).active_version() is not None
        if mode == "new" and live and not overwrite:
            raise NeaiError("APP_EXISTS", f"an app named '{name}' already exists",
                            hint="confirm to replace its code (its data is kept), or pick another name")
        target = (self._source_dir(name) if exists else None) or self.apps_dir / name
        on_disk = folder_files(target) if target.is_dir() else {}
        active = self._active_files(name) if exists else None
        # What this editor wrote last time (e.g. a rejected attempt): replacing that is not losing anyone's work.
        previous = _clean_files(body["previous"]) if isinstance(body.get("previous"), dict) else None
        if not overwrite and on_disk and on_disk not in (files, active, previous):
            raise NeaiError("SOURCE_CHANGED", f"{target} has changes that were never deployed",
                            hint="confirm to overwrite them with the editor's version", details={"dir": str(target)})
        target.mkdir(parents=True, exist_ok=True)
        for key in set(on_disk) - set(files):
            (target / key).unlink()  # a file the editor no longer has (e.g. an emptied workflows.py)
        for key, text in files.items():
            (target / key).parent.mkdir(parents=True, exist_ok=True)
            (target / key).write_text(text)
        result = deploy(self.db.url, target)
        result["dir"] = str(target)
        return result

    def generate(self, body: dict[str, Any]) -> dict[str, Any]:
        current = _clean_files(body["files"]) if body.get("files") is not None else None
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        with self._ai_turn():
            draft = ai.generate(body.get("request"), current=current, error=error)
        draft["review"] = _review(draft["files"])
        return {"ok": True, **draft}

    def generate_ui(self, body: dict[str, Any]) -> dict[str, Any]:
        files = _clean_files(body.get("files"))
        with self._ai_turn():
            draft = ai.generate_ui(body.get("request") or "", files=files)
        return {"ok": True, **draft}

    @contextmanager
    def _ai_turn(self) -> Iterator[None]:
        # Held for the whole DeepSeek call: limiting those is its job. No other lock is held
        # while waiting on DeepSeek, so a slow answer never blocks anything else.
        if not self._ai.acquire(timeout=AI_WAIT_SECONDS):
            raise NeaiError("AI_BUSY", f"{self.ai_slots} DeepSeek request(s) already running, "
                                       f"and none finished within {AI_WAIT_SECONDS}s",
                            hint="try again in a minute (NEAI_AI_CONCURRENCY allows more at once)")
        try:
            yield
        finally:
            self._ai.release()


    def _source_dir(self, name: str) -> Path | None:
        info = Store(self.db, name).info()
        return Path(info["source_dir"]) if info and info["source_dir"] else None

    def _draft_files(self, name: str) -> dict[str, str] | None:
        target = self._source_dir(name) or self.apps_dir / name
        return (folder_files(target) or None) if target.is_dir() else None

    def _active_files(self, name: str) -> dict[str, str] | None:
        store = Store(self.db, name)
        active = store.active_version()
        if not active:
            return None
        return store.get_version(active, files=True)["files"]


def _manifest_name(app_text: str) -> str:
    try:
        raw = json.loads(app_text)
    except ValueError as e:
        raise NeaiError("MANIFEST_INVALID", f"app.json is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
                        hint="app.json must be plain JSON: no comments, no trailing commas",
                        details={"line": e.lineno, "column": e.colno}) from None
    name = raw.get("name") if isinstance(raw, dict) else None
    return check_app_name(name)


def _clean_files(files: Any) -> dict[str, str]:
    """The editor's files: known names only, empty optional files left out (they mean "none")."""
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise NeaiError("BAD_REQUEST", 'files must be an object of file name to text, e.g. {"app.json": "..."}')
    bad = sorted(k for k in files if not definition_path_ok(k))
    if bad:
        raise NeaiError("BAD_REQUEST", f"not an app file: {', '.join(bad)}",
                        hint=f"app files are app.json, handlers.py, {', '.join(OPTIONAL_FILES)} and ui/<name>.html/.css/.js/...")
    out = {k: v for k, v in files.items() if v.strip() or k in (MANIFEST_FILE, HANDLERS_FILE)}
    for required in (MANIFEST_FILE, HANDLERS_FILE):
        if not out.get(required, "").strip():
            raise NeaiError("BAD_REQUEST", f"{required} can't be empty")
    return out


def _review(files: dict[str, str]) -> list[dict[str, Any]]:
    problems = []
    for fname in (HANDLERS_FILE, WORKFLOWS_CODE):
        if fname in files:
            problems += [{**p, "file": fname} for p in codecheck.check(files[fname])]
    return problems
