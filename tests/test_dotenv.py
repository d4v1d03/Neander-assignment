import os

import pytest

from neai import dotenv


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv, "PROJECT_ROOT", tmp_path / "no-project")
    for key in ("NEAI_TEST_A", "NEAI_TEST_B", "NEAI_TEST_C", "NEAI_TEST_D"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_parse_handles_comments_export_and_quotes():
    text = """
# a comment
NEAI_TEST_A=plain
export NEAI_TEST_B = "quoted value"
NEAI_TEST_C='single'
NEAI_TEST_D=value # trailing comment
not a line
1BAD=x
"""
    assert dotenv.parse(text) == {"NEAI_TEST_A": "plain", "NEAI_TEST_B": "quoted value",
                                  "NEAI_TEST_C": "single", "NEAI_TEST_D": "value"}


def test_load_sets_missing_variables_and_reports_names_only(isolated):
    env = isolated / ".env"
    env.write_text("NEAI_TEST_A=secret-value\nNEAI_TEST_B=\n")
    env.chmod(0o600)
    report = dotenv.load_dotenv()
    assert os.environ["NEAI_TEST_A"] == "secret-value"
    assert "NEAI_TEST_B" not in os.environ
    assert report == [{"file": str(env.resolve()), "variables": ["NEAI_TEST_A"]}]
    assert "secret-value" not in str(report)


def test_shell_variables_win_over_the_file(isolated, monkeypatch):
    (isolated / ".env").write_text("NEAI_TEST_A=from-file\n")
    monkeypatch.setenv("NEAI_TEST_A", "from-shell")
    dotenv.load_dotenv()
    assert os.environ["NEAI_TEST_A"] == "from-shell"


def test_warns_when_the_file_is_readable_by_others(isolated):
    env = isolated / ".env"
    env.write_text("NEAI_TEST_A=x\n")
    env.chmod(0o644)
    [entry] = dotenv.load_dotenv()
    assert "chmod 600" in entry["warning"]


def test_no_env_file_is_fine(isolated):
    assert dotenv.load_dotenv() == []
