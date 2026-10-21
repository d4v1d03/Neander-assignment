from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Iterator

from .auth import USERNAME_RE, Accounts, User
from .database import Database
from .errors import HandlerTimeout, NeaiError, error_envelope
from .handle import Context, DataHandle
from .loader import HANDLERS_FILE, app_traceback, code_filename, import_source, parse_definition
from .paths import check_app_name
from .schema import Manifest, json_type, validate_args
from .store import require_app
from .workflow import WORKFLOWS_CODE, WorkflowHandle

DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_CALLER_LENGTH = 256
LOG_PREVIEW_CHARS = 2000
# Logged as "warn" (the call was refused) rather than "error" (something is broken).
CALLER_MISTAKES = {"CALLER_INVALID", "ARGS_INVALID", "UNKNOWN_HANDLER", "NO_ACTIVE_VERSION", "UNIQUE_VIOLATION",
                   "AUTH_REQUIRED", "AUTH_INVALID", "FORBIDDEN", "WORKFLOW_FORBIDDEN", "WORKFLOW_FINISHED"}
AUTH_ACTIONS = ("signup", "login", "logout", "me")
SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie", re.I)


@contextmanager
def time_limit(seconds: float | None, *, what: str = "handler") -> Iterator[None]:
    """Raise HandlerTimeout in this thread if the block runs longer than `seconds`.

    On the main thread a SIGALRM interrupts it (even mid-sleep). Servers run each request on a
    worker thread, which signals can't reach, so there a watchdog thread raises the exception
    into the worker instead. Either way it lands between two Python bytecodes: code blocked
    inside a C call is stopped once that call returns, and a stuck query by statement_timeout.
    """
    if not seconds or seconds <= 0:
        yield
        return
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer"):
        with _alarm(seconds, what):
            yield
        return
    if _set_async_exc is None:  # not CPython: no way to interrupt a thread
        yield
        return
    with _watchdog(seconds):
        yield


@contextmanager
def _alarm(seconds: float, what: str) -> Iterator[None]:
    def on_alarm(signum: int, frame: Any) -> None:
        raise HandlerTimeout(what)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def _watchdog(seconds: float) -> Iterator[None]:
    target = threading.get_ident()
    guard = threading.Lock()
    state = {"done": False, "fired": False}

    def fire() -> None:
        # Under the guard, so it can't fire once the block below has finished: the exception
        # must never land in the platform's own code after the handler returned.
        with guard:
            if not state["done"]:
                state["fired"] = True
                _set_async_exc(ctypes.c_ulong(target), ctypes.py_object(HandlerTimeout))

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        with guard:
            state["done"] = True
            fired = state["fired"]
        timer.cancel()
        if fired:
            # It may not have been delivered yet: cancel it, and raise it here, at a known point.
            _set_async_exc(ctypes.c_ulong(target), None)
            raise HandlerTimeout()


_set_async_exc = getattr(getattr(ctypes, "pythonapi", None), "PyThreadState_SetAsyncExc", None)


@dataclass(frozen=True)
class LoadedVersion:
    version: int
    hash: str
    manifest: Manifest               # app.json + workflows.json
    module: ModuleType               # handlers.py
    flows: ModuleType | None         # workflows.py, if the app has one
    sources: tuple[str, ...]         # the code's file names, for tracebacks and line numbers
    files: dict[str, str]            # every file of the version, including the UI under ui/


