"""Many requests at once: a server's threads share one runtime and one pool of connections."""
import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from neai import database as database_module
from neai import deploy as deploy_module
from neai import runtime as runtime_module
from neai.auth import user_admin
from neai.database import Database
from neai.deploy import deploy
from neai.errors import HandlerTimeout, NeaiError
from neai.runtime import Runtime, time_limit
from neai.store import list_instances
from neai.ui import api as ui_api
from neai.ui.api import UI
from neai.ui.server import UIServer

from conftest import err, ok, write_app
from test_e2e import raw_post, start, stop
from test_ui import files_of

PASSWORD = "password123"
MANIFEST = {
    "name": "busy",
    "auth": {"signup": "open", "roles": ["a", "b"]},
    "collections": {"items": {"name": {"type": "string"}}},
    "unique": {"items": [["name"]]},
    "handlers": {
        "add": {"kind": "write", "args": {"name": {"type": "string"}}},
        "count": "read",
        "spin": "write",
        "doze": "write",
        "nap": {"kind": "read", "access": "public"},
        "ping": {"kind": "read", "access": "public"},
    },
}
CODE = '''
import time

def add(ctx, args):
    return ctx.db.insert("items", {"name": args["name"]})

def count(ctx, args):
    return len(ctx.db.find("items"))

def spin(ctx, args):
    ctx.db.insert("items", {"name": "half-done"})
    while True:
        pass

def doze(ctx, args):
    ctx.db.insert("items", {"name": "dozing"})
    time.sleep(4)

def nap(ctx, args):
    time.sleep(1.5)
    return "rested"

def ping(ctx, args):
    return "pong"
'''


@pytest.fixture
def busy(deployed, tmp_path):
    def make(timeout=None):
        return deployed(write_app(tmp_path / "busy", MANIFEST, CODE), timeout=timeout)
    return make


def in_parallel(fn, items):
    """Run fn(item) for every item, each on its own thread, all released at the same moment."""
    items = list(items)
    gate = threading.Barrier(len(items))

    def run(item):
        gate.wait(timeout=10)
        return fn(item)

    with ThreadPoolExecutor(len(items)) as pool:
        return list(pool.map(run, items))


# The runtime and the pool, shared by threads.

def test_parallel_writes_keep_every_record(busy):
    rt = busy()
    results = in_parallel(lambda i: rt.call("add", "alice", {"name": f"item-{i}"}), range(40))
    ids = [ok(r)["id"] for r in results]
    assert len(set(ids)) == 40
    assert ok(rt.call("count", "alice")) == 40


def test_unique_rules_hold_under_parallel_writes(busy):
    rt = busy()
    results = in_parallel(lambda _: rt.call("add", "alice", {"name": "same"}), range(12))
    assert sum(r["ok"] for r in results) == 1
    assert {r["error"]["code"] for r in results if not r["ok"]} == {"UNIQUE_VIOLATION"}


def test_a_new_version_is_loaded_once_however_many_threads_notice_it(busy, database, tmp_path, monkeypatch):
    rt = busy()
    ok(rt.call("ping"))
    imports = []
    real_import = runtime_module.import_source

    def slow_import(*args):
        imports.append(args[1])
        time.sleep(0.2)  # a big app: plenty of time for the other threads to arrive meanwhile
        return real_import(*args)

    monkeypatch.setattr(runtime_module, "import_source", slow_import)
    assert deploy(database, write_app(tmp_path / "busy2", MANIFEST, CODE + "\n# v2\n"))["version"] == 2
    results = in_parallel(lambda _: rt.call("ping"), range(16))
    assert all(r["ok"] and r["version"] == 2 for r in results)
    assert len(imports) == 1


def test_a_stuck_handler_on_a_worker_thread_times_out_and_releases_the_app(busy):
    rt = busy(timeout=0.3)
    with ThreadPoolExecutor(2) as pool:
        stuck = pool.submit(rt.call, "spin", "alice").result(timeout=10)
        assert err(stuck)["code"] == "HANDLER_TIMEOUT"
        # Its write was rolled back and the app's write lock let go: the next write goes through.
        assert ok(pool.submit(rt.call, "add", "alice", {"name": "after"}).result(timeout=10))["name"] == "after"
    assert ok(rt.call("count", "alice")) == 1


