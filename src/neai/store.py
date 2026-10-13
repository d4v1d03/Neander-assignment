from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import ensure_private_dir

LOG_RETENTION = 1000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_by_collection ON records(collection, id);

CREATE TABLE IF NOT EXISTS versions (
    version    INTEGER PRIMARY KEY,
    hash       TEXT NOT NULL,
    manifest   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploys (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    hash    TEXT,
    status  TEXT NOT NULL,      -- activated | unchanged | rejected
    version INTEGER,
    source  TEXT,
    error   TEXT                -- JSON, for rejected deploys
);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    level   TEXT NOT NULL,      -- info | error
    event   TEXT NOT NULL,      -- call | log | deploy | server
    handler TEXT,
    caller  TEXT,
    version INTEGER,
    message TEXT NOT NULL,
    data    TEXT                -- JSON
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, app_dir: Path):
        self.dir = ensure_private_dir(app_dir)
        self.path = self.dir / "app.db"
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.path.chmod(0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        if self.get_meta("app_id") is None:
            self.set_meta("app_id", str(uuid.uuid4()))
            self.set_meta("created_at", now())

    def close(self) -> None:
        self.conn.close()


    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        finally:
            self.conn.execute("ROLLBACK")


    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def active_version(self) -> int | None:
        v = self.get_meta("active_version")
        return int(v) if v is not None else None


    def get_record(self, collection: str, record_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, data FROM records WHERE id = ? AND collection = ?", (record_id, collection)
        ).fetchone()
        return _record(row) if row else None

    def iter_records(self, collection: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
        if collection is None:
            rows = self.conn.execute("SELECT collection, id, data FROM records ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT collection, id, data FROM records WHERE collection = ? ORDER BY id", (collection,)
            )
        for coll, rid, data in rows:
            yield coll, _record((rid, data))

    def insert_record(self, collection: str, data: dict[str, Any]) -> int:
        cur = self.conn.execute("INSERT INTO records(collection, data) VALUES (?, ?)", (collection, _dumps(data)))
        return int(cur.lastrowid)

    def replace_record(self, collection: str, record_id: int, data: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE records SET data = ? WHERE id = ? AND collection = ?", (_dumps(data), record_id, collection)
        )

    def delete_record(self, collection: str, record_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM records WHERE id = ? AND collection = ?", (record_id, collection))
        return cur.rowcount > 0

    def count_by_collection(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT collection, COUNT(*) FROM records GROUP BY collection")
        return {c: n for c, n in rows}


    def next_version(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM versions").fetchone()
        return int(row[0])

    def get_version(self, version: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT version, hash, manifest, created_at FROM versions WHERE version = ?", (version,)
        ).fetchone()
        if not row:
            return None
        return {"version": row[0], "hash": row[1], "manifest": json.loads(row[2]), "activated_at": row[3]}

    def add_version(self, version: int, digest: str, manifest: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO versions(version, hash, manifest, created_at) VALUES (?, ?, ?, ?)",
            (version, digest, _dumps(manifest), now()),
        )

    def add_deploy(
        self,
        status: str,
        *,
        digest: str | None,
        version: int | None = None,
        source: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO deploys(at, hash, status, version, source, error) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), digest, status, version, source, _dumps(error) if error else None),
        )

    def deploys(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, at, hash, status, version, source, error FROM deploys ORDER BY id DESC LIMIT ?", (limit,)
        )
        out = []
        for did, at, digest, status, version, source, error in rows:
            d = {"id": did, "at": at, "status": status, "hash": digest, "version": version, "source": source}
            if error:
                d["error"] = json.loads(error)
            out.append({k: v for k, v in d.items() if v is not None})
        return out


    def add_logs(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        with self.transaction(write=True):
            for e in entries:
                self.conn.execute(
                    "INSERT INTO logs(at, level, event, handler, caller, version, message, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        e.get("at") or now(),
                        e.get("level", "info"),
                        e["event"],
                        e.get("handler"),
                        e.get("caller"),
                        e.get("version"),
                        e["message"],
                        _dumps(e["data"]) if e.get("data") is not None else None,
                    ),
                )
            self.conn.execute(
                "DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (LOG_RETENTION,)
            )

    def logs(self, *, limit: int = 20, errors_only: bool = False, handler: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, at, level, event, handler, caller, version, message, data FROM logs"
        where, params = [], []
        if errors_only:
            where.append("level = 'error'")
        if handler:
            where.append("handler = ?")
            params.append(handler)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        out = []
        for lid, at, level, event, handler_, caller, version, message, data in self.conn.execute(sql, params):
            entry = {
                "id": lid, "at": at, "level": level, "event": event, "handler": handler_,
                "caller": caller, "version": version, "message": message,
                "data": json.loads(data) if data else None,
            }
            out.append({k: v for k, v in entry.items() if v is not None})
        return out


def _record(row: tuple[int, str]) -> dict[str, Any]:
    rid, data = row
    return {"id": rid, **json.loads(data)}


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=repr)
