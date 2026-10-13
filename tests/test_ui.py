import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

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
            payload = {"choices": [{"message": {"role": "assistant", "content": json.dumps(self.answer)},
                                    "finish_reason": "stop"}]}
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
    assert json.loads(draft["app_json"])["name"] == "todo"
    assert draft["handlers_py"].startswith("def list_items")


def test_fix_requests_include_the_platform_error(deepseek):
    error = {"code": "HANDLER_MISSING", "message": "list_items is not defined"}
    ai.generate("a todo list", current={"app_json": "{}", "handlers_py": "x = 1"}, error=error)
    user = deepseek.requests[0]["body"]["messages"][1]["content"]
    assert "HANDLER_MISSING" in user and "x = 1" in user


def test_deepseek_errors_are_structured(deepseek):
    deepseek.status = 401
    with pytest.raises(NeaiError) as e:
        ai.generate("a todo list")
    assert e.value.code == "AI_AUTH_FAILED" and "Authentication Fails" in e.value.message


def test_missing_key_is_reported_and_never_exposed(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(NeaiError) as e:
        ai.generate("anything")
    assert e.value.code == "AI_NOT_CONFIGURED"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-value")
    config = UI(tmp_path / "home", tmp_path / "apps").config()
    assert config["ai"]["enabled"] is True
    assert "sk-secret-value" not in json.dumps(config)


@pytest.fixture
def ui(tmp_path):
    instance = UI(tmp_path / "home", tmp_path / "apps")
    yield instance
    instance.close()


def files_of(name):
    return {"app_json": (APPS / name / "app.json").read_text(), "handlers_py": (APPS / name / "handlers.py").read_text()}


def test_ui_deploys_new_apps_into_the_apps_dir(ui):
    result = ui.deploy({"mode": "new", **files_of("notes")})
    assert result["status"] == "activated"
    assert (ui.apps_dir / "notes" / "handlers.py").exists()
    assert ui.app("notes")["files"]["handlers_py"] == files_of("notes")["handlers_py"]


def test_ui_refuses_unsafe_code(ui):
    files = files_of("notes")
    files["handlers_py"] = "import os\n" + files["handlers_py"]
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "new", **files})
    assert e.value.code == "UNSAFE_CODE" and e.value.details["problems"][0]["line"] == 1
    assert not (ui.apps_dir / "notes").exists()


def test_ui_asks_before_replacing_an_app_or_undeployed_edits(ui):
    ui.deploy({"mode": "new", **files_of("notes")})
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "new", **files_of("notes")})
    assert e.value.code == "APP_EXISTS"

    (ui.apps_dir / "notes" / "handlers.py").write_text("# someone's work in progress\n")
    changed = files_of("notes")
    changed["handlers_py"] += "\n# edited in the UI\n"
    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "notes", **changed})
    assert e.value.code == "SOURCE_CHANGED"
    assert ui.deploy({"mode": "change", "app": "notes", **changed, "overwrite": True})["version"] == 2

    with pytest.raises(NeaiError) as e:
        ui.deploy({"mode": "change", "app": "notes", **files_of("feedback")})
    assert e.value.code == "NAME_MISMATCH"


def test_ui_starts_calls_and_stops_an_app_server(ui):
    ui.deploy({"mode": "new", **files_of("feedback")})
    started = ui.start("feedback")
    assert started["status"] == "started" and started["url"].startswith("http://127.0.0.1:")
    post = ui.call("feedback", {"handler": "create_post", "caller": "alice", "args": {"text": "hi"}})
    assert post["ok"] and post["result"]["votes"] == 0
    assert ui.apps()["apps"][0]["running"] is True
    ui.stop("feedback")
    assert ui.apps()["apps"][0]["running"] is False


def test_ui_server_serves_the_page_with_protections(tmp_path):
    httpd = UIServer(0, UI(tmp_path / "home", tmp_path / "apps"))
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


def test_starting_a_second_ui_on_the_same_port_says_it_is_already_running(tmp_path):
    import io

    from neai.ui.server import serve_ui

    httpd = UIServer(0, UI(tmp_path / "home", tmp_path / "apps"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = io.StringIO()
        assert serve_ui(tmp_path / "home", str(tmp_path / "apps"), port=httpd.server_address[1], out=out) == 1
        error = json.loads(out.getvalue())["error"]
        assert error["code"] == "UI_ALREADY_RUNNING" and "just open" in error["hint"]
    finally:
        httpd.shutdown()
        httpd.server_close()
