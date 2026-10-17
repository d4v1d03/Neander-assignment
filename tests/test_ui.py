import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from neai.client import auth_request
from neai.errors import NeaiError
from neai.ui import ai, codecheck
from neai.ui.api import UI
from neai.ui.server import UIServer

from conftest import APPS


@pytest.mark.parametrize("code,needle", [
    ("import os", "import of 'os'"),
    ("import subprocess as sp", "import of 'subprocess'"),
    ("from socket import socket", "import from 'socket'"),
    ("from . import helpers", "import from '.'"),
    ("def h(ctx, args):\n    return open('/etc/passwd').read()", "use of 'open'"),
    ("def h(ctx, args):\n    return eval(args['x'])", "use of 'eval'"),
    ("def h(ctx, args):\n    return ().__class__.__mro__", "access to '__class__'"),
])
def test_codecheck_flags_escape_hatches(code, needle):
    problems = codecheck.check(code)
    assert any(needle in p["problem"] for p in problems), problems


def test_codecheck_passes_the_example_apps():
    for name in ("notes", "feedback"):
        assert codecheck.check((APPS / name / "handlers.py").read_text()) == []


class FakeDeepSeek(BaseHTTPRequestHandler):
    requests: list = []
    queue: list = []  # answers to send first, in order (a str is sent as-is, e.g. broken JSON)
    status = 200
    answer = {"app_json": {"name": "todo", "collections": {"items": {"text": {"type": "string"}}},
                           "handlers": {"list_items": "read"}},
              "handlers_py": "def list_items(ctx, args):\n    return ctx.db.find('items')",
              "summary": "A todo list."}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeDeepSeek.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.status != 200:
            payload = {"error": {"message": "Authentication Fails"}}
        else:
            item = FakeDeepSeek.queue.pop(0) if FakeDeepSeek.queue else self.answer
            content = item if isinstance(item, str) else json.dumps(item)
            payload = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
        data = json.dumps(payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def deepseek(monkeypatch):
    FakeDeepSeek.requests = []
    FakeDeepSeek.queue = []
    FakeDeepSeek.status = 200
    httpd = HTTPServer(("127.0.0.1", 0), FakeDeepSeek)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    yield FakeDeepSeek
    httpd.shutdown()


def test_generate_uses_deepseek_chat_completions_format(deepseek):
    draft = ai.generate("a todo list")
    [req] = deepseek.requests
    assert req["path"] == "/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer test-key"
    body = req["body"]
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    system, user = body["messages"]
    assert system["role"] == "system" and "Building apps on neai" in system["content"]
    assert "json" in system["content"]
    assert user == {"role": "user", "content": "Build this app: a todo list"}
    assert json.loads(draft["files"]["app.json"])["name"] == "todo"
    assert draft["files"]["handlers.py"].startswith("def list_items")
    assert set(draft["files"]) == {"app.json", "handlers.py"}  # no workflow: none returned


def test_fix_requests_include_the_platform_error(deepseek):
    error = {"code": "HANDLER_MISSING", "message": "list_items is not defined"}
    ai.generate("a todo list", current={"app.json": "{}", "handlers.py": "x = 1"}, error=error)
    user = deepseek.requests[0]["body"]["messages"][1]["content"]
    assert "HANDLER_MISSING" in user and "x = 1" in user


def test_deepseek_errors_are_structured(deepseek):
    deepseek.status = 401
    with pytest.raises(NeaiError) as e:
        ai.generate("a todo list")
    assert e.value.code == "AI_AUTH_FAILED" and "Authentication Fails" in e.value.message


def test_missing_key_is_reported_and_never_exposed(database, monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(NeaiError) as e:
        ai.generate("anything")
    assert e.value.code == "AI_NOT_CONFIGURED"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-value")
    config = UI(database, tmp_path / "apps").config()
    assert config["ai"]["enabled"] is True
    assert "sk-secret-value" not in json.dumps(config)


@pytest.fixture
def ui(database, tmp_path):
    instance = UI(database, tmp_path / "apps")
    yield instance
    instance.close()


def files_of(name):
    """An example app's files as the editor sends them (app.json, handlers.py and any others)."""
    root = APPS / name
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_ui_deploys_new_apps_into_the_apps_dir(ui):
    result = ui.deploy({"mode": "new", "files": files_of("notes")})
    assert result["status"] == "activated"
    assert (ui.apps_dir / "notes" / "handlers.py").exists()
    assert ui.app("notes")["files"]["handlers.py"] == files_of("notes")["handlers.py"]


def test_ui_refuses_unsafe_code(ui):
    files = files_of("notes")
    files["handlers.py"] = "import os\n" + files["handlers.py"]
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "new", "files": files})
    assert e.value.code == "UNSAFE_CODE" and e.value.details["problems"][0]["line"] == 1
    assert not (ui.apps_dir / "notes").exists()


