import http.client
import json
import os
import subprocess
import sys

import pytest

from conftest import APPS


def neai(home, *args):
    proc = subprocess.run([sys.executable, "-m", "neai", *args], capture_output=True, text=True,
                          env={**os.environ, "NEAI_HOME": str(home)}, timeout=30)
    return json.loads(proc.stdout), proc.returncode


@pytest.fixture
def server(home):
    proc = subprocess.Popen([sys.executable, "-m", "neai", "run", str(APPS / "notes"), "--port", "0", "--quiet"],
                            stdout=subprocess.PIPE, text=True, env={**os.environ, "NEAI_HOME": str(home)})
    try:
        deployed = json.loads(proc.stdout.readline())
        serving = json.loads(proc.stdout.readline())
        assert deployed["status"] == "activated" and serving["status"] == "serving", (deployed, serving)
        yield serving
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def raw_post(url, path, body, headers):
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_cli_and_server_round_trip(home, server):
    out, code = neai(home, "call", "notes", "create_note", "--as", "alice", "--args", '{"title": "hello"}')
    assert code == 0 and out["result"]["title"] == "hello"

    out, code = neai(home, "call", "notes", "list_notes", "--as", "bob")
    assert code == 0 and out["result"] == []

    out, code = neai(home, "call", "notes", "get_note", "--as", "bob", "--args", '{"id": 1}')
    assert code == 1 and out["error"]["code"] == "NOTE_NOT_FOUND"

    report, code = neai(home, "inspect", "notes")
    assert code == 0
    assert report["app"]["name"] == "notes" and report["active_version"]["version"] == 1
    assert {h["name"] for h in report["handlers"]} == {"create_note", "list_notes", "get_note", "update_note"}
    assert report["handlers"][2] == {"name": "get_note", "kind": "read", "args": {"id": {"type": "integer", "required": True}}}
    assert report["data"]["notes"]["count"] == 1
    assert report["server"]["running"] is True
    assert report["recent_errors"] == []
    assert report["recent_logs"][0]["message"] == "get_note refused: NOTE_NOT_FOUND"

    logs, _ = neai(home, "logs", "notes", "--errors")
    assert logs["logs"] == []

    out, code = neai(home, "call", "nots", "list_notes", "--as", "bob")
    assert code == 1 and out["error"]["code"] == "APP_NOT_FOUND" and out["error"]["details"]["known_apps"] == ["notes"]


def test_server_rejects_cross_site_and_rebinding_requests(server):
    body = json.dumps({"caller": "alice", "args": {"title": "x"}})
    status, out = raw_post(server["url"], "/call/create_note", body, {"Content-Type": "text/plain"})
    assert (status, out["error"]["code"]) == (415, "UNSUPPORTED_MEDIA_TYPE")
    status, out = raw_post(server["url"], "/call/create_note", body,
                           {"Content-Type": "application/json", "Host": "evil.example:80"})
    assert (status, out["error"]["code"]) == (403, "FORBIDDEN_HOST")
    status, out = raw_post(server["url"], "/call/create_note", json.dumps({"caler": "typo"}),
                           {"Content-Type": "application/json"})
    assert (status, out["error"]["code"]) == (400, "BAD_REQUEST")


def test_cli_errors_are_json(home):
    out, code = neai(home, "inspect", "nope")
    assert code == 1 and out["error"]["code"] == "APP_NOT_FOUND"
    out, code = neai(home, "call", "notes", "list_notes")
    assert code == 2 and out["error"]["code"] == "USAGE_ERROR"
    # what zsh passes for an unquoted --args {"title":"x","body":"y"}: quotes removed, braces split
    out, code = neai(home, "call", "notes", "create_note", "--as", "alice", "--args", "title:x", "body:y")
    assert code == 2 and "single quotes" in out["error"]["hint"]
    out, code = neai(home, "call", "notes", "create_note", "--as", "alice", "--args", "{title:x}")
    assert out["error"]["code"] == "ARGS_INVALID" and "single quotes" in out["error"]["hint"]
    out, code = neai(home, "inspect", "../../etc")
    assert code == 1 and out["error"]["code"] == "INVALID_APP_NAME"


def test_new_creates_a_deployable_skeleton(home, tmp_path):
    out, code = neai(home, "new", "todo", "--dir", str(tmp_path / "todo"))
    assert code == 0 and out["status"] == "created"
    out, code = neai(home, "deploy", str(tmp_path / "todo"))
    assert code == 0 and out["status"] == "activated"


def test_windows_style_liveness_check_probes_the_port(home, monkeypatch):
    import socket

    from neai import paths
    from neai.deploy import deploy

    deploy(home, APPS / "notes")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    paths.server_file(home, "notes").write_text(json.dumps({"pid": 999999, "port": port, "url": "x"}))
    monkeypatch.setattr(paths, "PROBE_PORT_FOR_LIVENESS", True)
    assert paths.read_server_info(home, "notes")["port"] == port
    listener.close()
    assert paths.read_server_info(home, "notes") is None
