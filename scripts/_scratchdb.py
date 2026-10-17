"""A throwaway database for the scripts, next to the real one, so they never touch your apps."""

from __future__ import annotations

import os
import re

import psycopg

from neai.database import DEFAULT_URL, START_HINT


def scratch_database(name: str) -> str:
    if not re.fullmatch(r"neai_[a-z_]+", name):
        raise ValueError("scratch databases are named neai_<something>")
    base = (os.environ.get("NEAI_DATABASE_URL") or DEFAULT_URL).split("?")[0]
    server = base.rsplit("/", 1)[0]
    try:
        with psycopg.connect(server + "/postgres", autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {name}")
    except psycopg.OperationalError as e:
        raise SystemExit(f"can't reach Postgres ({str(e).strip().splitlines()[0]}); {START_HINT}") from None
    return f"{server}/{name}"
