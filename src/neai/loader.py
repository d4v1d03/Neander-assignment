from __future__ import annotations

import ast
import difflib
import hashlib
import inspect
import json
import linecache
import re
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from .errors import NeaiError
from .schema import Manifest, parse_manifest
from .workflow import WORKFLOWS_CODE, WORKFLOWS_FILE, attach, parse_workflows

MANIFEST_FILE = "app.json"
HANDLERS_FILE = "handlers.py"
MAX_DEFINITION_BYTES = 1_000_000


REQUIRED_FILES = (MANIFEST_FILE, HANDLERS_FILE)
TESTS_FILE = "tests.json"
OPTIONAL_FILES = (WORKFLOWS_FILE, WORKFLOWS_CODE, TESTS_FILE)

# The app's own UI: text files under ui/, stored with the version and served by the app's server.
UI_DIR = "ui"
UI_INDEX = "ui/index.html"
UI_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
}
MAX_UI_FILES = 50
MAX_UI_BYTES = 2_000_000
UI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
UI_CALL_RE = re.compile(r"""neai\.call\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def read_definition(source_dir: Path) -> tuple[dict[str, str], Manifest]:
    source_dir = Path(source_dir)
    files = {}
    for fname in REQUIRED_FILES:
        path = source_dir / fname
        if not path.is_file():
            raise NeaiError(
                "DEFINITION_NOT_FOUND",
                f"{path} does not exist",
                hint=f"an app folder needs {MANIFEST_FILE} and {HANDLERS_FILE}; create one with `neai new <name>`",
                details={"dir": str(source_dir), "missing": fname},
            )
        files[fname] = _read_text(path, fname)
    for fname in OPTIONAL_FILES:
        if (source_dir / fname).is_file():
            files[fname] = _read_text(source_dir / fname, fname)
    files.update(_read_ui(source_dir))
    return files, parse_definition(files)


def _read_ui(source_dir: Path) -> dict[str, str]:
    root = source_dir / UI_DIR
    if not root.is_dir():
        return {}

    def invalid(message: str, **details: Any) -> NeaiError:
        return NeaiError("UI_INVALID", message, details=details,
                         hint=f"ui/ holds text files ({', '.join(sorted(UI_TYPES))}) and must include index.html; "
                              "inline small images as SVG or data: URLs")

    files: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .DS_Store, editor swap files and the like
        if path.is_symlink():
            raise invalid(f"ui/{rel.as_posix()} is a symlink; copy the file in instead", file=f"ui/{rel.as_posix()}")
        if path.is_dir():
            continue
        key = f"{UI_DIR}/{rel.as_posix()}"
        if len(rel.parts) > 4 or not all(UI_NAME_RE.match(part) for part in rel.parts):
            raise invalid(f"{key}: names may use letters, digits, '.', '_' and '-' (at most 3 folders deep)", file=key)
        if path.suffix.lower() not in UI_TYPES:
            raise invalid(f"{key}: '{path.suffix or 'no extension'}' files can't be part of a UI", file=key)
        files[key] = _read_text(path, key)
        total += len(files[key].encode())
    if len(files) > MAX_UI_FILES:
        raise invalid(f"ui/ has {len(files)} files; the limit is {MAX_UI_FILES}", files=len(files))
    if total > MAX_UI_BYTES:
        raise invalid(f"ui/ holds {total} bytes; the limit is {MAX_UI_BYTES}", bytes=total)
    if files and UI_INDEX not in files:
        raise invalid("ui/ has files but no index.html, which is the page the app serves at /", files=sorted(files))
    return files


def definition_path_ok(key: str) -> bool:
    """Is this a file an app definition may contain (as the dashboard editor sends them)?"""
    if key in REQUIRED_FILES or key in OPTIONAL_FILES:
        return True
    parts = key.split("/")
    return (len(parts) >= 2 and parts[0] == UI_DIR and len(parts) <= 5 and all(UI_NAME_RE.match(p) for p in parts[1:])
            and Path(key).suffix.lower() in UI_TYPES)


def folder_files(source_dir: Path) -> dict[str, str]:
    """The definition files a folder holds right now, read leniently (for comparing, not deploying)."""
    out = {}
    for fname in (*REQUIRED_FILES, *OPTIONAL_FILES):
        path = source_dir / fname
        if path.is_file():
            out[fname] = path.read_text(errors="replace")
    root = source_dir / UI_DIR
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            key = f"{UI_DIR}/{path.relative_to(root).as_posix()}"
            if path.is_file() and not path.is_symlink() and definition_path_ok(key):
                out[key] = path.read_text(errors="replace")
    return out


def ui_files(files: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in files.items() if k.startswith(UI_DIR + "/")}


def ui_warnings(files: dict[str, str], manifest: Manifest) -> list[str]:
    """The UI is code we don't run, but we can check that it only calls handlers that exist."""
    warnings = []
    for key, text in sorted(ui_files(files).items()):
        if not key.endswith((".html", ".js")):
            continue
        for name in sorted(set(UI_CALL_RE.findall(text)) - set(manifest.handlers)):
            close = difflib.get_close_matches(name, list(manifest.handlers), n=1)
            warnings.append(f"{key} calls neai.call('{name}'), but app.json has no handler '{name}'"
                            + (f" (did you mean '{close[0]}'?)" if close else ""))
    return warnings


def _read_text(path: Path, fname: str) -> str:
    data = path.read_bytes()
    if len(data) > MAX_DEFINITION_BYTES:
        raise NeaiError("DEFINITION_TOO_LARGE", f"{fname} is larger than {MAX_DEFINITION_BYTES} bytes",
                        details={"file": fname, "bytes": len(data)})
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NeaiError("DEFINITION_INVALID", f"{fname} is not UTF-8 text (byte {e.start})",
                        details={"file": fname}) from None
    if "\x00" in text:
        raise NeaiError("DEFINITION_INVALID", f"{fname} contains a NUL character", details={"file": fname})
    return text


def parse_definition(files: dict[str, str]) -> Manifest:
    """The manifest the platform runs: app.json, plus the workflows from workflows.json."""
    manifest = _parse_app_json(files)
    return attach(manifest, parse_workflows(files.get(WORKFLOWS_FILE), manifest))


def _parse_app_json(files: dict[str, str]) -> Manifest:
    try:
        raw = json.loads(files[MANIFEST_FILE])
    except ValueError as e:
        line = getattr(e, "lineno", None)
        col = getattr(e, "colno", None)
        raise NeaiError(
            "MANIFEST_INVALID",
            f"{MANIFEST_FILE} is not valid JSON: {getattr(e, 'msg', e)}" + (f" (line {line}, column {col})" if line else ""),
            hint="app.json must be plain JSON: no comments, no trailing commas",
            details={"file": MANIFEST_FILE, "line": line, "column": col},
        ) from None
    return parse_manifest(raw)


def content_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b"\0")
        h.update(files[name].encode())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def code_filename(app: str, label: str, fname: str = HANDLERS_FILE) -> str:
    # Code comes from the database, not from disk; tracebacks and line numbers use this name.
    return f"<neai {app} {label}>/{fname}"


