from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Json, Jsonb

from .errors import NeaiError

DEFAULT_URL = "postgresql://neai:neai@127.0.0.1:5433/neai"
CONNECT_TIMEOUT_SECONDS = 5
# Connections each Database (each server) may have open at once, and how long a request waits
# for one when they are all in use.
DEFAULT_POOL_SIZE = 10
POOL_WAIT_SECONDS = 10
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


class Rows:
    """A statement's result, read in full while the connection was in hand.

    The connection goes back to the pool as soon as the statement is done (another thread may
    be using it a moment later), so nothing here reads from it afterwards.
    """

    def __init__(self, cursor: psycopg.Cursor):
        self.rowcount = cursor.rowcount
        self._rows: list[Any] = cursor.fetchall() if cursor.description else []
        self._next = 0

    def fetchone(self) -> Any:
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self) -> list[Any]:
        rows, self._next = self._rows[self._next:], len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class Database:
    """A small pool of connections to Postgres, safe to share between threads.

    A statement borrows a connection and gives it back when done. A transaction keeps the one
    it borrowed until COMMIT or ROLLBACK: every statement in it has to go over the same
    connection, and other threads must never see it in the meantime. Transactions nest (as
    savepoints) within a thread.
    """

    def __init__(self, url: str | None = None, *, statement_timeout: float | None = None,
                 idle_timeout: float | None = None, pool_size: int | None = None):
        self.url = database_url(url)
        self.statement_timeout = statement_timeout
        self.idle_timeout = idle_timeout
        self.pool_size = max(1, pool_size or int(os.environ.get("NEAI_DB_POOL_SIZE", DEFAULT_POOL_SIZE)))
        self._idle: list[psycopg.Connection] = []
        self._idle_lock = threading.Lock()
        # One slot per connection that may be open at once; a thread waits here when all are in use.
        self._slots = threading.BoundedSemaphore(self.pool_size)
        self._local = threading.local()

    @property
    def _held(self) -> psycopg.Connection | None:
        """The connection this thread's open transaction is using, if any."""
        return getattr(self._local, "conn", None)

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    def _connect(self) -> psycopg.Connection:
        try:
            conn = psycopg.connect(self.url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS)
            if self.statement_timeout:
                conn.execute(f"SET statement_timeout = {int(self.statement_timeout * 1000)}")
            if self.idle_timeout:
                # Postgres ends a transaction that sits idle this long (say, a handler stuck in
                # time.sleep, which a worker thread's time limit can't cut short), and with it
                # the app's write lock, so other writers aren't held up.
                conn.execute(f"SET idle_in_transaction_session_timeout = {int(self.idle_timeout * 1000)}")
            if self.url not in _schema_ready:
                ensure_schema(conn)
                _schema_ready.add(self.url)
        except psycopg.OperationalError as e:
            raise unavailable(self.url, e) from None
        return conn

    def _borrow(self) -> tuple[psycopg.Connection, bool]:
        """A connection for this thread, and whether it was reused (it may have died while idle)."""
        if not self._slots.acquire(timeout=POOL_WAIT_SECONDS):
            raise NeaiError("DATABASE_BUSY", f"all {self.pool_size} database connections stayed busy for "
                                             f"{POOL_WAIT_SECONDS:g}s",
                            hint="the server is overloaded: retry shortly, run more copies behind `neai proxy`, "
                                 "or raise NEAI_DB_POOL_SIZE")
        try:
            with self._idle_lock:
                while self._idle:
                    conn = self._idle.pop()
                    if not conn.closed:
                        return conn, True
            return self._connect(), False
        except BaseException:
            self._slots.release()
            raise

    def _give_back(self, conn: psycopg.Connection) -> None:
        # Only a connection that is idle and outside any transaction may be reused; anything else
        # (a query cut off by a timeout, a broken link) is closed instead.
        if not conn.closed and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            with self._idle_lock:
                self._idle.append(conn)
        else:
            _close_quietly(conn)
        self._slots.release()

    def close(self) -> None:
        """Close the idle connections. The pool stays usable: it reconnects on demand."""
        with self._idle_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            _close_quietly(conn)

    def execute(self, sql: str, params: Any = None) -> Rows:
        held = self._held
        if held is not None:
            # Inside a transaction: a lost connection means the transaction is lost, so no retry.
            try:
                return self._run(held, sql, params)
            except psycopg.OperationalError as e:
                _close_quietly(held)
                raise unavailable(self.url, e) from None
        conn, reused = self._borrow()
        try:
            try:
                return self._run(conn, sql, params)
            except psycopg.OperationalError as e:
                # A connection that died while idle (say, Postgres restarted) is only noticed on
                # first use. Outside a transaction nothing has run yet, so one retry on a fresh
                # connection is safe.
                _close_quietly(conn)
                if not reused:
                    raise unavailable(self.url, e) from None
                conn = self._connect()
                try:
                    return self._run(conn, sql, params)
                except psycopg.OperationalError as e2:
                    _close_quietly(conn)
                    raise unavailable(self.url, e2) from None
        finally:
            self._give_back(conn)

    def _run(self, conn: psycopg.Connection, sql: str, params: Any) -> Rows:
        try:
            return Rows(conn.execute(sql, params))
        except psycopg.errors.QueryCanceled:
            raise NeaiError("QUERY_TIMEOUT", "a database query took too long and was cancelled",
                            hint="the whole call was rolled back") from None
        except psycopg.OperationalError:
            raise
        except psycopg.Error as e:
            raise NeaiError("DATABASE_ERROR", f"{type(e).__name__}: {str(e).strip()}") from None

    @contextmanager
    def transaction(self, *, lock: str | None = None, readonly: bool = False,
                    rollback: bool = False) -> Iterator[None]:
        """BEGIN ... COMMIT, or a savepoint when this thread is already inside a transaction.

        lock:      take this app's write lock first, so writes to one app happen one at a time
                   across every thread and every server that shares the database (released at
                   COMMIT/ROLLBACK).
        readonly:  a read-only snapshot; Postgres itself refuses writes inside it.
        rollback:  always roll back, even on success.
        """
        depth = self._depth
        outer = depth == 0
        savepoint = f"neai_sp_{depth}"
        if outer:
            self._begin("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" if readonly else "BEGIN")
        else:
            self.execute(f"SAVEPOINT {savepoint}")
        self._local.depth = depth + 1
        try:
            try:
                if lock is not None:
                    self.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"neai-app:{lock}",))
                yield
            except BaseException:
                self._local.depth = depth
                self._undo(outer, savepoint)
                raise
            self._local.depth = depth
            if rollback:
                self._undo(outer, savepoint)
            else:
                self.execute("COMMIT" if outer else f"RELEASE SAVEPOINT {savepoint}")
        finally:
            if outer:
                self._local.depth = 0
                conn, self._local.conn = self._local.conn, None
                if conn is not None:
                    self._give_back(conn)

    def _begin(self, sql: str) -> None:
        conn, reused = self._borrow()
        try:
            try:
                self._run(conn, sql, None)
            except psycopg.OperationalError as e:
                _close_quietly(conn)
                if not reused:
                    raise unavailable(self.url, e) from None
                conn = self._connect()
                try:
                    self._run(conn, sql, None)
                except psycopg.OperationalError as e2:
                    _close_quietly(conn)
                    raise unavailable(self.url, e2) from None
        except BaseException:
            self._give_back(conn)
            raise
        self._local.conn = conn

    def _undo(self, outer: bool, savepoint: str) -> None:
        conn = self._held
        if conn is None or conn.closed:
            return
        try:
            if outer:
                conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except psycopg.Error:
            # e.g. a timeout interrupted a query mid-flight; this connection is not reused.
            _close_quietly(conn)


def _close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except psycopg.Error:
        pass


def ensure_schema(conn: psycopg.Connection) -> None:
    # Several processes may start at once; the lock stops them racing on CREATE TABLE.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('neai-schema', 0))")
        conn.execute(SCHEMA_SQL)