class Runtime:
    """Runs one app's handlers. Holds no state of its own: data and code come from the database."""

    def __init__(self, database: str | None, name: str, *, timeout: float | None = None):
        self.name = check_app_name(name)
        if timeout is None:
            timeout = float(os.environ.get("NEAI_HANDLER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        self.timeout = timeout
        # The database also stops a single runaway query, a second after the handler's own limit,
        # and a transaction left idle half a second past it.
        self.db = Database(database, statement_timeout=timeout + 1 if timeout else None,
                           idle_timeout=timeout + 0.5 if timeout else None)
        self.store = require_app(self.db, self.name)
        self.accounts = Accounts(self.store)
        self._loaded: LoadedVersion | None = None
        # Many request threads may notice a new deploy at once; one loads it, the rest wait and reuse it.
        self._reload_lock = threading.Lock()

    @classmethod
    def sandbox(cls, db: Database, store: Any, loaded: LoadedVersion) -> "Runtime":
        """A runtime for a version that isn't active (or stored) yet, used by deploy to run tests.json."""
        rt = cls.__new__(cls)
        rt.name = loaded.manifest.name
        rt.timeout = float(os.environ.get("NEAI_HANDLER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        rt.db, rt.store, rt.accounts = db, store, Accounts(store)
        rt._loaded, rt._fixed = loaded, True
        rt._reload_lock = threading.Lock()
        return rt

    def close(self) -> None:
        self.db.close()

    def refresh(self) -> LoadedVersion | None:
        if getattr(self, "_fixed", False):
            return self._loaded
        active = self.store.active_version()
        if active is None:
            self._loaded = None
            return None
        loaded = self._loaded
        if loaded and loaded.version == active:
            return loaded  # the usual case: no lock, since a loaded version never changes
        with self._reload_lock:
            loaded = self._loaded
            if loaded and loaded.version == active:
                return loaded  # another thread loaded it while this one waited
            loaded = self._load(active)
            self._loaded = loaded
            return loaded

    def _load(self, active: int) -> LoadedVersion:
        info = self.store.get_version(active, files=True)
        files = info["files"]
        path = code_filename(self.name, f"v{active}")
        flows_path = code_filename(self.name, f"v{active}", WORKFLOWS_CODE)
        try:
            with time_limit(self.timeout, what="importing the app's code"):
                module = import_source(files[HANDLERS_FILE], path, f"neai_app_{self.name}_v{active}")
                flows = (import_source(files[WORKFLOWS_CODE], flows_path, f"neai_flows_{self.name}_v{active}")
                         if WORKFLOWS_CODE in files else None)
        except HandlerTimeout:
            raise NeaiError("HANDLER_IMPORT_ERROR", f"importing the app's code took longer than {self.timeout:g}s",
                            hint="don't do slow work at import time") from None
        sources = (path, flows_path) if flows else (path,)
        return LoadedVersion(active, info["hash"], parse_definition(files), module, flows, sources, files)

    def contract(self) -> dict[str, Any]:
        """What a UI needs to know about the app, and nothing more: no code, no data, no rule details."""
        loaded = self.refresh()
        if loaded is None:
            raise NeaiError("NO_ACTIVE_VERSION", f"app '{self.name}' has no active version",
                            hint="deploy a valid definition with `neai deploy <dir>`")
        m = loaded.manifest
        workflows = {
            name: {"collection": wf.collection, "field": wf.field, "exits": list(wf.exits),
                   "steps": [{"name": s.name, **({"by": list(s.by)} if s.by else {})} for s in wf.steps]}
            for name, wf in m.workflows.items()
        }
        return {"ok": True, "app": m.name, "description": m.description, "version": loaded.version,
                "auth": m.auth.to_dict(), "handlers": m.handler_list(), "workflows": workflows}


    def call(self, handler: Any, caller: Any = None, args: Any = None, *, token: str | None = None) -> dict[str, Any]:
        """Run one handler call.

        Over HTTP the caller is always the owner of `token` (a login token). In-process callers
        (tests, the platform itself) may pass `caller` instead: a User, a username, or None for
        an anonymous call. Either way, the handler's access rule is checked before it runs.
        """
        started = time.perf_counter()
        log: dict[str, Any] = {
            "event": "call",
            "handler": handler if isinstance(handler, str) else None,
            "caller": caller if isinstance(caller, str) else None,
        }
        ctx: Context | None = None
        try:
            loaded = self.refresh()
            if loaded is None:
                raise NeaiError("NO_ACTIVE_VERSION", f"app '{self.name}' has no active version",
                                hint="deploy a valid definition with `neai deploy <dir>`")
            log["version"] = loaded.version
            user = self.accounts.user_for_token(token) if token is not None else _check_caller(caller)
            log["caller"] = user.username if user else None
            args = _check_args(args)
            kind = loaded.manifest.handlers.get(handler) if isinstance(handler, str) else None
            if kind is None:
                available = [{"name": n, "kind": k} for n, k in loaded.manifest.handlers.items()]
                raise NeaiError(
                    "UNKNOWN_HANDLER", f"app '{self.name}' has no handler {handler!r}",
                    hint="available handlers: " + ", ".join(n for n in loaded.manifest.handlers),
                    details={"available": available},
                )
            _check_access(loaded.manifest, handler, user)
            if handler in loaded.manifest.handler_args:
                args = validate_args(handler, loaded.manifest.handler_args[handler], args)
            fn = getattr(loaded.module, handler)
            db = DataHandle(self.store, loaded.manifest, readonly=(kind == "read"), handler=handler,
                            sources=loaded.sources)
            ctx = Context(user, db, handler, self.name, loaded.version)
            ctx.workflow = WorkflowHandle(ctx, db, loaded.manifest, loaded.flows)
            db.on_insert = ctx.workflow._start
            txn = self.store.read_transaction() if kind == "read" else self.store.transaction(write=True)
            with txn:
                result = self._invoke(fn, ctx, args, loaded.sources)
                if db.violations:
                    # The handler caught READ_ONLY_VIOLATION and carried on; the call still fails.
                    raise db.violations[0]
                result = _plain_json(result, handler)
            response: dict[str, Any] = {"ok": True, "result": result, "version": loaded.version}
        except NeaiError as err:
            response = error_envelope(err)
            if "version" in log:
                response["version"] = log["version"]

        if response["ok"]:
            log["level"], log["message"] = "info", f"{log['handler']} ok"
        elif response["error"]["kind"] == "app" or response["error"]["code"] in CALLER_MISTAKES:
            log["level"], log["message"] = "warn", f"{log['handler']} refused: {response['error']['code']}"
        else:
            log["level"], log["message"] = "error", f"{log['handler']} failed: {response['error']['code']}"
        log["data"] = {"args": _preview(args), "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
        if not response["ok"]:
            log["data"]["error"] = response["error"]
        entries = []
        if ctx is not None:
            for line in ctx.logs:
                entries.append({"event": "log", "at": line["at"], "handler": ctx.handler, "caller": ctx.caller,
                                "version": ctx.version, "message": line["message"], "data": _preview(line["fields"])})
        entries.append(log)
        try:
            self.store.add_logs(entries)
        except NeaiError:
            pass  # the database is unreachable; the response already says so
        return response

    def auth(self, action: str, body: dict[str, Any] | None = None, *, token: str | None = None) -> dict[str, Any]:
        """Sign-up, login, logout and "who am I" for this app's users. Passwords are never logged."""
        body = body or {}
        username = body.get("username")
        log: dict[str, Any] = {"event": "auth", "handler": None,
                               "caller": username if isinstance(username, str) and USERNAME_RE.match(username) else None}
        try:
            loaded = self.refresh()
            if loaded is None:
                raise NeaiError("NO_ACTIVE_VERSION", f"app '{self.name}' has no active version",
                                hint="deploy a valid definition with `neai deploy <dir>`")
            log["version"] = loaded.version
            if action == "signup":
                response = {"ok": True, **self.accounts.signup(loaded.manifest, username, body.get("password"))}
            elif action == "login":
                response = {"ok": True, **self.accounts.login(username, body.get("password"))}
            elif token is None:
                raise NeaiError("AUTH_REQUIRED", f"{action} needs a login token",
                                hint="send Authorization: Bearer <token>")
            elif action == "logout":
                response = {"ok": True, "status": "logged_out", "user": self.accounts.logout(token).to_dict()}
            elif action == "me":
                response = {"ok": True, "user": self.accounts.user_for_token(token).to_dict()}
            else:
                raise NeaiError("ROUTE_NOT_FOUND", f"no auth action {action!r}",
                                hint="use /auth/signup, /auth/login, /auth/logout or /auth/me")
        except NeaiError as err:
            response = error_envelope(err)
        if response["ok"]:
            log["caller"] = response["user"]["username"]
            log["level"], log["message"] = "info", f"{action} ok"
        else:
            log["level"], log["message"] = "warn", f"{action} refused: {response['error']['code']}"
        if action != "me" or not response["ok"]:  # "me" is a lookup; logging every one would be noise
            try:
                self.store.add_logs([log])
            except NeaiError:
                pass
        return response

    def _invoke(self, fn: Any, ctx: Context, args: dict[str, Any], sources: tuple[str, ...]) -> Any:
        try:
            with time_limit(self.timeout, what=ctx.handler):
                return fn(ctx, args)
        except HandlerTimeout:
            raise NeaiError(
                "HANDLER_TIMEOUT", f"{ctx.handler} ran longer than {self.timeout:g}s and was stopped",
                hint="look for an infinite loop; any writes it made were rolled back "
                     "(the limit is NEAI_HANDLER_TIMEOUT)",
                details={"handler": ctx.handler, "timeout_seconds": self.timeout},
            ) from None
        except NeaiError:
            raise
        except (Exception, SystemExit) as e:
            frames = app_traceback(e, sources)
            raise NeaiError(
                "HANDLER_EXCEPTION", f"{ctx.handler} raised {type(e).__name__}: {e}",
                hint="fix the bug at the lines in details.traceback (handlers.py or workflows.py), or raise "
                     "AppError(code, message) for expected failures; any writes it made were rolled back",
                details={"handler": ctx.handler, "exception": type(e).__name__, "traceback": frames},
            ) from None


def _check_caller(caller: Any) -> User | None:
    if caller is None or isinstance(caller, User):
        return caller
    if not isinstance(caller, str):
        raise NeaiError("CALLER_INVALID", f"caller must be a User or a username, got {json_type(caller)}")
    if not caller or caller != caller.strip() or not caller.isprintable() or len(caller) > MAX_CALLER_LENGTH:
        raise NeaiError("CALLER_INVALID",
                        f"caller must be printable, without leading/trailing spaces, at most {MAX_CALLER_LENGTH} chars",
                        hint="use a plain identity such as alice or user-42")
    return User(caller)


def _check_access(manifest: Manifest, handler: str, user: User | None) -> None:
    access = manifest.access_for(handler)
    if access == "public":
        return
    if user is None:
        raise NeaiError(
            "AUTH_REQUIRED", f"{handler} needs a logged-in user",
            hint=f"log in first (POST /auth/login, or `neai login {manifest.name} <username>`), then send "
                 "Authorization: Bearer <token> (the CLI does this for you with --as <username>)",
            details={"handler": handler, "access": access},
        )
    if access == "user" or set(user.roles) & set(access):
        return
    raise NeaiError(
        "FORBIDDEN", f"{handler} needs the role {' or '.join(access)}; {user.username} has "
                     + (", ".join(user.roles) or "no roles"),
        hint=f"the operator grants roles: neai users {manifest.name} grant {user.username} {access[0]}",
        details={"handler": handler, "required_roles": list(access), "your_roles": list(user.roles)},
    )


def _check_args(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise NeaiError("ARGS_INVALID", f"args must be a JSON object, got {json_type(args)}",
                        hint='e.g. {"title": "hello"}')
    return args


def _plain_json(result: Any, handler: str) -> Any:
    try:
        return json.loads(json.dumps(result, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise NeaiError(
            "HANDLER_BAD_RESULT", f"{handler} returned a value that isn't plain JSON: {e}",
            hint="return dicts, lists, strings, numbers, booleans or None; the call was rolled back",
            details={"handler": handler},
        ) from None


def _preview(value: Any) -> Any:
    redacted = _redact(value)
    text = json.dumps(redacted, default=repr)
    if len(text) <= LOG_PREVIEW_CHARS:
        return json.loads(text)
    return {"_truncated": text[:LOG_PREVIEW_CHARS]}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if isinstance(k, str) and SENSITIVE_KEY.search(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
