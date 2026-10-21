from __future__ import annotations

import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .database import Database, json_text, jsonb
from .errors import NeaiError

LOG_RETENTION = 1000
INSTANCE_STALE_SECONDS = 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds") if value else None


class Store:
    """Everything the platform keeps about one app, in the shared Postgres database."""

    def __init__(self, db: Database, app: str):
        self.db = db
        self.app = app

    def close(self) -> None:
        self.db.close()


    def exists(self) -> bool:
        return self.db.execute("SELECT 1 FROM neai_apps WHERE name = %s", (self.app,)).fetchone() is not None

    def ensure(self) -> None:
        self.db.execute("INSERT INTO neai_apps (name, id) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                        (self.app, uuid.uuid4()))

    def info(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id, created_at, active_version, source_dir FROM neai_apps WHERE name = %s", (self.app,)
        ).fetchone()
        if not row:
            return None
        return {"id": str(row[0]), "created_at": iso(row[1]), "active_version": row[2], "source_dir": row[3]}

    def active_version(self) -> int | None:
        row = self.db.execute("SELECT active_version FROM neai_apps WHERE name = %s", (self.app,)).fetchone()
        return row[0] if row else None

    def activate(self, version: int, source: str | None) -> None:
        self.db.execute("UPDATE neai_apps SET active_version = %s, source_dir = COALESCE(%s, source_dir) "
                        "WHERE name = %s", (version, source, self.app))


    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[None]:
        with self.db.transaction(lock=self.app if write else None):
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        with self.db.transaction(readonly=True, rollback=True):
            yield


    def get_record(self, collection: str, record_id: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id, data FROM neai_records WHERE app = %s AND collection = %s AND id = %s",
                              (self.app, collection, record_id)).fetchone()
        return _record(row) if row else None

    def find_records(self, collection: str, where: dict[str, Any] | None = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        clauses, params = _where_sql(where)
        sql = "SELECT id, data FROM neai_records WHERE app = %s AND collection = %s" + clauses + " ORDER BY id"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        return [_record(row) for row in self.db.execute(sql, [self.app, collection, *params])]

    def count_records(self, collection: str, where: dict[str, Any] | None = None) -> int:
        clauses, params = _where_sql(where)
        sql = "SELECT count(*) FROM neai_records WHERE app = %s AND collection = %s" + clauses
        return int(self.db.execute(sql, [self.app, collection, *params]).fetchone()[0])

    def iter_records(self, collection: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
        if collection is None:
            rows = self.db.execute("SELECT collection, id, data FROM neai_records WHERE app = %s ORDER BY id",
                                   (self.app,))
        else:
            rows = self.db.execute("SELECT collection, id, data FROM neai_records WHERE app = %s AND collection = %s "
                                   "ORDER BY id", (self.app, collection))
        for coll, rid, data in rows:
            yield coll, _record((rid, data))

    def insert_record(self, collection: str, data: dict[str, Any]) -> int:
        # Ids are per app, shared by its collections, and start at 1 (like v1).
        row = self.db.execute("UPDATE neai_apps SET last_record_id = last_record_id + 1 WHERE name = %s "
                              "RETURNING last_record_id", (self.app,)).fetchone()
        new_id = int(row[0])
        self.db.execute("INSERT INTO neai_records (app, id, collection, data) VALUES (%s, %s, %s, %s)",
                        (self.app, new_id, collection, jsonb(data)))
        return new_id

    def replace_record(self, collection: str, record_id: int, data: dict[str, Any]) -> None:
        self.db.execute("UPDATE neai_records SET data = %s WHERE app = %s AND collection = %s AND id = %s",
                        (jsonb(data), self.app, collection, record_id))

    def delete_record(self, collection: str, record_id: int) -> bool:
        cur = self.db.execute("DELETE FROM neai_records WHERE app = %s AND collection = %s AND id = %s",
                              (self.app, collection, record_id))
        return cur.rowcount > 0

    def count_by_collection(self) -> dict[str, int]:
        rows = self.db.execute("SELECT collection, count(*) FROM neai_records WHERE app = %s GROUP BY collection",
                               (self.app,))
        return {c: int(n) for c, n in rows}

    def fill_default(self, collection: str, field: str, value: Any) -> int:
        cur = self.db.execute(
            "UPDATE neai_records SET data = data || jsonb_build_object(%s::text, %s::jsonb) "
            "WHERE app = %s AND collection = %s AND NOT (data ? %s)",
            (field, jsonb(value), self.app, collection, field))
        return cur.rowcount

    def duplicates(self, collection: str, fields: tuple[str, ...], limit: int = 5) -> list[dict[str, Any]]:
        values = ", ".join("data -> %s" for _ in fields)
        present = " AND ".join("data ? %s" for _ in fields)
        rows = self.db.execute(
            f"SELECT {values}, array_agg(id ORDER BY id) FROM neai_records "
            f"WHERE app = %s AND collection = %s AND {present} "
            f"GROUP BY {', '.join(str(i + 1) for i in range(len(fields)))} HAVING count(*) > 1 LIMIT %s",
            [*fields, self.app, collection, *fields, limit])
        return [{"values": dict(zip(fields, row[:-1])), "ids": list(row[-1])} for row in rows]


    def count_by_value(self, collection: str, field: str) -> dict[str, int]:
        rows = self.db.execute("SELECT data ->> %s, count(*) FROM neai_records WHERE app = %s AND collection = %s "
                               "GROUP BY 1", (field, self.app, collection))
        return {value: int(n) for value, n in rows}

    def records_outside(self, collection: str, field: str, allowed: list[str],
                        limit: int = 5) -> tuple[int, list[dict[str, Any]]]:
        """Records whose `field` holds a value not in `allowed` (e.g. a workflow step that no longer exists)."""
        where = "app = %s AND collection = %s AND data ? %s AND NOT (data ->> %s = ANY(%s))"
        params = (self.app, collection, field, field, allowed)
        total = int(self.db.execute(f"SELECT count(*) FROM neai_records WHERE {where}", params).fetchone()[0])
        rows = self.db.execute(f"SELECT id, data ->> %s FROM neai_records WHERE {where} ORDER BY id LIMIT %s",
                               (field, *params, limit))
        return total, [{"id": int(i), "step": v} for i, v in rows]

    def add_workflow_history(self, workflow: str, record_id: int, *, action: str, from_step: str | None,
                             to_step: str, skipped: list[str], by_user: str | None, note: str | None,
                             version: int | None) -> None:
        self.db.execute(
            "INSERT INTO neai_workflow_history (app, workflow, record_id, action, from_step, to_step, skipped, "
            "by_user, note, version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (self.app, workflow, record_id, action, from_step, to_step, json_text(skipped), by_user, note, version))

    def workflow_history(self, workflow: str, record_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT at, action, from_step, to_step, skipped, by_user, note FROM neai_workflow_history "
            "WHERE app = %s AND workflow = %s AND record_id = %s ORDER BY id", (self.app, workflow, record_id))
        out = []
        for at, action, came_from, to, skipped, by, note in rows:
            entry = {"at": iso(at), "action": action, "from": came_from, "to": to, "skipped": skipped,
                     "by": by, "note": note}
            out.append({k: v for k, v in entry.items() if v is not None})
        return out

    def next_version(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM neai_versions WHERE app = %s",
                              (self.app,)).fetchone()
        return int(row[0])

    def get_version(self, version: int, *, files: bool = False) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT version, hash, manifest, created_at" + (", files" if files else "") +
            " FROM neai_versions WHERE app = %s AND version = %s", (self.app, version)).fetchone()
        if not row:
            return None
        info = {"version": row[0], "hash": row[1], "manifest": row[2], "activated_at": iso(row[3])}
        if files:
            info["files"] = row[4]
        return info

    def versions(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT version, hash, created_at FROM neai_versions WHERE app = %s "
                               "ORDER BY version DESC", (self.app,))
        return [{"version": v, "hash": h, "created_at": iso(at)} for v, h, at in rows]

    def add_version(self, version: int, digest: str, manifest: dict[str, Any], files: dict[str, str]) -> None:
        self.db.execute("INSERT INTO neai_versions (app, version, hash, manifest, files) VALUES (%s, %s, %s, %s, %s)",
                        (self.app, version, digest, json_text(manifest), jsonb(files)))

    def add_deploy(self, status: str, *, digest: str | None, version: int | None = None,
                   source: str | None = None, error: dict[str, Any] | None = None) -> None:
        self.db.execute("INSERT INTO neai_deploys (app, hash, status, version, source, error) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (self.app, digest, status, version, source, json_text(error) if error else None))

    def deploys(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id, at, hash, status, version, source, error FROM neai_deploys "
                               "WHERE app = %s ORDER BY id DESC LIMIT %s", (self.app, limit))
        out = []
        for did, at, digest, status, version, source, error in rows:
            d = {"id": did, "at": iso(at), "status": status, "hash": digest, "version": version,
                 "source": source, "error": error}
            out.append({k: v for k, v in d.items() if v is not None})
        return out


    def add_logs(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        with self.db.transaction():
            for e in entries:
                self.db.execute(
                    "INSERT INTO neai_logs (app, at, level, event, handler, caller, version, message, data) "
                    "VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s, %s, %s)",
                    (self.app, e.get("at"), e.get("level", "info"), e["event"], _text(e.get("handler")),
                     _text(e.get("caller")), e.get("version"), _text(e["message"]),
                     json_text(e["data"]) if e.get("data") is not None else None))
            self.db.execute(
                "DELETE FROM neai_logs WHERE app = %s AND id <= ("
                "SELECT id FROM neai_logs WHERE app = %s ORDER BY id DESC OFFSET %s LIMIT 1)",
                (self.app, self.app, LOG_RETENTION))

    def logs(self, *, limit: int = 20, errors_only: bool = False, handler: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, at, level, event, handler, caller, version, message, data FROM neai_logs WHERE app = %s"
        params: list[Any] = [self.app]
        if errors_only:
            sql += " AND level = 'error'"
        if handler:
            sql += " AND handler = %s"
            params.append(handler)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        out = []
        for lid, at, level, event, handler_, caller, version, message, data in self.db.execute(sql, params):
            entry = {"id": lid, "at": iso(at), "level": level, "event": event, "handler": handler_,
                     "caller": caller, "version": version, "message": message, "data": data}
            out.append({k: v for k, v in entry.items() if v is not None})
        return out


    def add_user(self, username: str, password_hash: str, roles: list[str]) -> bool:
        # ON CONFLICT instead of a check first: two sign-ups for one name can't both win.
        row = self.db.execute("INSERT INTO neai_users (app, username, password_hash, roles) VALUES (%s, %s, %s, %s) "
                              "ON CONFLICT (app, username) DO NOTHING RETURNING username",
                              (self.app, username, password_hash, jsonb(roles))).fetchone()
        return row is not None

    def get_user(self, username: str, *, for_update: bool = False) -> dict[str, Any] | None:
        # for_update (inside a transaction): lock the row until COMMIT, so read-then-change steps
        # on one account (a login attempt, a role change) happen one at a time.
        row = self.db.execute(
            "SELECT username, password_hash, roles, created_at, failed_logins, locked_until, "
            "COALESCE(locked_until > now(), false) FROM neai_users WHERE app = %s AND username = %s"
            + (" FOR UPDATE" if for_update else ""),
            (self.app, username)).fetchone()
        if not row:
            return None
        name, pw_hash, roles, created, failed, locked_until, locked = row
        return {"username": name, "password_hash": pw_hash, "roles": roles, "created_at": iso(created),
                "failed_logins": failed, "locked_until": iso(locked_until), "locked": locked}

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT u.username, u.roles, u.created_at, COALESCE(u.locked_until > now(), false), "
            "(SELECT count(*) FROM neai_sessions s WHERE s.app = u.app AND s.username = u.username "
            " AND s.expires_at > now()) FROM neai_users u WHERE u.app = %s ORDER BY u.username", (self.app,))
        return [{"username": u, "roles": r, "created_at": iso(c), "locked": lk, "active_sessions": int(n)}
                for u, r, c, lk, n in rows]

    def set_roles(self, username: str, roles: list[str]) -> None:
        self.db.execute("UPDATE neai_users SET roles = %s WHERE app = %s AND username = %s",
                        (jsonb(roles), self.app, username))

    def set_password_hash(self, username: str, password_hash: str) -> None:
        self.db.execute("UPDATE neai_users SET password_hash = %s, failed_logins = 0, locked_until = NULL "
                        "WHERE app = %s AND username = %s", (password_hash, self.app, username))

    def delete_user(self, username: str) -> bool:
        # Their sessions go too (ON DELETE CASCADE); their records stay, like any other data.
        return self.db.execute("DELETE FROM neai_users WHERE app = %s AND username = %s",
                               (self.app, username)).rowcount > 0

    def record_login_failure(self, username: str, *, max_failures: int, lock_seconds: int) -> None:
        # Both SET expressions see the old row, so the counter and the lock change together.
        self.db.execute(
            "UPDATE neai_users SET "
            "locked_until = CASE WHEN failed_logins + 1 >= %s THEN now() + %s * interval '1 second' "
            "ELSE locked_until END, "
            "failed_logins = CASE WHEN failed_logins + 1 >= %s THEN 0 ELSE failed_logins + 1 END "
            "WHERE app = %s AND username = %s",
            (max_failures, lock_seconds, max_failures, self.app, username))

    def clear_login_failures(self, username: str) -> None:
        self.db.execute("UPDATE neai_users SET failed_logins = 0, locked_until = NULL "
                        "WHERE app = %s AND username = %s", (self.app, username))

    def add_session(self, token_hash: str, username: str, days: int) -> str:
        row = self.db.execute("INSERT INTO neai_sessions (token_hash, app, username, expires_at) "
                              "VALUES (%s, %s, %s, now() + %s * interval '1 day') RETURNING expires_at",
                              (token_hash, self.app, username, days)).fetchone()
        return iso(row[0])

    def session_user(self, token_hash: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT u.username, u.roles FROM neai_sessions s "
            "JOIN neai_users u ON u.app = s.app AND u.username = s.username "
            "WHERE s.token_hash = %s AND s.app = %s AND s.expires_at > now()", (token_hash, self.app)).fetchone()
        return {"username": row[0], "roles": row[1]} if row else None

    def delete_session(self, token_hash: str) -> bool:
        return self.db.execute("DELETE FROM neai_sessions WHERE token_hash = %s AND app = %s",
                               (token_hash, self.app)).rowcount > 0

    def delete_sessions(self, username: str) -> int:
        return self.db.execute("DELETE FROM neai_sessions WHERE app = %s AND username = %s",
                               (self.app, username)).rowcount


    def register_instance(self, instance_id: str, url: str) -> None:
        self.db.execute(
            "DELETE FROM neai_instances WHERE heartbeat_at < now() - interval '1 hour'")
        self.heartbeat(instance_id, url)

    def heartbeat(self, instance_id: str, url: str) -> None:
        # An upsert, so an instance whose row was cleaned up while the database was away comes back.
        self.db.execute(
            "INSERT INTO neai_instances (id, app, url, pid, host) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()",
            (instance_id, self.app, url, os.getpid(), socket.gethostname()))

    def remove_instance(self, instance_id: str) -> None:
        self.db.execute("DELETE FROM neai_instances WHERE id = %s", (instance_id,))

    def instances(self) -> list[dict[str, Any]]:
        return list_instances(self.db, self.app)


def list_instances(db: Database, app: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT id, app, url, pid, host, started_at, heartbeat_at FROM neai_instances "
           f"WHERE heartbeat_at > now() - interval '{INSTANCE_STALE_SECONDS} seconds'")
    params: list[Any] = []
    if app is not None:
        sql += " AND app = %s"
        params.append(app)
    rows = db.execute(sql + " ORDER BY app, started_at", params)
    return [{"id": str(i), "app": a, "url": u, "pid": p, "host": h, "started_at": iso(s), "heartbeat_at": iso(hb)}
            for i, a, u, p, h, s, hb in rows]


def list_apps(db: Database) -> list[str]:
    return [name for (name,) in db.execute("SELECT name FROM neai_apps ORDER BY name")]


def require_app(db: Database, name: str) -> Store:
    store = Store(db, name)
    if not store.exists():
        known = list_apps(db)
        raise NeaiError(
            "APP_NOT_FOUND", f"no app named {name!r} in the database",
            hint="deploy it first with `neai deploy <dir>`" + (f"; known apps: {', '.join(known)}" if known else ""),
            details={"known_apps": known},
        )
    return store


def _where_sql(where: dict[str, Any] | None) -> tuple[str, list[Any]]:
    clauses, params, contains = [], [], {}
    for key, value in (where or {}).items():
        if key == "id":
            clauses.append("id = %s")
            params.append(value)
        elif value is None:
            # Records never store null (optional fields are left out), so null matches "missing".
            clauses.append("NOT (data ? %s)")
            params.append(key)
        else:
            contains[key] = value
    if contains:
        # Containment uses the GIN index; on scalars it is plain equality (1 and 1.0 are equal).
        clauses.append("data @> %s")
        params.append(jsonb(contains))
    return "".join(" AND " + c for c in clauses), params


def _record(row: tuple[int, dict[str, Any]]) -> dict[str, Any]:
    rid, data = row
    return {"id": int(rid), **data}


def _text(value: Any) -> Any:
    return value.replace("\x00", "�") if isinstance(value, str) else value