def test_ui_asks_before_replacing_an_app_or_undeployed_edits(ui):
    ui.deploy({"mode": "new", "files": files_of("notes")})
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "new", "files": files_of("notes")})
    assert e.value.code == "APP_EXISTS"

    (ui.apps_dir / "notes" / "handlers.py").write_text("# someone's work in progress\n")
    changed = files_of("notes")
    changed["handlers.py"] += "\n# edited in the UI\n"
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "notes", "files": changed})
    assert e.value.code == "SOURCE_CHANGED"
    assert ui.deploy({"mode": "change", "app": "notes", "files": changed, "overwrite": True})["version"] == 2

    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "notes", "files": files_of("feedback")})
    assert e.value.code == "NAME_MISMATCH"


def test_ui_starts_calls_and_stops_an_app_server(ui):
    ui.deploy({"mode": "new", "files": files_of("feedback")})
    started = ui.start("feedback")
    assert started["status"] == "started" and started["url"].startswith("http://127.0.0.1:")
    login = auth_request(started["url"], "signup", body={"username": "alice", "password": "password123"})
    post = ui.call("feedback", {"handler": "create_post", "token": login["token"], "args": {"text": "hi"}})
    assert post["ok"] and post["result"]["votes"] == 0 and post["result"]["author"] == "alice"
    anonymous = ui.call("feedback", {"handler": "create_post", "args": {"text": "hi"}})
    assert anonymous["error"]["code"] == "AUTH_REQUIRED"
    assert ui.apps()["apps"][0]["running"] is True
    ui.stop("feedback")
    assert ui.apps()["apps"][0]["running"] is False


