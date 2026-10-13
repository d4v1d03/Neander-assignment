import io
import json

import pytest

from neai.deploy import deploy
from neai.inspection import inspect_app
from neai.runtime import Runtime
from neai.server import run

from conftest import APPS, ok


def edit(path, old, new):
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


@pytest.fixture
def notes_with_data(home, app_copy):
    src = app_copy("notes")
    assert deploy(home, src)["version"] == 1
    rt = Runtime(home, "notes")
    ok(rt.call("create_note", "alice", {"title": "one"}))
    ok(rt.call("create_note", "alice", {"title": "two", "body": "b"}))
    yield src, rt
    rt.close()


def titles(rt, caller="alice"):
    return [n["title"] for n in ok(rt.call("list_notes", caller))]


def test_data_survives_restart(home, notes_with_data):
    _, rt = notes_with_data
    rt.close()
    restarted = Runtime(home, "notes")
    assert titles(restarted) == ["one", "two"]
    restarted.close()


def test_redeploying_identical_code_is_a_no_op(home, notes_with_data):
    src, _ = notes_with_data
    result = deploy(home, src)
    assert (result["status"], result["version"]) == ("unchanged", 1)


def test_same_schema_code_update_keeps_data_and_changes_behaviour(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "handlers.py",
         'return ctx.db.find("notes", where={"owner": ctx.caller})',
         'return list(reversed(ctx.db.find("notes", where={"owner": ctx.caller})))')
    result = deploy(home, src)
    assert (result["status"], result["version"], result["schema_changed"]) == ("activated", 2, False)
    assert titles(rt) == ["two", "one"]
    assert rt.call("list_notes", "alice")["version"] == 2


def test_compatible_schema_change_is_accepted(home, notes_with_data):
    src, rt = notes_with_data
    manifest = json.loads((src / "app.json").read_text())
    manifest["collections"]["notes"]["pinned"] = {"type": "boolean", "required": False}
    manifest["collections"]["tags"] = {"label": {"type": "string"}}
    (src / "app.json").write_text(json.dumps(manifest))
    result = deploy(home, src)
    assert result["status"] == "activated" and result["schema_changed"] is True
    assert result["schema_diff"]["added_collections"] == ["tags"]
    assert titles(rt) == ["one", "two"]


def assert_rejected_safely(home, src, rt, code):
    result = deploy(home, src)
    assert result["ok"] is False and result["status"] == "rejected"
    assert result["error"]["code"] == code
    assert result["active_version"] == 1
    assert titles(rt) == ["one", "two"]
    assert rt.call("create_note", "alice", {"title": "three"})["version"] == 1
    report = inspect_app(home, "notes")
    assert report["active_version"]["version"] == 1
    assert report["latest_rejected"]["error"]["code"] == code
    return result["error"]


def test_syntax_error_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "handlers.py", "def list_notes(ctx, args):", "def list_notes(ctx, args)")
    e = assert_rejected_safely(home, src, rt, "HANDLER_IMPORT_ERROR")
    assert e["details"]["exception"] == "SyntaxError" and e["details"]["line"] > 0


def test_exception_at_import_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "handlers.py", "from neai import AppError", "from neai import AppError\nimport not_a_real_module")
    e = assert_rejected_safely(home, src, rt, "HANDLER_IMPORT_ERROR")
    assert e["details"]["exception"] == "ModuleNotFoundError"


def test_missing_handler_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "handlers.py", "def get_note(ctx, args):", "def fetch_note(ctx, args):")
    e = assert_rejected_safely(home, src, rt, "HANDLER_MISSING")
    assert e["details"]["missing"] == ["get_note"]


def test_bad_handler_signature_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "handlers.py", "def get_note(ctx, args):", "def get_note(ctx):")
    assert_rejected_safely(home, src, rt, "HANDLER_SIGNATURE")


def test_invalid_manifest_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "app.json", '"title": {"type": "string"}', '"title": {"type": "text"}')
    e = assert_rejected_safely(home, src, rt, "MANIFEST_INVALID")
    assert e["details"]["errors"][0]["path"] == "collections.notes.title.type"


def test_broken_json_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    edit(src / "app.json", '"name": "notes",', '"name": "notes",,')
    e = assert_rejected_safely(home, src, rt, "MANIFEST_INVALID")
    assert e["details"]["line"] == 2


def test_incompatible_schema_change_is_rejected(home, notes_with_data):
    src, rt = notes_with_data
    manifest = json.loads((src / "app.json").read_text())
    manifest["collections"]["notes"]["priority"] = {"type": "integer"}
    (src / "app.json").write_text(json.dumps(manifest))
    e = assert_rejected_safely(home, src, rt, "SCHEMA_INCOMPATIBLE")
    assert e["details"]["failing_records"] == 2
    assert "priority" in e["details"]["schema_diff"]["changed_collections"]["notes"]["added_fields"]


