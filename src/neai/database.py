from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Json, Jsonb

from .errors import NeaiError

DEFAULT_URL = "postgresql://neai:neai@127.0.0.1:5433/neai"
CONNECT_TIMEOUT_SECONDS = 5
START_HINT = ("start Postgres with `docker compose up -d` from the neai repo, "
              "or point NEAI_DATABASE_URL at a running Postgres")

# Every table is shared by all apps and every row carries its app's name. Handler code never
# writes SQL, so the platform's queries (always filtered by app) are the isolation boundary.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS neai_apps (
    name           text PRIMARY KEY,
    id             uuid NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    active_version integer,
    source_dir     text,
    last_record_id bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS neai_versions (
    app        text NOT NULL REFERENCES neai_apps(name) ON DELETE CASCADE,
    version    integer NOT NULL,
    hash       text NOT NULL,
    manifest   json NOT NULL,              -- json, not jsonb: keeps the order it was written in
    files      jsonb NOT NULL,            -- {"app.json": "...", "handlers.py": "...", ...}
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app, version)
);

CREATE TABLE IF NOT EXISTS neai_deploys (
    id      bigserial PRIMARY KEY,
    app     text NOT NULL,
    at      timestamptz NOT NULL DEFAULT now(),
    hash    text,
    status  text NOT NULL,                -- activated | unchanged | rejected
    version integer,
    source  text,
    error   json
);
CREATE INDEX IF NOT EXISTS neai_deploys_by_app ON neai_deploys (app, id);

CREATE TABLE IF NOT EXISTS neai_records (
    app        text NOT NULL,
    id         bigint NOT NULL,
    collection text NOT NULL,
    data       jsonb NOT NULL,
    PRIMARY KEY (app, id)
);
CREATE INDEX IF NOT EXISTS neai_records_by_collection ON neai_records (app, collection, id);
CREATE INDEX IF NOT EXISTS neai_records_data ON neai_records USING gin (data jsonb_path_ops);

CREATE TABLE IF NOT EXISTS neai_logs (
    id      bigserial PRIMARY KEY,
    app     text NOT NULL,
    at      timestamptz NOT NULL DEFAULT now(),
    level   text NOT NULL,                -- info | warn | error
    event   text NOT NULL,                -- call | log | deploy | server
    handler text,
    caller  text,
    version integer,
    message text NOT NULL,
    data    json
);
CREATE INDEX IF NOT EXISTS neai_logs_by_app ON neai_logs (app, id);

CREATE TABLE IF NOT EXISTS neai_users (
    app           text NOT NULL,
    username      text NOT NULL,
    password_hash text NOT NULL,          -- scrypt; the password itself is never stored
    roles         jsonb NOT NULL DEFAULT '[]',
    created_at    timestamptz NOT NULL DEFAULT now(),
    failed_logins integer NOT NULL DEFAULT 0,
    locked_until  timestamptz,
    PRIMARY KEY (app, username)
);

