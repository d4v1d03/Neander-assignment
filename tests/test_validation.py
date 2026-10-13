import pytest

from neai.errors import NeaiError
from neai.schema import Field, parse_manifest, validate_record

from conftest import err, ok, write_app

GOOD = {
    "name": "demo",
    "collections": {"items": {"title": {"type": "string"}, "n": {"type": "integer", "required": False}}},
    "handlers": {"add": "write", "list": "read"},
}


def manifest_errors(raw):
    with pytest.raises(NeaiError) as e:
        parse_manifest(raw)
    assert e.value.code == "MANIFEST_INVALID"
    return {x["path"]: x["problem"] for x in e.value.details["errors"]}


def test_valid_manifest_parses():
    m = parse_manifest(GOOD)
    assert m.handlers == {"add": "write", "list": "read"}
    assert m.collections["items"]["n"] == Field("integer", required=False)


def test_manifest_reports_every_problem_with_a_path():
    raw = {
        "name": "Bad Name",
        "collections": {"items": {"id": {"type": "string"}, "when": {"type": "date"}}},
        "handlers": {"add": "upsert"},
        "handler": {},
    }
    errors = manifest_errors(raw)
    assert set(errors) == {"name", "collections.items.id", "collections.items.when.type", "handlers.add", "handler"}
    assert "reserved" in errors["collections.items.id"]


def test_manifest_rejects_unknown_field_keys_and_bad_required():
    raw = {**GOOD, "collections": {"items": {"title": {"type": "string", "required": "yes", "unique": True}}}}
    errors = manifest_errors(raw)
    assert set(errors) == {"collections.items.title.required", "collections.items.title.unique"}


@pytest.mark.parametrize("ftype,good,bad", [
    ("string", "x", 1),
    ("integer", 3, 3.5),
    ("integer", 3, True),       # bool is an int in Python; must not pass as an integer
    ("number", 2.5, False),
    ("number", 2, "2"),
    ("number", 1.0, float("nan")),
    ("boolean", True, 1),
])
def test_field_types(ftype, good, bad):
    fields = {"v": Field(ftype)}
    assert validate_record("c", fields, {"v": good}) == {"v": good}
    with pytest.raises(NeaiError) as e:
        validate_record("c", fields, {"v": bad})
    assert e.value.code == "VALIDATION_ERROR"
    assert e.value.details["errors"][0]["field"] == "v"


def test_required_unknown_and_reserved_fields():
    fields = {"a": Field("string"), "b": Field("string", required=False)}
    assert validate_record("c", fields, {"a": "x", "b": None}) == {"a": "x"}
    with pytest.raises(NeaiError) as e:
        validate_record("c", fields, {"b": "y", "zzz": 1, "id": 7})
    problems = {p["field"] for p in e.value.details["errors"]}
    assert problems == {"a", "zzz", "id"}


def test_declared_args_are_checked_before_the_handler_runs(deployed):
    notes = deployed("notes")
    note = ok(notes.call("create_note", "alice", {"title": "t", "body": "b"}))
    e = err(notes.call("create_note", "alice", {"title": "t", "body": 42}))
    assert e["code"] == "ARGS_INVALID" and e["details"]["errors"] == [{"field": "body", "problem": "expected string, got integer"}]
    assert "title (string), body (string, optional)" in e["hint"]
    e = err(notes.call("create_note", "alice", {"titel": "typo"}))
    assert {p["field"] for p in e["details"]["errors"]} == {"titel", "title"}
    assert err(notes.call("get_note", "alice", {"id": "1"}))["code"] == "ARGS_INVALID"
    assert err(notes.call("update_note", "alice", {"id": note["id"], "body": ["x"]}))["code"] == "ARGS_INVALID"
    assert ok(notes.call("get_note", "alice", {"id": note["id"]}))["body"] == "b"
    assert len(ok(notes.call("list_notes", "alice"))) == 1


def test_records_are_validated_on_insert_and_update(deployed, tmp_path):
    manifest = {"name": "raw", "collections": {"items": {"n": {"type": "integer"}, "tags": {"type": "list", "items": "string", "required": False}}},
                "handlers": {"put": "write", "patch": "write", "count": "read"}}
    code = (
        "def put(ctx, args):\n    return ctx.db.insert('items', args['record'])\n\n"
        "def patch(ctx, args):\n    return ctx.db.update('items', args['id'], args['changes'])\n\n"
        "def count(ctx, args):\n    return ctx.db.count('items')\n"
    )
    rt = deployed(write_app(tmp_path / "raw", manifest, code))
    item = ok(rt.call("put", "a", {"record": {"n": 1, "tags": ["x"]}}))
    e = err(rt.call("put", "a", {"record": {"n": True}}))
    assert e["code"] == "VALIDATION_ERROR" and e["details"]["errors"][0]["field"] == "n"
    e = err(rt.call("patch", "a", {"id": item["id"], "changes": {"tags": ["x", 2]}}))
    assert e["code"] == "VALIDATION_ERROR" and "item 1" in e["message"]
    assert ok(rt.call("count", "a")) == 1


@pytest.mark.parametrize("spec,good,bad", [
    ({"type": "list"}, ["a", 1, {"k": None}], "a"),
    ({"type": "list", "items": "string"}, ["a", "b"], ["a", 1]),
    ({"type": "list", "items": "integer"}, [1, 2], [1, True]),
    ({"type": "object"}, {"a": [1, {"b": "c"}]}, ["not", "an", "object"]),
    ({"type": "object"}, {"a": 1}, {"a": float("inf")}),
])
def test_list_and_object_types(spec, good, bad):
    m = parse_manifest({"name": "t", "collections": {"c": {"v": spec}}, "handlers": {"h": "read"}})
    fields = m.collections["c"]
    assert validate_record("c", fields, {"v": good}) == {"v": good}
    with pytest.raises(NeaiError) as e:
        validate_record("c", fields, {"v": bad})
    assert e.value.code == "VALIDATION_ERROR"


def test_long_form_handlers_and_their_errors():
    m = parse_manifest({**GOOD, "handlers": {
        "add": {"kind": "write", "args": {"title": {"type": "string"}, "id": {"type": "integer"}}, "description": "adds"},
        "list": "read"}})
    assert m.handlers == {"add": "write", "list": "read"}
    assert set(m.handler_args["add"]) == {"title", "id"}
    assert m.handler_list()[0]["description"] == "adds"
    assert parse_manifest(m.to_dict()) == m
    errors = manifest_errors({**GOOD, "handlers": {
        "a": {"kind": "upsert"}, "b": {"kind": "read", "args": [1]}, "c": {"kind": "read", "argz": {}},
        "d": {"kind": "read", "args": {"x": {"type": "string", "items": "string"}}}, "e": 7}})
    assert set(errors) == {"handlers.a.kind", "handlers.b.args", "handlers.c.argz", "handlers.d.args.x.items", "handlers.e"}


def test_cannot_filter_on_list_or_object_fields(deployed, tmp_path):
    manifest = {"name": "tagged", "collections": {"items": {"tags": {"type": "list"}}}, "handlers": {"q": "read"}}
    rt = deployed(write_app(tmp_path / "tagged", manifest, "def q(ctx, args):\n    return ctx.db.find('items', where={'tags': []})\n"))
    assert err(rt.call("q", "a"))["code"] == "INVALID_QUERY"