def test_a_sleeping_handler_cant_hold_the_apps_write_lock(busy):
    # A worker thread's time limit can't cut a sleep short, but Postgres ends the idle transaction.
    rt = busy(timeout=1.0)
    with ThreadPoolExecutor(2) as pool:
        dozing = pool.submit(rt.call, "doze", "alice")
        time.sleep(0.2)
        started = time.monotonic()
        added = pool.submit(rt.call, "add", "alice", {"name": "not held up"}).result(timeout=10)
        assert ok(added)["name"] == "not held up"
        assert time.monotonic() - started < 3  # well before the sleeper wakes up (at 4 s)
        assert err(dozing.result(timeout=10))["code"] == "HANDLER_TIMEOUT"
    assert ok(rt.call("count", "alice")) == 1  # the sleeper's insert was rolled back


def test_time_limit_stops_a_loop_on_a_worker_thread():
    def work():
        with time_limit(0.2):
            while True:
                pass

    with ThreadPoolExecutor(1) as pool, pytest.raises(HandlerTimeout):
        pool.submit(work).result(timeout=5)


def test_time_limit_never_fires_after_its_block_ends():
    def work():
        stopped = 0
        for i in range(150):
            try:
                with time_limit(0.002):
                    end = time.perf_counter() + 0.001 * (i % 15)  # 0-14 ms: some finish in time, some don't
                    while time.perf_counter() < end:
                        pass
            except HandlerTimeout:
                stopped += 1
            for _ in range(2000):  # a late timeout would land here and escape
                pass
        return stopped

    with ThreadPoolExecutor(1) as pool:
        assert 0 < pool.submit(work).result(timeout=30) < 150


def test_a_full_pool_makes_requests_wait_then_says_busy(database, monkeypatch):
    monkeypatch.setattr(database_module, "POOL_WAIT_SECONDS", 0.3)
    db = Database(database, pool_size=2)
    release, holding = threading.Event(), threading.Barrier(3)

    def hold():
        with db.transaction():
            db.execute("SELECT 1")
            holding.wait(timeout=5)
            release.wait(timeout=5)

    with ThreadPoolExecutor(2) as pool:
        held = [pool.submit(hold) for _ in range(2)]
        holding.wait(timeout=5)
        with pytest.raises(NeaiError) as e:
            db.execute("SELECT 1")
        assert e.value.code == "DATABASE_BUSY" and e.value.http_status == 503
        release.set()
        [f.result(timeout=5) for f in held]
    assert db.execute("SELECT 2").fetchone() == (2,)
    db.close()


def test_a_burst_of_wrong_passwords_cant_get_past_the_lockout(busy):
    rt = busy()
    ok_signup = rt.auth("signup", {"username": "bob", "password": PASSWORD})
    assert ok_signup["ok"], ok_signup
    results = in_parallel(lambda _: rt.auth("login", {"username": "bob", "password": "wrong-guess"}), range(10))
    codes = [r["error"]["code"] for r in results]
    assert codes.count("LOGIN_FAILED") == 5 and codes.count("LOGIN_LOCKED") == 5


def test_roles_granted_at_the_same_time_all_stick(busy):
    rt = busy()
    manifest = rt.refresh().manifest
    for n in range(5):
        user_admin(rt.store, manifest, "add", username=f"u{n}", password=PASSWORD)
        in_parallel(lambda role: user_admin(rt.store, manifest, "grant", username=f"u{n}", role=role), ["a", "b"])
        assert sorted(rt.store.get_user(f"u{n}")["roles"]) == ["a", "b"]


def test_a_deploy_compares_with_whatever_went_live_while_its_tests_ran(database, tmp_path, monkeypatch):
    assert deploy(database, write_app(tmp_path / "v1", MANIFEST, CODE))["version"] == 1
    newer = CODE + "\n# v2\n"
    real_tests, others = deploy_module.run_tests, []

    def tests_while_someone_else_deploys(*args, **kwargs):
        result = real_tests(*args, **kwargs)
        if not others:
            others.append(None)  # first, so their deploy (which runs tests too) doesn't start another
            others[0] = deploy(database, write_app(tmp_path / "theirs", MANIFEST, newer))
        return result

    monkeypatch.setattr(deploy_module, "run_tests", tests_while_someone_else_deploys)
    mine = deploy(database, write_app(tmp_path / "mine", MANIFEST, newer))
    assert others[0]["status"] == "activated" and others[0]["version"] == 2
    assert mine["status"] == "unchanged" and mine["version"] == 2  # not a duplicate version 3


# Real servers.

def test_a_slow_request_doesnt_hold_up_the_others(database, tmp_path):
    proc, lines = start("run", str(write_app(tmp_path / "busy", MANIFEST, CODE)))
    url = lines[-1]["url"]
    headers = {"Content-Type": "application/json"}
    try:
        with ThreadPoolExecutor(1) as pool:
            napping = pool.submit(raw_post, url, "/call/nap", "{}", headers)
            time.sleep(0.2)
            started = time.monotonic()
            status, out, _ = raw_post(url, "/call/ping", "{}", headers)
            assert status == 200 and out["result"] == "pong"
            assert time.monotonic() - started < 1.0
            assert not napping.done()
            assert napping.result(timeout=10)[1]["result"] == "rested"
    finally:
        stop(proc)


