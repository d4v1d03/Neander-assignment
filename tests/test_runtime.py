import psycopg

from conftest import err, ok, write_app

# A small app whose handlers misbehave on purpose.
PROBE_MANIFEST = {
    "name": "probe",
    "collections": {"things": {"label": {"type": "string"}}},
    "handlers": {
        "sneaky_read": "read",
        "swallowing_read": "read",
        "add_then_fail": "write",
        "add": "write",
        "count": "read",
        "touch_other_app": "read",
        "spin": "read",
        "bad_result": "write",
        "app_error": "read",
        "sloppy_code": "read",
        "helper_write": "read",
    },
}
PROBE_CODE = '''
from neai import AppError

def sneaky_read(ctx, args):
    ctx.db.insert("things", {"label": "should never be stored"})

def swallowing_read(ctx, args):
    try:
        ctx.db.insert("things", {"label": "should never be stored"})
    except Exception:
        pass
    return "pretending everything is fine"

def add_then_fail(ctx, args):
    ctx.db.insert("things", {"label": "half-done"})
    ctx.log("about to fail")
    raise RuntimeError("boom")

def add(ctx, args):
    return ctx.db.insert("things", {"label": args["label"]})

def count(ctx, args):
    return ctx.db.count("things")

def touch_other_app(ctx, args):
    return ctx.db.find("notes")

def spin(ctx, args):
    while True:
        pass

def bad_result(ctx, args):
    ctx.db.insert("things", {"label": "rolled back"})
    return {"not json": object()}

def app_error(ctx, args):
    raise AppError("THING_MISSING", "no such thing", hint="create it first", details={"label": "x"})

def sloppy_code(ctx, args):
    raise AppError("not a code", "codes must be UPPER_SNAKE_CASE")

def _sneaky_helper(ctx):
    ctx.db.delete("things", 1)

def helper_write(ctx, args):
    _sneaky_helper(ctx)
'''


def probe(deployed, tmp_path, timeout=None):
    return deployed(write_app(tmp_path / "probe", PROBE_MANIFEST, PROBE_CODE), timeout=timeout)


def test_notes_are_scoped_to_the_caller(deployed):
    notes = deployed("notes")
    a = ok(notes.call("create_note", "alice", {"title": "alice's"}))
    ok(notes.call("create_note", "bob", {"title": "bob's"}))

    assert [n["title"] for n in ok(notes.call("list_notes", "alice"))] == ["alice's"]
    assert [n["title"] for n in ok(notes.call("list_notes", "bob"))] == ["bob's"]

    assert err(notes.call("get_note", "bob", {"id": a["id"]}))["code"] == "NOTE_NOT_FOUND"
    assert err(notes.call("get_note", "bob", {"id": 999}))["code"] == "NOTE_NOT_FOUND"
    assert err(notes.call("update_note", "bob", {"id": a["id"], "title": "mine now"}))["code"] == "NOTE_NOT_FOUND"
    assert ok(notes.call("get_note", "alice", {"id": a["id"]}))["title"] == "alice's"

    updated = ok(notes.call("update_note", "alice", {"id": a["id"], "body": "more"}))
    assert updated == {"id": a["id"], "owner": "alice", "title": "alice's", "body": "more"}


def test_anonymous_and_malformed_callers(deployed):
    notes = deployed("notes")
    assert err(notes.call("list_notes", None))["code"] == "AUTH_REQUIRED"  # notes has no public handlers
    assert err(notes.call("list_notes", ""))["code"] == "CALLER_INVALID"
    assert err(notes.call("list_notes", 42))["code"] == "CALLER_INVALID"
    assert err(notes.call("list_notes", " alice"))["code"] == "CALLER_INVALID"


def test_feedback_repeat_votes_are_not_counted(deployed):
    fb = deployed("feedback")
    post = ok(fb.call("create_post", "alice", {"text": "dark mode please"}))
    first = ok(fb.call("vote", "bob", {"post_id": post["id"]}))
    again = ok(fb.call("vote", "bob", {"post_id": post["id"]}))
    other = ok(fb.call("vote", "carol", {"post_id": post["id"]}))
    assert (first["counted"], first["votes"]) == (True, 1)
    assert (again["counted"], again["votes"]) == (False, 1)
    assert (other["counted"], other["votes"]) == (True, 2)

    [listed] = ok(fb.call("list_posts", "bob"))
    assert listed["votes"] == 2 and listed["voted_by_me"] is True
    assert ok(fb.call("list_posts", "dave"))[0]["voted_by_me"] is False
    assert err(fb.call("vote", "bob", {"post_id": 12345}))["code"] == "POST_NOT_FOUND"


