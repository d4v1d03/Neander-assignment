from __future__ import annotations

import copy
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

MANIFEST_KEYS = {"name", "description", "auth", "collections", "handlers", "unique"}
FIELD_KEYS = {"type", "required", "items", "default"}
HANDLER_KEYS = {"kind", "args", "description", "access"}
AUTH_KEYS = {"signup", "roles"}
SIGNUP_MODES = ("open", "closed")
# Access levels a handler can have besides a list of roles; they can't be role names.
ACCESS_LEVELS = ("public", "user")
DEFAULT_ACCESS = "user"


@dataclass(frozen=True)
class Field:
    type: str
    required: bool = True
    items: str | None = None
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "required": self.required}
        if self.items:
            out["items"] = self.items
        if self.default is not None:
            out["default"] = self.default
        return out


@dataclass(frozen=True)
class AuthConfig:
    signup: str = "open"          # "open": anyone can sign up; "closed": the operator creates accounts
    roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"signup": self.signup, "roles": list(self.roles)}


@dataclass(frozen=True)
class Manifest:
    name: str
    description: str
    collections: dict[str, dict[str, Field]]
    handlers: dict[str, str]
    handler_args: dict[str, dict[str, Field]] = field(default_factory=dict)
    handler_descriptions: dict[str, str] = field(default_factory=dict)
    unique: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    auth: AuthConfig = field(default_factory=AuthConfig)
    # "public", "user", or a tuple of roles (any one of them is enough)
    handler_access: dict[str, Any] = field(default_factory=dict)
    # From workflows.json (see workflow.py); not part of app.json or to_dict().
    workflows: dict[str, Any] = field(default_factory=dict)

    def access_for(self, handler: str) -> Any:
        return self.handler_access.get(handler, DEFAULT_ACCESS)

    def schema_dict(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {c: _fields_dict(fields) for c, fields in self.collections.items()}

    def unique_dict(self) -> dict[str, list[list[str]]]:
        return {c: [list(combo) for combo in combos] for c, combos in self.unique.items()}

    def data_contract(self) -> dict[str, Any]:
        # What stored records must satisfy; when it changes, deploy re-checks the stored data.
        return {"collections": self.schema_dict(), "unique": self.unique_dict(),
                "workflows": {name: wf.summary() for name, wf in self.workflows.items()}}

    def handler_list(self) -> list[dict[str, Any]]:
        out = []
        for name, kind in self.handlers.items():
            access = self.access_for(name)
            h: dict[str, Any] = {"name": name, "kind": kind,
                                 "access": list(access) if isinstance(access, tuple) else access}
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
        out["auth"] = self.auth.to_dict()
        out["collections"] = self.schema_dict()
        if self.unique:
            out["unique"] = self.unique_dict()
        handlers: dict[str, Any] = {}
        for h in self.handler_list():
            extra = {k: v for k, v in h.items()
                     if k not in ("name", "kind") and not (k == "access" and v == DEFAULT_ACCESS)}
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

    auth = _parse_auth(raw.get("auth"), bad)

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
    handler_access: dict[str, Any] = {}
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
                    bad(f"{hpath}.{key}", "unknown key (allowed: kind, args, description, access)")
                kind, kind_path = spec.get("kind"), f"{hpath}.kind"
                if "args" in spec:
                    if isinstance(spec["args"], dict):
                        handler_args[hname] = _parse_fields(spec["args"], f"{hpath}.args", bad, reserved=())
                    else:
                        bad(f"{hpath}.args", 'must be an object mapping argument names to {"type": ...}')
                if "access" in spec:
                    handler_access[hname] = _parse_access(spec["access"], f"{hpath}.access", auth.roles, bad)
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

    unique = _parse_unique(raw.get("unique", {}), collections, bad)

    if errors:
        _raise_manifest(errors)
    return Manifest(name=name, description=description, collections=collections, handlers=handlers,
                    handler_args=handler_args, handler_descriptions=handler_descriptions, unique=unique,
                    auth=auth, handler_access=handler_access)


def _parse_auth(raw: Any, bad: Callable[[str, str], None]) -> AuthConfig:
    if raw is None:
        return AuthConfig()
    if not isinstance(raw, dict):
        bad("auth", 'must be an object like {"signup": "open", "roles": ["admin"]}')
        return AuthConfig()
    for key in sorted(set(raw) - AUTH_KEYS):
        bad(f"auth.{key}", f"unknown key (allowed: {', '.join(sorted(AUTH_KEYS))})")
    signup = raw.get("signup", "open")
    if signup not in SIGNUP_MODES:
        bad("auth.signup", 'must be "open" (anyone can sign up) or "closed" (the operator creates accounts)')
        signup = "open"
    raw_roles = raw.get("roles", [])
    if not isinstance(raw_roles, list):
        bad("auth.roles", 'must be a list of role names, e.g. ["admin"]')
        raw_roles = []
    roles: list[str] = []
    for i, role in enumerate(raw_roles):
        if not isinstance(role, str) or not IDENT_RE.match(role):
            bad(f"auth.roles[{i}]", "role names are lowercase identifiers (a-z, 0-9, _), starting with a letter")
        elif role in ACCESS_LEVELS:
            bad(f"auth.roles[{i}]", f"'{role}' is an access level, not a role; pick another name")
        elif role in roles:
            bad(f"auth.roles[{i}]", f"'{role}' is listed twice")
        else:
            roles.append(role)
    return AuthConfig(signup=signup, roles=tuple(roles))


def _parse_access(value: Any, path: str, roles: tuple[str, ...], bad: Callable[[str, str], None]) -> Any:
    if value in ACCESS_LEVELS:
        return value
    wanted = [value] if isinstance(value, str) else value
    if not isinstance(wanted, list) or not wanted or not all(isinstance(r, str) for r in wanted):
        bad(path, 'must be "public", "user", a role name, or a list of role names')
        return DEFAULT_ACCESS
    unknown = [r for r in wanted if r not in roles]
    if unknown:
        bad(path, f"unknown role(s): {', '.join(unknown)} (declare roles in auth.roles; "
                  f"declared: {', '.join(roles) or 'none'})")
        return DEFAULT_ACCESS
    return tuple(dict.fromkeys(wanted))


def _parse_unique(raw: Any, collections: dict[str, dict[str, Field]],
                  bad: Callable[[str, str], None]) -> dict[str, list[tuple[str, ...]]]:
    example = '{"votes": [["post_id", "voter"]]}'
    if not isinstance(raw, dict):
        bad("unique", f"must be an object mapping collections to lists of unique field sets, e.g. {example}")
        return {}
    unique: dict[str, list[tuple[str, ...]]] = {}
    for cname, combos in raw.items():
        upath = f"unique.{cname}"
        fields = collections.get(cname)
        if fields is None:
            bad(upath, f"no collection named '{cname}'")
            continue
        if not isinstance(combos, list) or not combos:
            bad(upath, f"must be a non-empty list of field lists, e.g. {example}")
            continue
        parsed = []
        for i, combo in enumerate(combos):
            cpath = f"{upath}[{i}]"
            if isinstance(combo, str):
                combo = [combo]
            if not isinstance(combo, list) or not combo or not all(isinstance(f, str) for f in combo):
                bad(cpath, 'must be a field name or a list of field names, e.g. ["post_id", "voter"]')
                continue
            if len(set(combo)) != len(combo):
                bad(cpath, "names a field more than once")
                continue
            ok = True
            for fname in combo:
                if fname not in fields:
                    bad(cpath, f"unknown field '{fname}' (fields: {', '.join(fields)})")
                    ok = False
                elif fields[fname].type in ("list", "object"):
                    bad(cpath, f"'{fname}' is a {fields[fname].type}; only scalar fields can be unique")
                    ok = False
            if ok:
                parsed.append(tuple(combo))
        unique[cname] = parsed
    return unique


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
            bad(f"{fpath}.{key}", "unknown key (allowed: type, required, items, default)")
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
        default = spec.get("default")
        if "default" in spec:
            if default is None:
                bad(f"{fpath}.default", "can't be null; leave the default out instead")
            elif ftype in FIELD_TYPES and (problem := type_problem(Field(ftype, items=items), default)):
                bad(f"{fpath}.default", f"doesn't match the field's type: {problem}")
                default = None
        fields[fname] = Field(type=ftype, required=required, items=items, default=default)
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
        if ok and "\x00" in value:
            return "strings can't contain the NUL character (\\u0000)"
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
    if isinstance(value, str):
        return "strings can't contain the NUL character (\\u0000)" if "\x00" in value else None
    if value is None or isinstance(value, (bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else "NaN and infinity can't be stored"
    if isinstance(value, list):
        return next((p for p in map(_json_problem, value) if p), None)
    if isinstance(value, dict):
        if not all(isinstance(k, str) and "\x00" not in k for k in value):
            return "object keys must be strings without the NUL character"
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


def with_defaults(fields: dict[str, Field], record: Any) -> Any:
    if not isinstance(record, dict):
        return record
    missing = {f: copy.deepcopy(spec.default) for f, spec in fields.items()
               if spec.default is not None and record.get(f) is None}
    return {**record, **missing} if missing else record


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
    args = with_defaults(fields, args)
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
    if old.unique_dict() != new.unique_dict():
        diff["unique"] = {"from": old.unique_dict(), "to": new.unique_dict()}
    return {k: v for k, v in diff.items() if v}