def test_removing_a_collection_with_data_is_rejected(home, deployed, app_copy):
    fb = deployed(app_copy("feedback"))
    post = ok(fb.call("create_post", "alice", {"text": "hi"}))
    ok(fb.call("vote", "bob", {"post_id": post["id"]}))
    src = app_copy("feedback", dest="feedback-v2")
    manifest = json.loads((src / "app.json").read_text())
    del manifest["collections"]["votes"]
    (src / "app.json").write_text(json.dumps(manifest))
    result = deploy(home, src)
    assert result["error"]["code"] == "SCHEMA_INCOMPATIBLE"
    assert ok(fb.call("list_posts", "bob"))[0]["votes"] == 1


def test_schema_can_change_freely_while_there_is_no_data(home, app_copy):
    src = app_copy("notes")
    deploy(home, src)
    manifest = json.loads((src / "app.json").read_text())
    manifest["collections"]["notes"]["priority"] = {"type": "integer"}
    (src / "app.json").write_text(json.dumps(manifest))
    assert deploy(home, src)["status"] == "activated"


def test_later_successful_deploy_clears_latest_rejected(home, notes_with_data):
    src, _ = notes_with_data
    edit(src / "handlers.py", "def list_notes(ctx, args):", "def list_notes(ctx, args)")
    assert deploy(home, src)["status"] == "rejected"
    edit(src / "handlers.py", "def list_notes(ctx, args)", "def list_notes(ctx, args):")
    edit(src / "handlers.py", "title must not be blank", "a title must not be blank")
    assert deploy(home, src)["status"] == "activated"
    assert inspect_app(home, "notes")["latest_rejected"] is None


def test_validate_is_a_dry_run(home, notes_with_data):
    src, _ = notes_with_data
    edit(src / "handlers.py", "title must not be blank", "a title must not be blank")
    result = deploy(home, src, dry_run=True)
    assert result["status"] == "valid"
    assert inspect_app(home, "notes")["active_version"]["version"] == 1
    edit(src / "handlers.py", "def list_notes(ctx, args):", "def list_notes(ctx, args)")
    assert deploy(home, src, dry_run=True)["status"] == "rejected"
    assert inspect_app(home, "notes")["latest_rejected"] is None


def test_first_deploy_rejection_is_still_inspectable(home, tmp_path):
    src = tmp_path / "never-valid"
    src.mkdir()
    (src / "app.json").write_text(json.dumps({"name": "never-valid", "collections": {}, "handlers": {"h": "read"}}))
    (src / "handlers.py").write_text("oops(\n")
    assert deploy(home, src)["active_version"] is None
    report = inspect_app(home, "never-valid")
    assert report["active_version"] is None
    assert report["latest_rejected"]["error"]["code"] == "HANDLER_IMPORT_ERROR"


def test_restart_with_broken_source_falls_back_to_last_valid_version(home, notes_with_data, monkeypatch):
    src, rt = notes_with_data
    edit(src / "handlers.py", "def list_notes(ctx, args):", "def list_notes(ctx, args)")

    from neai import server

    def stop_immediately(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(server.AppServer, "serve_forever", stop_immediately)
    out = io.StringIO()
    assert run(home, src, port=0, out=out, quiet=True) == 0
    deploy_line, serving_line = [json.loads(line) for line in out.getvalue().splitlines()]
    assert deploy_line["status"] == "rejected" and deploy_line["error"]["code"] == "HANDLER_IMPORT_ERROR"
    assert serving_line["status"] == "serving" and serving_line["fallback"] is True
    assert serving_line["version"] == 1
    assert titles(rt) == ["one", "two"]


def test_run_refuses_to_start_without_any_valid_version(home, tmp_path):
    src = tmp_path / "nothing"
    src.mkdir()
    (src / "app.json").write_text(json.dumps({"name": "nothing", "collections": {}, "handlers": {"h": "read"}}))
    (src / "handlers.py").write_text("syntax error here\n")
    out = io.StringIO()
    assert run(home, src, port=0, out=out, quiet=True) == 1
    assert json.loads(out.getvalue().splitlines()[0])["status"] == "rejected"


def test_validating_a_new_app_creates_nothing(home):
    for name in ("notes", "feedback"):
        assert deploy(home, APPS / name, dry_run=True)["status"] == "valid"
    assert not home.exists()


def test_read_handler_that_writes_gets_a_deploy_warning(home, app_copy):
    src = app_copy("notes")
    edit(src / "handlers.py", "def list_notes(ctx, args):\n",
         "def list_notes(ctx, args):\n    ctx.db.insert(\"notes\", {\"owner\": ctx.caller, \"title\": \"x\"})\n")
    result = deploy(home, src)
    assert result["status"] == "activated"
    assert len(result["warnings"]) == 1 and "list_notes" in result["warnings"][0]
    assert "READ_ONLY_VIOLATION" in result["warnings"][0]


def test_restoring_the_active_files_clears_latest_rejected(home, notes_with_data):
    src, _ = notes_with_data
    original = (src / "handlers.py").read_text()
    edit(src / "handlers.py", "def list_notes(ctx, args):", "def list_notes(ctx, args)")
    assert deploy(home, src)["status"] == "rejected"
    (src / "handlers.py").write_text(original)
    assert deploy(home, src)["status"] == "unchanged"
    assert inspect_app(home, "notes")["latest_rejected"] is None