def test_write_from_read_handler_fails_with_structured_error(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    e = err(rt.call("sneaky_read", "alice"))
    assert e["code"] == "READ_ONLY_VIOLATION"
    insert_line = PROBE_CODE.splitlines().index('    ctx.db.insert("things", {"label": "should never be stored"})') + 1
    assert e["details"] == {"handler": "sneaky_read", "op": "insert", "collection": "things",
                            "file": "handlers.py", "line": insert_line}
    assert "hint" in e
    assert ok(rt.call("count", "alice")) == 0


def test_swallowed_read_only_violation_still_fails_the_call(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    assert err(rt.call("swallowing_read", "alice"))["code"] == "READ_ONLY_VIOLATION"
    assert ok(rt.call("count", "alice")) == 0


def test_failing_write_handler_is_rolled_back_but_logged(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    e = err(rt.call("add_then_fail", "alice"))
    assert e["code"] == "HANDLER_EXCEPTION"
    assert e["details"]["exception"] == "RuntimeError"
    assert any("raise RuntimeError" in line for line in e["details"]["traceback"])
    assert ok(rt.call("count", "alice")) == 0
    messages = [entry["message"] for entry in rt.store.logs(limit=10)]
    assert "about to fail" in messages  # ctx.log survives the rollback


def test_non_json_result_rolls_back(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    assert err(rt.call("bad_result", "alice"))["code"] == "HANDLER_BAD_RESULT"
    assert ok(rt.call("count", "alice")) == 0


def test_stuck_handler_times_out(deployed, tmp_path):
    rt = probe(deployed, tmp_path, timeout=0.3)
    e = err(rt.call("spin", "alice"))
    assert e["code"] == "HANDLER_TIMEOUT"
    assert ok(rt.call("add", "alice", {"label": "still works"}))["label"] == "still works"


def test_unknown_handler_and_bad_args(deployed):
    notes = deployed("notes")
    e = err(notes.call("delete_everything", "alice"))
    assert e["code"] == "UNKNOWN_HANDLER"
    assert {"name": "list_notes", "kind": "read"} in e["details"]["available"]
    assert err(notes.call("list_notes", "alice", ["not", "an", "object"]))["code"] == "ARGS_INVALID"


def test_apps_have_separate_data(database, deployed, tmp_path):
    notes = deployed("notes")
    fb = deployed("feedback")
    rt = probe(deployed, tmp_path)
    ok(notes.call("create_note", "alice", {"title": "only in notes"}))
    ok(fb.call("create_post", "alice", {"text": "only in feedback"}))

    with psycopg.connect(database) as conn:
        rows = conn.execute("SELECT app, collection, count(*) FROM neai_records GROUP BY 1, 2 ORDER BY 1").fetchall()
    assert rows == [("feedback", "posts", 1), ("notes", "notes", 1)]

    e = err(rt.call("touch_other_app", "alice"))
    assert e["code"] == "UNKNOWN_COLLECTION"
    assert e["details"]["declared"] == ["things"]


def test_app_errors_carry_hint_and_details(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    e = err(rt.call("app_error", "alice"))
    assert e == {"code": "THING_MISSING", "kind": "app", "message": "no such thing",
                 "hint": "create it first", "details": {"label": "x"}}
    e = err(rt.call("sloppy_code", "alice"))
    assert e["code"] == "HANDLER_EXCEPTION" and "UPPER_SNAKE_CASE" in e["message"]


def test_read_only_violation_points_at_the_line_even_inside_a_helper(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    e = err(rt.call("helper_write", "alice"))
    assert e["code"] == "READ_ONLY_VIOLATION" and e["details"]["op"] == "delete"
    assert e["details"]["line"] == PROBE_CODE.splitlines().index('    ctx.db.delete("things", 1)') + 1


def test_logs_separate_bugs_from_refusals(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    rt.call("app_error", "alice")
    rt.call("nope", "alice")
    rt.call("add_then_fail", "alice")
    levels = {entry["message"]: entry["level"] for entry in rt.store.logs(limit=10) if entry["event"] == "call"}
    assert levels == {"app_error refused: THING_MISSING": "warn", "nope refused: UNKNOWN_HANDLER": "warn",
                      "add_then_fail failed: HANDLER_EXCEPTION": "error"}
    assert [entry["handler"] for entry in rt.store.logs(errors_only=True)] == ["add_then_fail"]


def test_error_responses_say_which_version_answered(deployed, tmp_path):
    rt = probe(deployed, tmp_path)
    response = rt.call("app_error", "alice")
    assert response["ok"] is False and response["version"] == 1
