from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import NeaiError

SCALAR_TYPES = ("string", "integer", "number", "boolean")
FIELD_TYPES = SCALAR_TYPES + ("list", "object")
HANDLER_KINDS = ("read", "write")
RESERVED_FIELDS = ("id",)

APP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MANIFEST_KEYS = {"name", "description", "collections", "handlers"}
FIELD_KEYS = {"type", "required", "items"}
HANDLER_KEYS = {"kind", "args", "description"}


@dataclass(frozen=True)
class Field:
    type: str
    required: bool = True
    items: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "required": self.required}
        if self.items:
            out["items"] = self.items
        return out


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    collections: dict[str, dict[str, Field]]
    handlers: dict[str, str]
    handler_args: dict[str, dict[str, Field]] = field(default_factory=dict)
    handler_descriptions: dict[str, str] = field(default_factory=dict)

    def schema_dict(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {c: _fields_dict(fields) for c, fields in self.collections.items()}

    def handler_list(self) -> list[dict[str, Any]]:
        out = []
        for name, kind in self.handlers.items():
            h: dict[str, Any] = {"name": name, "kind": kind}
            if name in self.handler_args:
                h["args"] = _fields_dict(self.handler_args[name])
            if self.handler_descriptions.get(name):
                h["description"] = self.handler_descriptions[name]
            out.append(h)
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description:
            out["description"] = self.description
        out["collections"] = self.schema_dict()
        handlers: dict[str, Any] = {}
        for h in self.handler_list():
            extra = {k: v for k, v in h.items() if k not in ("name", "kind")}
            handlers[h["name"]] = {"kind": h["kind"], **extra} if extra else h["kind"]
        out["handlers"] = handlers
        return out


def _fields_dict(fields: dict[str, Field]) -> dict[str, dict[str, Any]]:
    return {f: spec.to_dict() for f, spec in fields.items()}


def parse_manifest(raw: Any) -> Manifest:
    errors: list[dict[str, str]] = []

    def bad(path: str, problem: str) -> None:
        errors.append({"path": path, "problem": problem})

    if not isinstance(raw, dict):
        bad("$", "app.json must contain a JSON object")
        _raise_manifest(errors)

    for key in sorted(set(raw) - MANIFEST_KEYS):
        bad(key, f"unknown key (allowed: {', '.join(sorted(MANIFEST_KEYS))})")

    name = raw.get("name")
    if not isinstance(name, str) or not APP_NAME_RE.match(name):
        bad("name", "must be a string of lowercase letters, digits, '_' or '-', starting with a letter (max 64)")

    description = raw.get("description", "")
    if not isinstance(description, str):
        bad("description", "must be a string")
        description = ""

    collections: dict[str, dict[str, Field]] = {}
    raw_collections = raw.get("collections")
    if not isinstance(raw_collections, dict):
        bad("collections", "must be an object mapping collection names to field definitions")
    else:
        for cname, raw_fields in raw_collections.items():
            cpath = f"collections.{cname}"
            if not IDENT_RE.match(cname):
                bad(cpath, "collection names must be lowercase identifiers (a-z, 0-9, _), starting with a letter")
            if not isinstance(raw_fields, dict) or not raw_fields:
                bad(cpath, 'must be a non-empty object mapping field names to {"type": ...}')
                continue
            collections[cname] = _parse_fields(raw_fields, cpath, bad, reserved=RESERVED_FIELDS)

    handlers: dict[str, str] = {}
    handler_args: dict[str, dict[str, Field]] = {}
    handler_descriptions: dict[str, str] = {}
    raw_handlers = raw.get("handlers")
    if not isinstance(raw_handlers, dict) or not raw_handlers:
        bad("handlers", 'must be a non-empty object mapping handler names to "read" or "write"')
    else:
        for hname, spec in raw_handlers.items():
            hpath = f"handlers.{hname}"
            if not IDENT_RE.match(hname):
                bad(hpath, "handler names must be lowercase Python identifiers (a-z, 0-9, _), starting with a letter")
            if isinstance(spec, str):
                kind, kind_path = spec, hpath
            elif isinstance(spec, dict):
                for key in sorted(set(spec) - HANDLER_KEYS):
                    bad(f"{hpath}.{key}", "unknown key (allowed: kind, args, description)")
                kind, kind_path = spec.get("kind"), f"{hpath}.kind"
                if "args" in spec:
                    if isinstance(spec["args"], dict):
                        handler_args[hname] = _parse_fields(spec["args"], f"{hpath}.args", bad, reserved=())
                    else:
                        bad(f"{hpath}.args", 'must be an object mapping argument names to {"type": ...}')
                desc = spec.get("description", "")
                if isinstance(desc, str):
                    handler_descriptions[hname] = desc
                else:
                    bad(f"{hpath}.description", "must be a string")
            else:
                bad(hpath, 'must be "read", "write", or {"kind": "read"|"write", "args": {...}}')
                continue
            if kind not in HANDLER_KINDS:
                bad(kind_path, f'must be "read" or "write" (got {kind!r})')
            handlers[hname] = kind

    if errors:
        _raise_manifest(errors)
    return Manifest(name=name, description=description, collections=collections, handlers=handlers,
                    handler_args=handler_args, handler_descriptions=handler_descriptions)


def _parse_fields(raw_fields: dict[str, Any], path: str, bad: Callable[[str, str], None], *,
                  reserved: tuple[str, ...]) -> dict[str, Field]:
    fields: dict[str, Field] = {}
    for fname, spec in raw_fields.items():
        fpath = f"{path}.{fname}"
        if fname in reserved:
            bad(fpath, f"'{fname}' is reserved: the platform assigns it on insert")
            continue
        if not IDENT_RE.match(fname):
            bad(fpath, "names must be lowercase identifiers (a-z, 0-9, _), starting with a letter")
        if not isinstance(spec, dict):
            bad(fpath, 'must be an object like {"type": "string"}')
            continue
        for key in sorted(set(spec) - FIELD_KEYS):
            bad(f"{fpath}.{key}", "unknown key (allowed: type, required, items)")
        ftype = spec.get("type")
        if ftype not in FIELD_TYPES:
            bad(f"{fpath}.type", f"must be one of: {', '.join(FIELD_TYPES)} (got {ftype!r})")
        required = spec.get("required", True)
        if not isinstance(required, bool):
            bad(f"{fpath}.required", "must be true or false")
            required = True
        items = spec.get("items")
        if items is not None and (ftype != "list" or items not in SCALAR_TYPES):
            bad(f"{fpath}.items", f"only lists take items, and items must be one of: {', '.join(SCALAR_TYPES)}")
            items = None
        fields[fname] = Field(type=ftype, required=required, items=items)
    return fields


def _raise_manifest(errors: list[dict[str, str]]) -> None:
    first = errors[0]
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    raise NeaiError(
        "MANIFEST_INVALID",
        f"app.json: {first['path']}: {first['problem']}{more}",
        hint="fix every entry in details.errors, then deploy again",
        details={"errors": errors},
    )


def type_problem(field: Field, value: Any) -> str | None:
    t = field.type
    if t == "list":
        if not isinstance(value, list):
            return f"expected list, got {json_type(value)}"
        for i, item in enumerate(value):
            problem = _scalar_problem(field.items, item) if field.items else _json_problem(item)
            if problem:
                return f"item {i}: {problem}"
        return None
    if t == "object":
        if not isinstance(value, dict):
            return f"expected object, got {json_type(value)}"
        return _json_problem(value)
    return _scalar_problem(t, value)


def _scalar_problem(t: str, value: Any) -> str | None:
    # bool is a subclass of int in Python; never let True/False pass as numbers.
    if t == "string":
        ok = isinstance(value, str)
    elif t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if ok and isinstance(value, float) and not math.isfinite(value):
            return "expected a finite number (NaN and infinity can't be stored)"
    elif t == "boolean":
        ok = isinstance(value, bool)
    else:
        return f"unknown field type {t!r}"
    return None if ok else f"expected {t}, got {json_type(value)}"


def _json_problem(value: Any) -> str | None:
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else "NaN and infinity can't be stored"
    if isinstance(value, list):
        return next((p for p in map(_json_problem, value) if p), None)
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            return "object keys must be strings"
        return next((p for p in map(_json_problem, value.values()) if p), None)
    return f"{json_type(value)} isn't JSON"


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def record_problems(fields: dict[str, Field], record: Any, *, reserved: tuple[str, ...] = RESERVED_FIELDS,
                    what: str = "a record") -> list[dict[str, str]]:
    if not isinstance(record, dict):
        return [{"field": "$", "problem": f"{what} must be an object, got {json_type(record)}"}]
    problems = []
    for key in record:
        if key in reserved:
            problems.append({"field": key, "problem": f"{key} is assigned by the platform and can't be set or changed"})
        elif key not in fields:
            declared = ", ".join(fields) or "none"
            noun = "argument" if what == "args" else "field"
            problems.append({"field": key, "problem": f"unknown {noun} (declared: {declared})"})
    for fname, spec in fields.items():
        value = record.get(fname)
        if value is None:
            if spec.required:
                problems.append({"field": fname, "problem": f"required {spec.type} is missing"})
            continue
        problem = type_problem(spec, value)
        if problem:
            problems.append({"field": fname, "problem": problem})
    return problems


def validate_record(collection: str, fields: dict[str, Field], record: Any) -> dict[str, Any]:
    problems = record_problems(fields, record)
    if problems:
        first = problems[0]
        raise NeaiError(
            "VALIDATION_ERROR",
            f"{collection}.{first['field']}: {first['problem']}",
            hint=f"records in '{collection}' must match its fields in app.json",
            details={"collection": collection, "errors": problems},
        )
    return {k: v for k, v in record.items() if v is not None}


def validate_args(handler: str, fields: dict[str, Field], args: dict[str, Any]) -> dict[str, Any]:
    problems = record_problems(fields, args, reserved=(), what="args")
    if problems:
        first = problems[0]
        declared = ", ".join(f"{n} ({f.type}{'' if f.required else ', optional'})" for n, f in fields.items())
        raise NeaiError(
            "ARGS_INVALID",
            f"{handler}: {first['field']}: {first['problem']}",
            hint=f"{handler} takes: {declared or 'no arguments'}",
            details={"handler": handler, "errors": problems, "declared": _fields_dict(fields)},
        )
    return {k: v for k, v in args.items() if v is not None}


def schema_diff(old: Manifest, new: Manifest) -> dict[str, Any]:
    diff: dict[str, Any] = {
        "added_collections": sorted(set(new.collections) - set(old.collections)),
        "removed_collections": sorted(set(old.collections) - set(new.collections)),
        "changed_collections": {},
    }
    for cname in sorted(set(old.collections) & set(new.collections)):
        o, n = old.collections[cname], new.collections[cname]
        change = {
            "added_fields": {f: n[f].to_dict() for f in n if f not in o},
            "removed_fields": sorted(f for f in o if f not in n),
            "changed_fields": {f: {"from": o[f].to_dict(), "to": n[f].to_dict()} for f in o if f in n and o[f] != n[f]},
        }
        change = {k: v for k, v in change.items() if v}
        if change:
            diff["changed_collections"][cname] = change
    return {k: v for k, v in diff.items() if v}