CREATE TABLE IF NOT EXISTS neai_sessions (
    token_hash text PRIMARY KEY,          -- sha256 of the login token; the token itself is never stored
    app        text NOT NULL,
    username   text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (app, username) REFERENCES neai_users (app, username) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS neai_sessions_by_user ON neai_sessions (app, username);

CREATE TABLE IF NOT EXISTS neai_workflow_history (
    id        bigserial PRIMARY KEY,
    app       text NOT NULL,
    workflow  text NOT NULL,
    record_id bigint NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    action    text NOT NULL,              -- start | advance | exit
    from_step text,
    to_step   text NOT NULL,
    skipped   json NOT NULL DEFAULT '[]', -- steps passed over by a skip rule on the way
    by_user   text,
    note      text,
    version   integer
);
CREATE INDEX IF NOT EXISTS neai_workflow_history_by_record ON neai_workflow_history (app, workflow, record_id, id);

CREATE TABLE IF NOT EXISTS neai_instances (
    id           uuid PRIMARY KEY,
    app          text NOT NULL,
    url          text NOT NULL,
    pid          integer,
    host         text,
    started_at   timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);
"""

TABLES = ("neai_apps", "neai_versions", "neai_deploys", "neai_records", "neai_logs", "neai_instances",
          "neai_users", "neai_sessions", "neai_workflow_history")

_schema_ready: set[str] = set()


def database_url(url: str | None = None) -> str:
    return url or os.environ.get("NEAI_DATABASE_URL") or DEFAULT_URL


def safe_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=_dumps)


def json_text(value: Any) -> Json:
    # For json (not jsonb) columns: stored as written, so key order survives.
    return Json(value, dumps=_dumps)


def _dumps(value: Any) -> str:
    # Postgres can't store the NUL character in text or jsonb; records reject it during
    # validation, and anything else (log previews, error details) is made storable here.
    return json.dumps(value, separators=(",", ":"), default=repr).replace("\\u0000", "\\ufffd")


def unavailable(url: str, exc: Exception) -> NeaiError:
    reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return NeaiError("DATABASE_UNAVAILABLE", f"can't reach the database at {safe_url(url)}: {reason}",
                     hint=START_HINT, details={"database": safe_url(url)})


class Database:
    """One connection to Postgres, reconnected on demand, with nestable transactions."""

    def __init__(self, url: str | None = None, *, statement_timeout: float | None = None):
        self.url = database_url(url)
        self.statement_timeout = statement_timeout
        self._conn: psycopg.Connection | None = None
        self._depth = 0

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._depth = 0
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> psycopg.Connection:
        try:
            conn = psycopg.connect(self.url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS)
            if self.statement_timeout:
                conn.execute(f"SET statement_timeout = {int(self.statement_timeout * 1000)}")
            if self.url not in _schema_ready:
                ensure_schema(conn)
                _schema_ready.add(self.url)
        except psycopg.OperationalError as e:
            raise unavailable(self.url, e) from None
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None
        self._depth = 0

    def execute(self, sql: str, params: Any = None) -> psycopg.Cursor:
        # A connection that died while idle (say, Postgres restarted) is only noticed on first use.
        # Outside a transaction nothing has run yet, so one retry on a fresh connection is safe.
        may_retry = self._depth == 0 and self._conn is not None
        try:
            return self.conn.execute(sql, params)
        except psycopg.errors.QueryCanceled:
            raise NeaiError("QUERY_TIMEOUT", "a database query took too long and was cancelled",
                            hint="the whole call was rolled back") from None
        except psycopg.OperationalError as e:
            self.close()
            if may_retry:
                return self.execute(sql, params)
            raise unavailable(self.url, e) from None
        except psycopg.Error as e:
            raise NeaiError("DATABASE_ERROR", f"{type(e).__name__}: {str(e).strip()}") from None

    @contextmanager
    def transaction(self, *, lock: str | None = None, readonly: bool = False,
                    rollback: bool = False) -> Iterator[None]:
        """BEGIN ... COMMIT, or a savepoint when already inside a transaction.

        lock:      take this app's write lock first, so writes to one app happen one at a time
                   across every server that shares the database (released at COMMIT/ROLLBACK).
        readonly:  a read-only snapshot; Postgres itself refuses writes inside it.
        rollback:  always roll back, even on success.
        """
        outer = self._depth == 0
        savepoint = f"neai_sp_{self._depth}"
        if outer:
            self.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" if readonly else "BEGIN")
        else:
            self.execute(f"SAVEPOINT {savepoint}")
        self._depth += 1
        try:
            if lock is not None:
                self.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"neai-app:{lock}",))
            yield
        except BaseException:
            self._depth = max(0, self._depth - 1)
            self._undo(outer, savepoint)
            raise
        self._depth = max(0, self._depth - 1)
        if rollback:
            self._undo(outer, savepoint)
        else:
            self.execute("COMMIT" if outer else f"RELEASE SAVEPOINT {savepoint}")

    def _undo(self, outer: bool, savepoint: str) -> None:
        if self._conn is None or self._conn.closed:
            return
        try:
            if outer:
                self._conn.execute("ROLLBACK")
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except psycopg.Error:
            # e.g. a timeout interrupted a query mid-flight; start over with a fresh connection.
            self.close()


def ensure_schema(conn: psycopg.Connection) -> None:
    # Several processes may start at once; the lock stops them racing on CREATE TABLE.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('neai-schema', 0))")
        conn.execute(SCHEMA_SQL)
