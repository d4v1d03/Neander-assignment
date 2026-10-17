from __future__ import annotations

import json
import traceback
from typing import Any

from .errors import NeaiError
from .schema import Field, Manifest, json_type, type_problem, validate_record, with_defaults
from .store import Store, now

MAX_FIND_LIMIT = 10_000


class DataHandle:
    def __init__(self, store: Store, manifest: Manifest, *, readonly: bool, handler: str,
                 sources: tuple[str, ...] = ()):
        self._sources = sources  # the app's code files, so errors can name the line that caused them
        self._store = store
        self._manifest = manifest
        self._readonly = readonly
        self._handler = handler
        self.violations: list[NeaiError] = []
        # Set by the runtime: starts a new record's workflow, if its collection has one.
        self.on_insert: Any = None
        self._managed = {wf.collection: (wf.name, wf.field) for wf in manifest.workflows.values()}

    @property
    def readonly(self) -> bool:
        return self._readonly


    def get(self, collection: str, id: int) -> dict[str, Any] | None:
        fields = self._fields(collection)
        if not _is_id(id):
            return None
        record = self._store.get_record(collection, id)
        return _shape(fields, record) if record else None

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        fields = self._fields(collection)
        conditions = self._where(collection, fields, where)
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= MAX_FIND_LIMIT):
            raise NeaiError(
                "INVALID_QUERY", f"limit must be an integer between 1 and {MAX_FIND_LIMIT}, got {limit!r}",
                details={"collection": collection},
            )
        return [_shape(fields, r) for r in self._store.find_records(collection, conditions, limit)]

    def count(self, collection: str, where: dict[str, Any] | None = None) -> int:
        fields = self._fields(collection)
        return self._store.count_records(collection, self._where(collection, fields, where))


    def insert(self, collection: str, record: dict[str, Any]) -> dict[str, Any]:
        self._check_writable("insert", collection)
        fields = self._fields(collection)
        self._check_unmanaged(collection, record)
        data = validate_record(collection, fields, with_defaults(fields, record))
        self._check_unique(collection, data, None)
        new_id = self._store.insert_record(collection, data)
        stored = _shape(fields, {"id": new_id, **data})
        if self.on_insert is not None and collection in self._managed:
            stored = self.on_insert(collection, stored)
        return stored

    def update(self, collection: str, id: int, changes: dict[str, Any]) -> dict[str, Any]:
        self._check_writable("update", collection)
        fields = self._fields(collection)
        if not isinstance(changes, dict):
            raise NeaiError(
                "VALIDATION_ERROR", f"update changes must be an object, got {json_type(changes)}",
                details={"collection": collection},
            )
        self._check_unmanaged(collection, changes)
        existing = self._store.get_record(collection, id) if _is_id(id) else None
        if existing is None:
            raise NeaiError(
                "RECORD_NOT_FOUND", f"no record with id {id!r} in '{collection}'",
                hint="check the id with db.get() before updating",
                details={"collection": collection, "id": id},
            )
        current = {k: v for k, v in existing.items() if k != "id"}
        data = validate_record(collection, fields, with_defaults(fields, {**current, **changes}))
        self._check_unique(collection, data, id)
        self._store.replace_record(collection, id, data)
        return _shape(fields, {"id": id, **data})

    def delete(self, collection: str, id: int) -> bool:
        self._check_writable("delete", collection)
        self._fields(collection)
        if not _is_id(id):
            return False
        return self._store.delete_record(collection, id)


    def _fields(self, collection: str) -> dict[str, Field]:
        fields = self._manifest.collections.get(collection) if isinstance(collection, str) else None
        if fields is None:
            declared = sorted(self._manifest.collections)
            raise NeaiError(
                "UNKNOWN_COLLECTION",
                f"app '{self._manifest.name}' has no collection {collection!r}",
                hint=f"declared collections: {', '.join(declared)}; add new ones to app.json and deploy",
                details={"collection": collection, "declared": declared},
            )
        return fields

    def _check_unique(self, collection: str, data: dict[str, Any], own_id: int | None) -> None:
        # Safe without a database constraint: write handlers hold the app's write lock, so no
        # other write to this app can run between this check and the insert.
        for combo in self._manifest.unique.get(collection, ()):
            if any(data.get(f) is None for f in combo):
                continue
            values = {f: data[f] for f in combo}
            clash = next((r for r in self._store.find_records(collection, values, limit=2) if r["id"] != own_id), None)
            if clash:
                shown = ", ".join(f"{f}={json.dumps(v)}" for f, v in values.items())
                raise NeaiError(
                    "UNIQUE_VIOLATION", f"'{collection}' already has a record with {shown} (id {clash['id']})",
                    hint=f"app.json makes ({', '.join(combo)}) unique in '{collection}'; "
                         "look for the existing record with ctx.db.find before inserting",
                    details={"collection": collection, "fields": list(combo), "values": values,
                             "existing_id": clash["id"]},
                )

    def _check_unmanaged(self, collection: str, record: Any) -> None:
        if collection not in self._managed or not isinstance(record, dict):
            return
        workflow, field = self._managed[collection]
        if record.get(field) is not None:
            raise NeaiError(
                "WORKFLOW_FIELD_MANAGED",
                f"'{collection}.{field}' is the step of workflow '{workflow}'; only the platform sets it",
                hint=f"leave '{field}' out: new records start at the first step, and "
                     f"ctx.workflow.advance('{workflow}', id) / ctx.workflow.exit(...) move them",
                details={"collection": collection, "field": field, "workflow": workflow},
            )

    def _check_writable(self, op: str, collection: str, call: str | None = None) -> None:
        if not self._readonly:
            return
        details: dict[str, Any] = {"handler": self._handler, "op": op, "collection": collection}
        where = self._caller_line()
        if where:
            details["file"], details["line"] = where
        err = NeaiError(
            "READ_ONLY_VIOLATION",
            f"{self._handler} is a read handler; {call or f'db.{op}({collection!r})'} is not allowed"
            + (f" ({where[0]} line {where[1]})" if where else ""),
            hint=f'declare {self._handler} as "write" in app.json, or move the write into a write handler',
            details=details,
        )
        self.violations.append(err)
        raise err

    def _caller_line(self) -> tuple[str, int] | None:
        frames = [f for f in traceback.extract_stack() if f.filename in self._sources]
        if not frames:
            return None
        return frames[-1].filename.rsplit("/", 1)[-1], frames[-1].lineno

    @staticmethod
    def _where(collection: str, fields: dict[str, Field], where: Any) -> dict[str, Any]:
        if where is None:
            return {}
        if not isinstance(where, dict):
            raise NeaiError("INVALID_QUERY", f"where must be an object, got {json_type(where)}",
                            details={"collection": collection})
        for key, value in where.items():
            if key == "id":
                if not _is_id(value):
                    raise NeaiError("INVALID_QUERY", f"where.id must be an integer, got {json_type(value)}",
                                    details={"collection": collection})
                continue
            if key not in fields:
                raise NeaiError(
                    "INVALID_QUERY", f"'{collection}' has no field {key!r} to filter on",
                    hint=f"fields: {', '.join(fields)}", details={"collection": collection, "field": key},
                )
            if fields[key].type in ("list", "object"):
                raise NeaiError(
                    "INVALID_QUERY", f"can't filter on {fields[key].type} field '{key}'",
                    hint="filters compare scalar fields for equality; find() and then filter in Python",
                    details={"collection": collection, "field": key},
                )
            if value is not None and (problem := type_problem(fields[key], value)):
                raise NeaiError("INVALID_QUERY", f"where.{key}: {problem}",
                                details={"collection": collection, "field": key})
        return where


class Context:
    """What a handler gets. caller/user come from a verified login token, never from the request."""

    def __init__(self, user: Any, db: DataHandle, handler: str, app: str, version: int):
        self.caller: str | None = user.username if user else None
        self.user: dict[str, Any] | None = user.to_dict() if user else None
        self.db = db
        self.workflow: Any = None  # set by the runtime: the WorkflowHandle for this call
        self.handler = handler
        self.app = app
        self.version = version
        self.logs: list[dict[str, Any]] = []

    def has_role(self, role: str) -> bool:
        return self.user is not None and role in self.user["roles"]

    def log(self, message: str, **fields: Any) -> None:
        self.logs.append({"at": now(), "message": str(message), "fields": fields or None})


def _shape(fields: dict[str, Field], record: dict[str, Any]) -> dict[str, Any]:
    # jsonb stores keys in its own order; give records back in the order app.json declares them.
    out = {"id": record["id"]}
    out.update((f, record[f]) for f in fields if f in record)
    out.update((k, v) for k, v in record.items() if k not in out)
    return out


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
