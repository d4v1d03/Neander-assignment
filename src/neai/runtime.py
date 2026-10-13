from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from .errors import HandlerTimeout, NeaiError, error_envelope
from .handle import Context, DataHandle
from .loader import HANDLERS_FILE, app_traceback, import_handlers
from .paths import check_app_name, require_app, resolve_home
from .schema import Manifest, json_type, parse_manifest, validate_args
from .store import Store

DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_CALLER_LENGTH = 256
LOG_PREVIEW_CHARS = 2000
CALLER_MISTAKES = {"CALLER_REQUIRED", "CALLER_INVALID", "ARGS_INVALID", "UNKNOWN_HANDLER", "NO_ACTIVE_VERSION"}
SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie", re.I)


@contextmanager
def time_limit(seconds: float | None, *, what: str = "handler") -> Iterator[None]:
    usable = (
        seconds
        and seconds > 0
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def on_alarm(signum: int, frame: Any) -> None:
        raise HandlerTimeout(what)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class LoadedVersion:
    version: int
    hash: str
    manifest: Manifest
    module: ModuleType
    path: Path


class Runtime:
    def __init__(self, home: str | os.PathLike[str] | None, name: str, *, timeout: float | None = None):
        self.home = resolve_home(home)
        self.name = check_app_name(name)
        self.store = Store(require_app(self.home, self.name))
        if timeout is None:
            timeout = float(os.environ.get("NEAI_HANDLER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        self.timeout = timeout
        self._loaded: LoadedVersion | None = None

    def close(self) -> None:
        self.store.close()

    def refresh(self) -> LoadedVersion | None:
        active = self.store.active_version()
        if active is None:
            self._loaded = None
            return None
        if self._loaded and self._loaded.version == active:
            return self._loaded
        info = self.store.get_version(active)
        path = self.store.dir / "versions" / str(active) / HANDLERS_FILE
        try:
            with time_limit(self.timeout, what=f"importing {HANDLERS_FILE}"):
                module = import_handlers(path, f"neai_app_{self.name}_v{active}")
        except HandlerTimeout:
            raise NeaiError("HANDLER_IMPORT_ERROR", f"importing {HANDLERS_FILE} took longer than {self.timeout:g}s",
                            hint="don't do slow work at import time") from None
        self._loaded = LoadedVersion(active, info["hash"], parse_manifest(info["manifest"]), module, path)
        return self._loaded


    def call(self, handler: Any, caller: Any, args: Any = None) -> dict[str, Any]:
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
            caller = _check_caller(caller)
            args = _check_args(args)
            kind = loaded.manifest.handlers.get(handler) if isinstance(handler, str) else None
            if kind is None:
                available = [{"name": n, "kind": k} for n, k in loaded.manifest.handlers.items()]
                raise NeaiError(
                    "UNKNOWN_HANDLER", f"app '{self.name}' has no handler {handler!r}",
                    hint="available handlers: " + ", ".join(n for n in loaded.manifest.handlers),
                    details={"available": available},
                )
            if handler in loaded.manifest.handler_args:
                args = validate_args(handler, loaded.manifest.handler_args[handler], args)
            fn = getattr(loaded.module, handler)
            db = DataHandle(self.store, loaded.manifest, readonly=(kind == "read"), handler=handler,
                            source=loaded.path)
            ctx = Context(caller, db, handler, self.name, loaded.version)
            txn = self.store.read_transaction() if kind == "read" else self.store.transaction(write=True)
            with txn:
                result = self._invoke(fn, ctx, args, loaded.path)
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
        self.store.add_logs(entries)
        return response

    def _invoke(self, fn: Any, ctx: Context, args: dict[str, Any], path: Path) -> Any:
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
            frames = app_traceback(e, path)
            raise NeaiError(
                "HANDLER_EXCEPTION", f"{ctx.handler} raised {type(e).__name__}: {e}",
                hint="fix the bug in handlers.py, or raise AppError(code, message) for expected failures; "
                     "any writes it made were rolled back",
                details={"handler": ctx.handler, "exception": type(e).__name__, "traceback": frames},
            ) from None


def _check_caller(caller: Any) -> str:
    if caller is None or caller == "":
        raise NeaiError("CALLER_REQUIRED", "every call needs a caller identity",
                        hint='pass "caller" in the request body (CLI: --as <name>)')
    if not isinstance(caller, str):
        raise NeaiError("CALLER_INVALID", f"caller must be a string, got {json_type(caller)}")
    if caller != caller.strip() or not caller.isprintable() or len(caller) > MAX_CALLER_LENGTH:
        raise NeaiError("CALLER_INVALID",
                        f"caller must be printable, without leading/trailing spaces, at most {MAX_CALLER_LENGTH} chars",
                        hint="use a plain identity such as alice or user-42")
    return caller


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
