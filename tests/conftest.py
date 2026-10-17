import json
import os
import shutil
from pathlib import Path

import psycopg
import pytest

from neai.database import DEFAULT_URL, TABLES, ensure_schema
from neai.deploy import deploy
from neai.runtime import Runtime

REPO = Path(__file__).resolve().parents[1]
APPS = REPO / "apps"


def _test_url():
    # A separate database next to the real one, so running the tests never touches your apps.
    if os.environ.get("NEAI_TEST_DATABASE_URL"):
        return os.environ["NEAI_TEST_DATABASE_URL"]
    base = (os.environ.get("NEAI_DATABASE_URL") or DEFAULT_URL).split("?")[0]
    return base.rsplit("/", 1)[0] + "/neai_test"


@pytest.fixture(scope="session")
def _test_db_url():
    url = _test_url()
    server, name = url.rsplit("/", 1)
    try:
        with psycopg.connect(server + "/postgres", autocommit=True, connect_timeout=3) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                conn.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(url, autocommit=True) as conn:
            ensure_schema(conn)
    except psycopg.OperationalError as e:
        pytest.exit(f"the tests need Postgres: start it with `docker compose up -d` ({e})", returncode=2)
    return url


@pytest.fixture
def database(_test_db_url, monkeypatch):
    with psycopg.connect(_test_db_url, autocommit=True) as conn:
        conn.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
    # Subprocesses (neai run / serve / call) inherit this, so they use the test database too.
    monkeypatch.setenv("NEAI_DATABASE_URL", _test_db_url)
    return _test_db_url


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def app_copy(tmp_path):
    def make(name, dest=None):
        target = tmp_path / (dest or f"src-{name}")
        shutil.copytree(APPS / name, target)
        return target

    return make


def write_app(directory, manifest, code):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "app.json").write_text(json.dumps(manifest) if not isinstance(manifest, str) else manifest)
    (directory / "handlers.py").write_text(code)
    return directory


@pytest.fixture
def deployed(database):
    runtimes = []

    def make(source, timeout=None):
        source = APPS / source if isinstance(source, str) else source
        result = deploy(database, source)
        assert result["ok"], result
        rt = Runtime(database, result["app"], timeout=timeout)
        runtimes.append(rt)
        return rt

    yield make
    for rt in runtimes:
        rt.close()


def ok(response):
    assert response["ok"], response
    return response["result"]


def err(response):
    assert not response["ok"], response
    return response["error"]