def import_source(code: str, filename: str, module_name: str) -> ModuleType:
    label = filename.rsplit("/", 1)[-1]
    module = ModuleType(module_name)
    module.__file__ = filename
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    try:
        exec(compile(code, filename, "exec"), module.__dict__)
    except SyntaxError as e:
        raise NeaiError(
            "HANDLER_IMPORT_ERROR",
            f"{label} line {e.lineno}: SyntaxError: {e.msg}",
            hint=f"fix the syntax error in {label}, then deploy again",
            details={"file": label, "line": e.lineno, "exception": "SyntaxError", "text": (e.text or "").strip()},
        ) from None
    except (Exception, SystemExit) as e:
        line = _app_line(e, filename)
        raise NeaiError(
            "HANDLER_IMPORT_ERROR",
            f"{label}" + (f" line {line}" if line else "") + f": {type(e).__name__} while importing: {e}",
            hint=f"{label} must import cleanly; only `neai` and the standard library are available",
            details={"file": label, "line": line, "exception": type(e).__name__,
                     "traceback": app_traceback(e, filename)},
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
WORKFLOW_MOVES = {"advance", "exit"}


def read_handler_writes(code: str, manifest: Manifest) -> list[str]:
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
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute)):
                continue
            target = {"db": WRITE_METHODS, "workflow": WORKFLOW_MOVES}.get(func.value.attr, set())
            if func.attr in target:
                warnings.append(
                    f"read handler '{node.name}' calls {func.value.attr}.{func.attr}() on line {sub.lineno}; that "
                    f'call will fail with READ_ONLY_VIOLATION (declare {node.name} as "write" in app.json)'
                )
    return warnings


def app_traceback(exc: BaseException, filenames: str | tuple[str, ...], limit: int = 8) -> list[str]:
    """The traceback lines that are in the app's own code (handlers.py, workflows.py)."""
    names = (filenames,) if isinstance(filenames, str) else filenames
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename in names]
    return [f"{f.filename.rsplit('/', 1)[-1]} line {f.lineno}, in {f.name}: {(f.line or '').strip()}"
            for f in frames[-limit:]]


def _app_line(exc: BaseException, filename: str) -> int | None:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == filename]
    return frames[-1].lineno if frames else None


def describe_value(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 200 else text[:197] + "..."
