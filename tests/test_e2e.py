import http.client
import json
import os
import subprocess
import sys
import urllib.request

import pytest

from conftest import APPS


PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def cli_home(home, monkeypatch):
    # Where the CLI saves logins in these tests.
    monkeypatch.setenv("NEAI_HOME", str(home))


def neai(*args, stdin=None):
    proc = subprocess.run([sys.executable, "-m", "neai", *args], capture_output=True, text=True,
                          env=os.environ.copy(), timeout=30, input=stdin)
    return json.loads(proc.stdout), proc.returncode


def cli_signup(app, username):
    out, code = neai("signup", app, username, "--password-stdin", stdin=PASSWORD + "\n")
    assert code == 0 and out["status"] == "signed_up", out
    return out


def start(*args):
    """Start `neai <args>` and wait for its "serving"/"proxying" line."""
    proc = subprocess.Popen([sys.executable, "-m", "neai", *args, "--port", "0", "--quiet"],
                            stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    lines = []
    for line in proc.stdout:
        lines.append(json.loads(line))
        if lines[-1].get("status") in ("serving", "proxying") or not lines[-1].get("ok", True):
            break
    assert lines and lines[-1].get("status") in ("serving", "proxying"), lines
    return proc, lines


def stop(proc):
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def server(database):
    proc, lines = start("run", str(APPS / "notes"))
    assert lines[0]["status"] == "activated"
    yield lines[-1]
    stop(proc)


def raw_post(url, path, body, headers):
    host, port = url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read()), dict(resp.getheaders())


def signup(url, username):
    status, out, _ = raw_post(url, "/auth/signup", json.dumps({"username": username, "password": PASSWORD}),
                              {"Content-Type": "application/json"})
    assert status == 200 and out["ok"], out
    return out["token"]