def test_ui_server_serves_the_page_with_protections(database, tmp_path):
    httpd = UIServer(0, UI(database, tmp_path / "apps"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200 and b"<title>neai</title>" in resp.read()
        assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy")
        conn.request("POST", "/api/deploy", body="{}", headers={"Content-Type": "text/plain"})
        resp = conn.getresponse()
        assert resp.status == 415 and json.loads(resp.read())["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
        conn.request("GET", "/api/apps", headers={"Host": "attacker.example"})
        resp = conn.getresponse()
        assert resp.status == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_starting_a_second_ui_on_the_same_port_says_it_is_already_running(database, tmp_path):
    import io

    from neai.ui.server import serve_ui

    httpd = UIServer(0, UI(database, tmp_path / "apps"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = io.StringIO()
        assert serve_ui(database, str(tmp_path / "apps"), port=httpd.server_address[1], out=out) == 1
        error = json.loads(out.getvalue())["error"]
        assert error["code"] == "UI_ALREADY_RUNNING" and "just open" in error["hint"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ui_signs_in_through_the_app_and_manages_users(ui):
    ui.deploy({"mode": "new", "files": files_of("feedback")})
    ui.start("feedback")
    signed = ui.auth("feedback", "signup", {"username": "alice", "password": "password123"})
    assert signed["ok"] and signed["user"] == {"username": "alice", "roles": []}
    token = signed["token"]
    assert ui.auth("feedback", "me", {"token": token})["user"]["username"] == "alice"

    post = ui.call("feedback", {"handler": "create_post", "token": token, "args": {"text": "hi"}})["result"]
    refused = ui.call("feedback", {"handler": "delete_post", "token": token, "args": {"post_id": post["id"]}})
    assert refused["error"]["code"] == "FORBIDDEN"
    assert ui.users("feedback", {"action": "grant", "username": "alice", "role": "moderator"})["status"] == "granted"
    assert ui.call("feedback", {"handler": "delete_post", "token": token, "args": {"post_id": post["id"]}})["ok"]

    added = ui.users("feedback", {"action": "add", "username": "bob", "password": "password123", "roles": ["moderator"]})
    assert added["user"] == {"username": "bob", "roles": ["moderator"]}
    listing = ui.users("feedback")
    assert listing["roles"] == ["moderator"] and [u["username"] for u in listing["users"]] == ["alice", "bob"]
    with pytest.raises(NeaiError) as e:
        ui.users("feedback", {"action": "grant", "username": "bob", "role": "wizard"})
    assert e.value.code == "ROLE_UNKNOWN"

    assert ui.auth("feedback", "logout", {"token": token})["ok"]
    assert ui.auth("feedback", "me", {"token": token})["error"]["code"] == "AUTH_INVALID"
    assert ui.app("feedback")["auth"]["users"] == 2


def test_ui_api_responses_are_never_cached(database, tmp_path):
    httpd = UIServer(0, UI(database, tmp_path / "apps"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/apps")
        resp = conn.getresponse()
        resp.read()
        assert resp.getheader("Cache-Control") == "no-store"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_editor_deploys_every_kind_of_app_file(ui):
    files = files_of("hiring")
    assert {"workflows.json", "workflows.py", "ui/index.html", "ui/style.css"} <= set(files)
    result = ui.deploy({"mode": "new", "files": files})
    assert result["status"] == "activated", result
    assert set(ui.app("hiring")["files"]) == set(files)
    # Emptying an optional file in the editor removes it from the app (and from its folder).
    smaller = {**files, "ui/style.css": "", "ui/index.html": files["ui/index.html"].replace("/style.css", "/x.css")}
    assert ui.deploy({"mode": "change", "app": "hiring", "files": smaller})["status"] == "activated"
    assert "ui/style.css" not in ui.app("hiring")["files"]
    assert not (ui.apps_dir / "hiring" / "ui" / "style.css").exists()
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "hiring", "files": {**files, "../evil.py": "x"}})
    assert e.value.code == "BAD_REQUEST"


def test_workflows_py_is_safety_checked_too(ui):
    files = {**files_of("hiring"), "workflows.py": "import subprocess\n" + files_of("hiring")["workflows.py"]}
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "new", "files": files})
    assert e.value.code == "UNSAFE_CODE" and e.value.details["file"] == "workflows.py"


def test_generate_returns_workflow_files_when_the_model_writes_them(deepseek):
    deepseek.answer = {**FakeDeepSeek.answer, "workflows_json": {"workflows": {}}, "workflows_py": "def hook(ctx, r):\n    pass"}
    try:
        draft = ai.generate("an approvals app")
    finally:
        deepseek.answer = FakeDeepSeek.answer
    assert set(draft["files"]) == {"app.json", "handlers.py", "workflows.json", "workflows.py"}
    system = deepseek.requests[0]["body"]["messages"][0]["content"]
    assert "workflows_json" in system


def test_generate_ui_writes_index_html_from_the_contract(deepseek):
    original = FakeDeepSeek.answer
    deepseek.answer = {"ui_html": "<!doctype html><script src='/_neai/sdk.js'></script>", "summary": "A page."}
    try:
        draft = ai.generate_ui("make it friendly", files=files_of("notes"))
    finally:
        deepseek.answer = original
    assert draft["files"] == {"ui/index.html": "<!doctype html><script src='/_neai/sdk.js'></script>\n"}
    system, user = deepseek.requests[0]["body"]["messages"]
    assert "/_neai/sdk.js" in system["content"] and "innerHTML" in system["content"]
    assert "create_note" in user["content"] and "make it friendly" in user["content"]


def test_redeploying_after_a_rejected_first_try_needs_no_confirmation(ui):
    broken = {**files_of("notes"), "handlers.py": files_of("notes")["handlers.py"].replace("def list_notes(ctx, args):", "def list_notes(ctx, args)")}
    assert ui.deploy({"mode": "new", "files": broken})["status"] == "rejected"
    # Same editor, fixed files: no APP_EXISTS (it never went live) and no SOURCE_CHANGED (it wrote those files).
    result = ui.deploy({"mode": "new", "files": files_of("notes"), "previous": broken})
    assert result["status"] == "activated"
    # Someone else's edits on disk are still protected:
    (ui.apps_dir / "notes" / "handlers.py").write_text("# edited by hand\n" + files_of("notes")["handlers.py"])
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "notes", "files": {**files_of("notes"), "handlers.py": "x = 1\n"},
                   "previous": files_of("notes")})
    assert e.value.code == "SOURCE_CHANGED"


def test_editing_an_app_that_never_went_live_opens_its_folder(ui):
    broken = {**files_of("notes"), "tests.json": files_of("notes")["tests.json"].replace('"groceries"', '"wrong"', 1)}
    assert ui.deploy({"mode": "new", "files": broken})["status"] == "rejected"
    opened = ui.app("notes")["files"]
    assert opened == broken  # the draft from the folder, not a template
    fixed = {**opened, "tests.json": files_of("notes")["tests.json"]}
    assert ui.deploy({"mode": "change", "app": "notes", "files": fixed, "previous": opened})["status"] == "activated"


def test_common_json_slips_in_model_answers_are_forgiven():
    assert ai._json_answer('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai._json_answer('{"code": "x = \'it\\\'s\'"}') == {"code": "x = 'it\\'s'"}  # \' kept for Python
    assert ai._json_answer('{"a": "line1\nline2"}') == {"a": "line1\nline2"}            # a raw newline
    assert ai._json_answer('{"a": 1} and some words') == {"a": 1}
    with pytest.raises(NeaiError) as e:
        ai._json_answer('{"app_json": {"name": "x"}, "handlers_py": "def f(')
    assert e.value.code == "AI_BAD_RESPONSE" and "character" in e.value.message
    assert e.value.details["end"].endswith("def f(") and e.value.details["kind"] == "invalid_json"


def test_an_answer_that_isnt_json_is_asked_for_again_once(deepseek):
    deepseek.queue = ['{"app_json": {"name": "todo"}, "handlers_py": "def f(']  # cut short, then a good answer
    draft = ai.generate("a todo list")
    assert json.loads(draft["files"]["app.json"])["name"] == "todo"
    assert len(deepseek.requests) == 2
    retry = deepseek.requests[1]["body"]["messages"]
    assert retry[-2]["role"] == "assistant" and "isn't valid JSON" in retry[-1]["content"]


def test_a_second_bad_answer_is_reported_with_where_it_broke(deepseek):
    deepseek.queue = ["{not json", "{still not json"]
    with pytest.raises(NeaiError) as e:
        ai.generate("a todo list")
    assert e.value.code == "AI_BAD_RESPONSE" and e.value.details["near"] and len(deepseek.requests) == 2
