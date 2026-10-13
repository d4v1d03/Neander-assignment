from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from .errors import NeaiError
from .schema import Field, Manifest, json_type, type_problem, validate_record
from .store import Store, now

MAX_FIND_LIMIT = 10_000


class DataHandle:
    def __init__(self, store: Store, manifest: Manifest, *, readonly: bool, handler: str,
                 source: Path | None = None):
        self._source = source
        self._store = store
        self._manifest = manifest
        self._readonly = readonly
        self._handler = handler
        self.violations: list[NeaiError] = []

    @property
    def readonly(self) -> bool:
        return self._readonly


    def get(self, collection: str, id: int) -> dict[str, Any] | None:
        self._fields(collection)
        if not _is_id(id):
            return None
        return self._store.get_record(collection, id)

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        fields = self._fields(collection)
        conditions = self._where(collection, fields, where)
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= MAX_FIND_LIMIT):
            raise NeaiError(
                "INVALID_QUERY", f"limit must be an integer between 1 and {MAX_FIND_LIMIT}, got {limit!r}",
                details={"collection": collection},
            )
        out = []
        for _, record in self._store.iter_records(collection):
            if all(record.get(k) == v for k, v in conditions.items()):
                out.append(record)
                if limit is not None and len(out) >= limit:
                    break
        return out

    def count(self, collection: str, where: dict[str, Any] | None = None) -> int:
        return len(self.find(collection, where))


    def insert(self, collection: str, record: dict[str, Any]) -> dict[str, Any]:
        self._check_writable("insert", collection)
        fields = self._fields(collection)
        data = validate_record(collection, fields, record)
        new_id = self._store.insert_record(collection, data)
        return {"id": new_id, **data}

    def update(self, collection: str, id: int, changes: dict[str, Any]) -> dict[str, Any]:
        self._check_writable("update", collection)
        fields = self._fields(collection)
        if not isinstance(changes, dict):
            raise NeaiError(
                "VALIDATION_ERROR", f"update changes must be an object, got {json_type(changes)}",
                details={"collection": collection},
            )
        existing = self._store.get_record(collection, id) if _is_id(id) else None
        if existing is None:
            raise NeaiError(
                "RECORD_NOT_FOUND", f"no record with id {id!r} in '{collection}'",
                hint="check the id with db.get() before updating",
                details={"collection": collection, "id": id},
            )
        current = {k: v for k, v in existing.items() if k != "id"}
        data = validate_record(collection, fields, {**current, **changes})
        self._store.replace_record(collection, id, data)
        return {"id": id, **data}

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

    def _check_writable(self, op: str, collection: str) -> None:
        if not self._readonly:
            return
        details: dict[str, Any] = {"handler": self._handler, "op": op, "collection": collection}
        line = self._caller_line()
        if line:
            details["line"] = line
        err = NeaiError(
            "READ_ONLY_VIOLATION",
            f"{self._handler} is a read handler; db.{op}({collection!r}) is not allowed"
            + (f" (handlers.py line {line})" if line else ""),
            hint=f'declare {self._handler} as "write" in app.json, or move the write into a write handler',
            details=details,
        )
        self.violations.append(err)
        raise err

    def _caller_line(self) -> int | None:
        if self._source is None:
            return None
        frames = [f for f in traceback.extract_stack() if Path(f.filename) == self._source]
        return frames[-1].lineno if frames else None

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
    def __init__(self, caller: str, db: DataHandle, handler: str, app: str, version: int):
        self.caller = caller
        self.db = db
        self.handler = handler
        self.app = app
        self.version = version
        self.logs: list[dict[str, Any]] = []

    def log(self, message: str, **fields: Any) -> None:
        self.logs.append({"at": now(), "message": str(message), "fields": fields or None})


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
