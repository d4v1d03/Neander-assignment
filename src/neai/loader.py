from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from .errors import NeaiError
from .schema import Manifest, parse_manifest

MANIFEST_FILE = "app.json"
HANDLERS_FILE = "handlers.py"
MAX_DEFINITION_BYTES = 1_000_000


def read_definition(source_dir: Path) -> tuple[bytes, bytes, Manifest]:
    source_dir = Path(source_dir)
    files = {}
    for fname in (MANIFEST_FILE, HANDLERS_FILE):
        path = source_dir / fname
        if not path.is_file():
            raise NeaiError(
                "DEFINITION_NOT_FOUND",
                f"{path} does not exist",
                hint=f"an app folder needs {MANIFEST_FILE} and {HANDLERS_FILE}; create one with `neai new <name>`",
                details={"dir": str(source_dir), "missing": fname},
            )
        data = path.read_bytes()
        if len(data) > MAX_DEFINITION_BYTES:
            raise NeaiError("DEFINITION_TOO_LARGE", f"{fname} is larger than {MAX_DEFINITION_BYTES} bytes",
                            details={"file": fname, "bytes": len(data)})
        files[fname] = data

    try:
        raw = json.loads(files[MANIFEST_FILE])
    except (ValueError, UnicodeDecodeError) as e:
        line = getattr(e, "lineno", None)
        col = getattr(e, "colno", None)
        raise NeaiError(
            "MANIFEST_INVALID",
            f"{MANIFEST_FILE} is not valid JSON: {getattr(e, 'msg', e)}" + (f" (line {line}, column {col})" if line else ""),
            hint="app.json must be plain JSON: no comments, no trailing commas",
            details={"file": MANIFEST_FILE, "line": line, "column": col},
        ) from None
    return files[MANIFEST_FILE], files[HANDLERS_FILE], parse_manifest(raw)


def content_hash(manifest_bytes: bytes, code_bytes: bytes) -> str:
    h = hashlib.sha256()
    h.update(manifest_bytes)
    h.update(b"\0")
    h.update(code_bytes)
    return "sha256:" + h.hexdigest()


def import_handlers(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise NeaiError("HANDLER_IMPORT_ERROR", f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SyntaxError as e:
        raise NeaiError(
            "HANDLER_IMPORT_ERROR",
            f"{HANDLERS_FILE} line {e.lineno}: SyntaxError: {e.msg}",
            hint=f"fix the syntax error in {HANDLERS_FILE}, then deploy again",
            details={"file": HANDLERS_FILE, "line": e.lineno, "exception": "SyntaxError", "text": (e.text or "").strip()},
        ) from None
    except (Exception, SystemExit) as e:
        line = _app_line(e, path)
        raise NeaiError(
            "HANDLER_IMPORT_ERROR",
            f"{HANDLERS_FILE}" + (f" line {line}" if line else "") + f": {type(e).__name__} while importing: {e}",
            hint=f"{HANDLERS_FILE} must import cleanly; only `neai` and the standard library are available",
            details={"file": HANDLERS_FILE, "line": line, "exception": type(e).__name__,
                     "traceback": app_traceback(e, path)},
        ) from None
    return module


def check_handlers(module: ModuleType, manifest: Manifest) -> list[str]:
    missing, bad_signature = [], []
    for name in manifest.handlers:
        fn = getattr(module, name, None)
        if not callable(fn):
            missing.append(name)
            continue
        try:
            inspect.signature(fn).bind(object(), {})
        except TypeError:
            bad_signature.append(name)
        except ValueError:
            pass
    if missing:
        defined = sorted(n for n, v in vars(module).items() if inspect.isfunction(v) and not n.startswith("_"))
        raise NeaiError(
            "HANDLER_MISSING",
            f"app.json declares handler(s) not defined in {HANDLERS_FILE}: {', '.join(missing)}",
            hint=f"define `def {missing[0]}(ctx, args):` in {HANDLERS_FILE}, or remove it from app.json",
            details={"missing": missing, "defined_functions": defined},
        )
    if bad_signature:
        raise NeaiError(
            "HANDLER_SIGNATURE",
            f"handler(s) can't be called as fn(ctx, args): {', '.join(bad_signature)}",
            hint="every handler takes exactly two arguments: def name(ctx, args):",
            details={"handlers": bad_signature},
        )
    warnings = []
    for n, v in sorted(vars(module).items()):
        if inspect.isfunction(v) and v.__module__ == module.__name__ and not n.startswith("_") and n not in manifest.handlers:
            warnings.append(
                f"function '{n}' in {HANDLERS_FILE} is not declared in app.json, so it can't be called "
                f"(declare it, or rename it to _{n} if it's a helper)"
            )
    return warnings


WRITE_METHODS = {"insert", "update", "delete"}


def read_handler_writes(code: bytes, manifest: Manifest) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    warnings = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and manifest.handlers.get(node.name) == "read"):
            continue
        for sub in ast.walk(node):
            func = sub.func if isinstance(sub, ast.Call) else None
            if (isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS
                    and isinstance(func.value, ast.Attribute) and func.value.attr == "db"):
                warnings.append(
                    f"read handler '{node.name}' calls db.{func.attr}() on line {sub.lineno}; that call will fail "
                    f'with READ_ONLY_VIOLATION (declare {node.name} as "write" in app.json)'
                )
    return warnings


def app_traceback(exc: BaseException, path: Path, limit: int = 8) -> list[str]:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename) == Path(path)]
    return [f"{HANDLERS_FILE} line {f.lineno}, in {f.name}: {(f.line or '').strip()}" for f in frames[-limit:]]


def _app_line(exc: BaseException, path: Path) -> int | None:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename) == Path(path)]
    return frames[-1].lineno if frames else None


def describe_value(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 200 else text[:197] + "..."