class SlowDeepSeek(BaseHTTPRequestHandler):
    delay = 1.0
    running = 0
    most = 0
    lock = threading.Lock()
    answer = {"app_json": {"name": "todo", "collections": {"items": {"text": {"type": "string"}}},
                           "handlers": {"list_items": "read"}},
              "handlers_py": "def list_items(ctx, args):\n    return ctx.db.find('items')",
              "summary": "A todo list."}

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        cls = SlowDeepSeek
        with cls.lock:
            cls.running += 1
            cls.most = max(cls.most, cls.running)
        time.sleep(cls.delay)
        with cls.lock:
            cls.running -= 1
        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(cls.answer)},
                                        "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_deepseek(monkeypatch):
    SlowDeepSeek.running = SlowDeepSeek.most = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), SlowDeepSeek)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    yield SlowDeepSeek
    httpd.shutdown()


@pytest.fixture
def dashboard(database, tmp_path):
    def make():
        httpd = UIServer(0, UI(database, tmp_path / "apps"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    servers = []
    yield make
    for httpd in servers:
        httpd.shutdown()
        httpd.ui.close()


def dashboard_request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return json.loads(resp.read())


def test_the_dashboard_keeps_answering_while_deepseek_works(slow_deepseek, dashboard):
    port = dashboard()
    with ThreadPoolExecutor(1) as pool:
        generating = pool.submit(dashboard_request, port, "POST", "/api/ai", {"request": "a todo list"})
        time.sleep(0.2)
        started = time.monotonic()
        assert dashboard_request(port, "GET", "/api/apps")["ok"]
        assert time.monotonic() - started < 0.5
        assert not generating.done()
        assert generating.result(timeout=10)["ok"]


def test_deepseek_calls_take_turns_beyond_the_limit(slow_deepseek, dashboard, monkeypatch):
    monkeypatch.setenv("NEAI_AI_CONCURRENCY", "1")
    port = dashboard()
    results = in_parallel(lambda _: dashboard_request(port, "POST", "/api/ai", {"request": "a todo list"}), range(2))
    assert all(r["ok"] for r in results)
    assert slow_deepseek.most == 1  # the second waited for the first


def test_deepseek_queue_gives_up_with_a_clear_error(slow_deepseek, dashboard, monkeypatch):
    monkeypatch.setenv("NEAI_AI_CONCURRENCY", "1")
    monkeypatch.setattr(ui_api, "AI_WAIT_SECONDS", 0.3)
    port = dashboard()
    results = in_parallel(lambda _: dashboard_request(port, "POST", "/api/ai", {"request": "a todo list"}), range(2))
    assert sorted(r["ok"] for r in results) == [False, True]
    assert next(r for r in results if not r["ok"])["error"]["code"] == "AI_BUSY"


def test_two_start_clicks_start_one_server(database, tmp_path):
    ui = UI(database, tmp_path / "apps")
    try:
        assert ui.deploy({"mode": "new", "files": files_of("feedback")})["ok"]
        first, second = in_parallel(lambda _: ui.start("feedback"), range(2))
        assert sorted([first["status"], second["status"]]) == ["running", "started"]
        assert first["url"] == second["url"]
        assert len(list_instances(ui.db, "feedback")) == 1
    finally:
        ui.close()


def test_saving_one_app_waits_for_its_other_save_but_not_for_other_apps(database, tmp_path, monkeypatch):
    ui = UI(database, tmp_path / "apps")
    spans, real_deploy = [], ui_api.deploy

    def slow_deploy(db_url, target):
        began = time.monotonic()
        time.sleep(0.3)
        out = real_deploy(db_url, target)
        spans.append((target.name, began, time.monotonic()))
        return out

    monkeypatch.setattr(ui_api, "deploy", slow_deploy)
    saves = [("notes", files_of("notes")), ("notes", files_of("notes")), ("feedback", files_of("feedback"))]
    try:
        results = in_parallel(lambda s: ui.deploy({"mode": "new", "overwrite": True, "files": s[1]}), saves)
        assert all(r["ok"] for r in results), results
    finally:
        ui.close()
    notes = sorted((b, e) for name, b, e in spans if name == "notes")
    [(_, f_began, f_ended)] = [s for s in spans if s[0] == "feedback"]
    assert notes[0][1] <= notes[1][0]  # the two saves of notes took turns
    assert f_began < max(e for _, e in notes) and f_ended > min(b for b, _ in notes)  # feedback didn't wait