def post(url, handler, token, args=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return raw_post(url, f"/call/{handler}", json.dumps({"args": args or {}}), headers)


def test_cli_and_server_round_trip(server):
    cli_signup("notes", "alice")
    cli_signup("notes", "bob")
    out, code = neai("call", "notes", "create_note", "--as", "alice", "--args", '{"title": "hello"}')
    assert code == 0 and out["result"]["title"] == "hello"

    out, code = neai("call", "notes", "list_notes", "--as", "bob")
    assert code == 0 and out["result"] == []

    out, code = neai("call", "notes", "get_note", "--as", "bob", "--args", '{"id": 1}')
    assert code == 1 and out["error"]["code"] == "NOTE_NOT_FOUND"

    report, code = neai("inspect", "notes")
    assert code == 0
    assert report["app"]["name"] == "notes" and report["active_version"]["version"] == 1
    assert report["active_version"]["files"] == ["app.json", "handlers.py", "tests.json"]
    assert {h["name"] for h in report["handlers"]} == {"create_note", "list_notes", "get_note", "update_note"}
    assert report["handlers"][2] == {"name": "get_note", "kind": "read", "access": "user",
                                     "args": {"id": {"type": "integer", "required": True}}}
    assert report["data"]["notes"]["count"] == 1
    assert report["server"]["running"] is True and report["server"]["url"] == server["url"]
    assert report["recent_errors"] == []
    assert report["recent_logs"][0]["message"] == "get_note refused: NOTE_NOT_FOUND"
    assert report["recent_logs"][0]["caller"] == "bob"

    logs, _ = neai("logs", "notes", "--errors")
    assert logs["logs"] == []

    out, code = neai("call", "notes", "list_notes", "--as", "nobody")
    assert code == 1 and out["error"]["code"] == "NOT_LOGGED_IN" and "neai login notes nobody" in out["error"]["hint"]

    out, code = neai("call", "nots", "list_notes", "--as", "bob")
    assert code == 1 and out["error"]["code"] == "APP_NOT_FOUND" and out["error"]["details"]["known_apps"] == ["notes"]


def test_server_rejects_cross_site_and_rebinding_requests(server):
    body = json.dumps({"args": {"title": "x"}})
    status, out, _ = raw_post(server["url"], "/call/create_note", body, {"Content-Type": "text/plain"})
    assert (status, out["error"]["code"]) == (415, "UNSUPPORTED_MEDIA_TYPE")
    status, out, _ = raw_post(server["url"], "/call/create_note", body,
                              {"Content-Type": "application/json", "Host": "evil.example:80"})
    assert (status, out["error"]["code"]) == (403, "FORBIDDEN_HOST")
    status, out, _ = raw_post(server["url"], "/call/create_note", json.dumps({"argz": {}}),
                              {"Content-Type": "application/json"})
    assert (status, out["error"]["code"]) == (400, "BAD_REQUEST")


def test_callers_come_from_tokens_not_from_the_request(server):
    status, out, _ = raw_post(server["url"], "/call/list_notes", json.dumps({"caller": "alice"}),
                              {"Content-Type": "application/json"})
    assert (status, out["error"]["code"]) == (400, "BAD_REQUEST") and "Bearer" in out["error"]["hint"]
    status, out, _ = post(server["url"], "list_notes", None)
    assert (status, out["error"]["code"]) == (401, "AUTH_REQUIRED")
    status, out, _ = post(server["url"], "list_notes", "neai_made-up-token")
    assert (status, out["error"]["code"]) == (401, "AUTH_INVALID")
    status, out, _ = raw_post(server["url"], "/call/list_notes", "{}",
                              {"Content-Type": "application/json", "Authorization": "Basic abc"})
    assert (status, out["error"]["code"]) == (401, "AUTH_INVALID")
    token = signup(server["url"], "alice")
    status, out, _ = post(server["url"], "create_note", token, {"title": "mine"})
    assert status == 200 and out["result"]["owner"] == "alice"


def test_cli_errors_are_json(database):
    out, code = neai("inspect", "nope")
    assert code == 1 and out["error"]["code"] == "APP_NOT_FOUND"
    out, code = neai("call", "notes")
    assert code == 2 and out["error"]["code"] == "USAGE_ERROR"
    # what zsh passes for an unquoted --args {"title":"x","body":"y"}: quotes removed, braces split
    out, code = neai("call", "notes", "create_note", "--as", "alice", "--args", "title:x", "body:y")
    assert code == 2 and "single quotes" in out["error"]["hint"]
    out, code = neai("call", "notes", "create_note", "--as", "alice", "--args", "{title:x}")
    assert out["error"]["code"] == "ARGS_INVALID" and "single quotes" in out["error"]["hint"]
    out, code = neai("inspect", "../../etc")
    assert code == 1 and out["error"]["code"] == "INVALID_APP_NAME"


def test_unreachable_database_is_a_clear_error(database):
    out, code = neai("inspect", "notes", "--db", "postgresql://neai:neai@127.0.0.1:1/neai")
    assert code == 1 and out["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "docker compose up" in out["error"]["hint"]
    assert ":neai@" not in json.dumps(out)  # the password is never echoed


def test_new_creates_a_deployable_skeleton(database, tmp_path):
    out, code = neai("new", "todo", "--dir", str(tmp_path / "todo"))
    assert code == 0 and out["status"] == "created"
    out, code = neai("deploy", str(tmp_path / "todo"))
    assert code == 0 and out["status"] == "activated"


def test_servers_are_stateless_copies(database):
    """Two servers for one app share everything through the database; neither keeps state."""
    neai("deploy", str(APPS / "notes"))
    a, [a_info] = start("serve", "notes")
    b, [b_info] = start("serve", "notes")
    try:
        assert a_info["url"] != b_info["url"]
        token = signup(a_info["url"], "alice")  # logged in on A ...
        _, created, _ = post(a_info["url"], "create_note", token, {"title": "written via A"})
        _, listed, _ = post(b_info["url"], "list_notes", token)  # ... and the same token works on B
        assert [n["title"] for n in listed["result"]] == ["written via A"]

        ps, _ = neai("ps", "notes")
        assert {i["url"] for i in ps["instances"]} == {a_info["url"], b_info["url"]}

        stop(a)
        ps, _ = neai("ps", "notes")
        assert [i["url"] for i in ps["instances"]] == [b_info["url"]]
        out, code = neai("call", "notes", "get_note", "--token", token, "--args", json.dumps({"id": created["result"]["id"]}))
        assert code == 0 and out["result"]["title"] == "written via A"
    finally:
        for proc in (a, b):
            if proc.poll() is None:
                stop(proc)


def test_proxy_spreads_requests_and_survives_a_dead_instance(database):
    neai("deploy", str(APPS / "notes"))
    a, [a_info] = start("serve", "notes")
    b, [b_info] = start("serve", "notes")
    proxy, [p_info] = start("proxy", "notes")
    try:
        assert set(p_info["instances"]) == {a_info["url"], b_info["url"]}
        token = signup(p_info["url"], "alice")
        served_by = set()
        for i in range(4):
            status, out, headers = post(p_info["url"], "create_note", token, {"title": f"n{i}"})
            assert status == 200 and out["ok"]
            served_by.add(headers["X-Neai-Instance"])
        assert served_by == {a_info["url"], b_info["url"]}

        stop(a)
        for _ in range(3):
            status, out, headers = post(p_info["url"], "list_notes", token)
            assert status == 200 and len(out["result"]) == 4
            assert headers["X-Neai-Instance"] == b_info["url"]
    finally:
        for proc in (a, b, proxy):
            if proc.poll() is None:
                stop(proc)


def test_a_dropped_database_connection_is_replaced_without_failing_a_call(database):
    """Kill the server's connection from the database side (what a Postgres restart does)."""
    import psycopg

    neai("deploy", str(APPS / "notes"))
    proc, [info] = start("serve", "notes")
    try:
        token = signup(info["url"], "alice")
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        status, out, _ = post(info["url"], "list_notes", token)
        assert status == 200 and out["ok"], out
    finally:
        stop(proc)


def test_health_reports_the_instance(server):
    with urllib.request.urlopen(server["url"] + "/_neai/health", timeout=5) as resp:
        health = json.loads(resp.read())
    assert health["ok"] and health["instance"] == server["instance"] and health["version"] == 1
