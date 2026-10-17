import json
import shutil
import socket
import threading
from urllib.parse import urlsplit

import pytest

from neai.deploy import deploy, rollback
from neai.errors import NeaiError
from neai.runtime import Runtime
from neai.schema import parse_manifest

from conftest import APPS, err, ok, write_app

VOTES = {
    "name": "polls",
    "collections": {"votes": {"post_id": {"type": "integer"}, "voter": {"type": "string"},
                              "weight": {"type": "integer", "required": False}}},
    "unique": {"votes": [["post_id", "voter"]]},
    "handlers": {"add": "write", "move": "write", "all": "read"},
}
VOTES_CODE = '''
def add(ctx, args):
    return ctx.db.insert("votes", args)

def move(ctx, args):
    return ctx.db.update("votes", args["id"], {"post_id": args["post_id"]})

def all(ctx, args):
    return ctx.db.find("votes", where=args.get("where"))
'''


def test_unique_constraint_refuses_a_second_matching_record(deployed, tmp_path):
    rt = deployed(write_app(tmp_path / "polls", VOTES, VOTES_CODE))
    first = ok(rt.call("add", "a", {"post_id": 1, "voter": "bob"}))
    e = err(rt.call("add", "a", {"post_id": 1, "voter": "bob"}))
    assert e["code"] == "UNIQUE_VIOLATION"
    assert e["details"] == {"collection": "votes", "fields": ["post_id", "voter"],
                            "values": {"post_id": 1, "voter": "bob"}, "existing_id": first["id"]}
    ok(rt.call("add", "a", {"post_id": 1, "voter": "carol"}))
    other = ok(rt.call("add", "a", {"post_id": 2, "voter": "bob"}))
    assert err(rt.call("move", "a", {"id": other["id"], "post_id": 1}))["code"] == "UNIQUE_VIOLATION"
    assert len(ok(rt.call("all", "a"))) == 3
    levels = {e["message"]: e["level"] for e in rt.store.logs(limit=10)}
    assert levels["move refused: UNIQUE_VIOLATION"] == "warn"  # a refusal, not a bug


def test_adding_a_unique_constraint_that_stored_data_breaks_is_rejected(deployed, tmp_path):
    loose = {**VOTES, "unique": {}}
    src = write_app(tmp_path / "polls", loose, VOTES_CODE)
    rt = deployed(src)
    ok(rt.call("add", "a", {"post_id": 1, "voter": "bob"}))
    ok(rt.call("add", "a", {"post_id": 1, "voter": "bob"}))
    write_app(src, VOTES, VOTES_CODE)
    result = deploy(rt.db.url, src)
    assert result["error"]["code"] == "SCHEMA_INCOMPATIBLE"
    assert result["error"]["details"]["duplicates"] == [
        {"collection": "votes", "fields": ["post_id", "voter"], "values": {"post_id": 1, "voter": "bob"}, "ids": [1, 2]}]


def test_unique_manifest_errors():
    def errors(unique):
        with pytest.raises(NeaiError) as e:
            parse_manifest({**VOTES, "unique": unique})
        return {x["path"]: x["problem"] for x in e.value.details["errors"]}

    assert "no collection" in errors({"nope": [["voter"]]})["unique.nope"]
    assert "unknown field" in errors({"votes": [["voterr"]]})["unique.votes[0]"]
    assert "more than once" in errors({"votes": [["voter", "voter"]]})["unique.votes[0]"]
    assert parse_manifest({**VOTES, "unique": {"votes": ["voter"]}}).unique == {"votes": [("voter",)]}


def test_defaults_fill_inserts_and_existing_records(database, app_copy):
    src = app_copy("notes")
    deploy(database, src)
    rt = Runtime(database, "notes")
    try:
        ok(rt.call("create_note", "alice", {"title": "old"}))
        manifest = json.loads((src / "app.json").read_text())
        # A required field added to a collection that already has data: fine, thanks to the default.
        manifest["collections"]["notes"]["priority"] = {"type": "integer", "default": 3}
        manifest["collections"]["notes"]["tags"] = {"type": "list", "items": "string", "default": []}
        (src / "app.json").write_text(json.dumps(manifest))
        result = deploy(database, src)
        assert result["status"] == "activated", result
        assert result["filled_defaults"] == {"notes.priority": 1, "notes.tags": 1}
        [old] = ok(rt.call("list_notes", "alice"))
        assert (old["priority"], old["tags"]) == (3, [])
        new = ok(rt.call("create_note", "alice", {"title": "new"}))
        assert new["priority"] == 3 and list(new)[:4] == ["id", "owner", "title", "priority"]
    finally:
        rt.close()


def test_default_must_match_the_field_type():
    def errors(spec):
        with pytest.raises(NeaiError) as e:
            parse_manifest({"name": "d", "collections": {"c": {"f": spec}}, "handlers": {"h": "read"}})
        return {x["path"]: x["problem"] for x in e.value.details["errors"]}

    assert "doesn't match" in errors({"type": "integer", "default": "3"})["collections.c.f.default"]
    assert "item 1" in errors({"type": "list", "items": "string", "default": ["a", 2]})["collections.c.f.default"]
    assert "can't be null" in errors({"type": "string", "default": None})["collections.c.f.default"]


def test_handler_args_take_defaults(deployed, tmp_path):
    manifest = {"name": "greet", "collections": {"x": {"y": {"type": "string"}}},
                "handlers": {"hello": {"kind": "read", "args": {"name": {"type": "string", "default": "world"}}}}}
    rt = deployed(write_app(tmp_path / "greet", manifest, "def hello(ctx, args):\n    return 'hi ' + args['name']\n"))
    assert ok(rt.call("hello", "a")) == "hi world"
    assert ok(rt.call("hello", "a", {"name": "bob"})) == "hi bob"


