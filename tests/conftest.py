import json
import shutil
from pathlib import Path

import pytest

from neai.deploy import deploy
from neai.runtime import Runtime

REPO = Path(__file__).resolve().parents[1]
APPS = REPO / "apps"


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
def deployed(home):
    runtimes = []

    def make(source, timeout=None):
        source = APPS / source if isinstance(source, str) else source
        result = deploy(home, source)
        assert result["ok"], result
        rt = Runtime(home, result["app"], timeout=timeout)
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