def test_rollback_deploys_an_old_version_again(database, app_copy):
    src = app_copy("notes")
    deploy(database, src)
    original = (src / "handlers.py").read_text()
    (src / "handlers.py").write_text(original.replace('"title must not be blank"', '"give it a title"'))
    assert deploy(database, src)["version"] == 2
    rt = Runtime(database, "notes")
    try:
        assert err(rt.call("create_note", "alice", {"title": " "}))["message"] == "give it a title"
        result = rollback(database, "notes", 1)
        assert (result["status"], result["version"], result["rolled_back_to"]) == ("activated", 3, 1)
        assert err(rt.call("create_note", "alice", {"title": " "}))["message"] == "title must not be blank"
        assert rollback(database, "notes", 1)["status"] == "unchanged"
        with pytest.raises(NeaiError) as e:
            rollback(database, "notes", 9)
        assert e.value.code == "VERSION_NOT_FOUND" and e.value.details["versions"] == [3, 2, 1]
    finally:
        rt.close()


def test_rollback_refuses_to_strand_stored_data(database, app_copy):
    src = app_copy("notes")
    deploy(database, src)
    manifest = json.loads((src / "app.json").read_text())
    manifest["collections"]["notes"]["priority"] = {"type": "integer", "default": 1}
    (src / "app.json").write_text(json.dumps(manifest))
    deploy(database, src)
    rt = Runtime(database, "notes")
    try:
        ok(rt.call("create_note", "alice", {"title": "has a priority"}))
    finally:
        rt.close()
    # Version 1 doesn't know "priority", so going back would leave this record invalid.
    result = rollback(database, "notes", 1)
    assert result["status"] == "rejected" and result["error"]["code"] == "SCHEMA_INCOMPATIBLE"
    assert result["active_version"] == 2


def test_queries_run_in_the_database(deployed, tmp_path):
    rt = deployed(write_app(tmp_path / "polls", VOTES, VOTES_CODE))
    ok(rt.call("add", "a", {"post_id": 1, "voter": "bob", "weight": 2}))
    ok(rt.call("add", "a", {"post_id": 2, "voter": "bob"}))
    ok(rt.call("add", "a", {"post_id": 1, "voter": "amy"}))
    assert [v["id"] for v in ok(rt.call("all", "a", {"where": {"voter": "bob"}}))] == [1, 2]
    assert [v["id"] for v in ok(rt.call("all", "a", {"where": {"weight": None}}))] == [2, 3]
    assert [v["id"] for v in ok(rt.call("all", "a", {"where": {"post_id": 1, "voter": "amy"}}))] == [3]
    assert [v["id"] for v in ok(rt.call("all", "a", {"where": {"id": 2}}))] == [2]


def test_strings_with_nul_are_refused(deployed, tmp_path):
    rt = deployed(write_app(tmp_path / "polls", VOTES, VOTES_CODE))
    e = err(rt.call("add", "a", {"post_id": 1, "voter": "bo\x00b"}))
    assert e["code"] == "VALIDATION_ERROR" and "NUL" in e["message"]


def test_code_runs_from_the_database_not_the_source_folder(database, app_copy):
    src = app_copy("notes")
    deploy(database, src)
    shutil.rmtree(src)
    rt = Runtime(database, "notes")
    try:
        assert ok(rt.call("create_note", "alice", {"title": "no files needed"}))["title"] == "no files needed"
    finally:
        rt.close()


def test_writes_to_one_app_are_serialized_across_connections(database):
    """Two runtimes (two connections, like two servers) racing on check-then-insert."""
    deploy(database, APPS / "feedback")
    setup = Runtime(database, "feedback")
    post = ok(setup.call("create_post", "alice", {"text": "dark mode"}))
    setup.close()

    def vote_many():
        rt = Runtime(database, "feedback")
        try:
            for _ in range(15):
                ok(rt.call("vote", "bob", {"post_id": post["id"]}))
        finally:
            rt.close()

    threads = [threading.Thread(target=vote_many) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check = Runtime(database, "feedback")
    try:
        assert ok(check.call("list_posts", "bob"))[0]["votes"] == 1
    finally:
        check.close()


class Pipe:
    """A TCP pipe to Postgres that a test can cut, to make the database "go away" and come back."""

    def __init__(self, host, port, listen_port=0):
        self.target = (host, port)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", listen_port))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            upstream = socket.create_connection(self.target)
            self.conns += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._copy, args=(a, b), daemon=True).start()

    @staticmethod
    def _copy(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass

    def cut(self):
        self.sock.close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass


def test_calls_fail_clearly_while_the_database_is_away_and_recover_right_after(database):
    deploy(database, APPS / "notes")
    real = urlsplit(database)
    pipe = Pipe(real.hostname, real.port)
    via_pipe = real._replace(netloc=f"{real.username}:{real.password}@127.0.0.1:{pipe.port}").geturl()
    rt = Runtime(via_pipe, "notes")
    try:
        ok(rt.call("create_note", "alice", {"title": "before the outage"}))
        pipe.cut()
        e = err(rt.call("list_notes", "alice"))
        assert e["code"] == "DATABASE_UNAVAILABLE" and "docker compose up" in e["hint"]
        Pipe(real.hostname, real.port, listen_port=pipe.port)  # the database is back
        assert [n["title"] for n in ok(rt.call("list_notes", "alice"))] == ["before the outage"]
    finally:
        rt.close()
