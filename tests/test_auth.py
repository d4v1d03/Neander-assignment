import json
import os
import stat
import subprocess
import sys

import psycopg
import pytest

from neai.auth import Accounts, hash_password, verify_password
from neai.errors import NeaiError
from neai.schema import parse_manifest

from conftest import APPS, err, ok, write_app

PASSWORD = "correct horse battery"

CLUB = {
    "name": "club",
    "auth": {"signup": "open", "roles": ["staff", "admin"]},
    "collections": {"notices": {"text": {"type": "string"}}},
    "handlers": {
        "read_notices": {"kind": "read", "access": "public"},
        "whoami": "read",
        "post_notice": {"kind": "write", "access": "staff", "args": {"text": {"type": "string"}}},
        "wipe": {"kind": "write", "access": ["admin"]},
        "either": {"kind": "read", "access": ["staff", "admin"]},
    },
}
CLUB_CODE = '''
def read_notices(ctx, args):
    return {"caller": ctx.caller, "notices": ctx.db.find("notices")}

def whoami(ctx, args):
    return {"caller": ctx.caller, "user": ctx.user, "is_staff": ctx.has_role("staff")}

def post_notice(ctx, args):
    return ctx.db.insert("notices", {"text": args["text"]})

def wipe(ctx, args):
    for n in ctx.db.find("notices"):
        ctx.db.delete("notices", n["id"])
    return "wiped"

def either(ctx, args):
    return "ok"
'''


@pytest.fixture
def club(deployed, tmp_path):
    return deployed(write_app(tmp_path / "club", CLUB, CLUB_CODE))


def signup(rt, username, password=PASSWORD):
    response = rt.auth("signup", {"username": username, "password": password})
    assert response["ok"], response
    return response["token"]


def test_passwords_are_salted_scrypt_hashes():
    h1, h2 = hash_password("hunter2hunter2"), hash_password("hunter2hunter2")
    assert h1 != h2 and h1.startswith("scrypt$")
    assert verify_password("hunter2hunter2", h1) and not verify_password("hunter2hunter3", h1)
    assert not verify_password("x", "garbage")


def test_signup_login_me_and_logout(club):
    token = signup(club, "alice")
    assert club.auth("me", token=token) == {"ok": True, "user": {"username": "alice", "roles": []}}
    login = club.auth("login", {"username": "alice", "password": PASSWORD})
    assert login["ok"] and login["token"] != token and login["expires_at"]
    assert club.auth("logout", token=token)["status"] == "logged_out"
    assert club.auth("me", token=token)["error"]["code"] == "AUTH_INVALID"
    assert club.auth("me", token=login["token"])["ok"]  # other sessions are unaffected
    assert club.auth("me")["error"]["code"] == "AUTH_REQUIRED"


def test_the_caller_is_whoever_owns_the_token(club):
    token = signup(club, "alice")
    me = ok(club.call("whoami", token=token))
    assert me == {"caller": "alice", "user": {"username": "alice", "roles": []}, "is_staff": False}
    assert club.store.logs(limit=1)[0]["caller"] == "alice"


def test_tokens_and_passwords_are_never_stored_in_plain_text(club, database):
    token = signup(club, "alice")
    with psycopg.connect(database) as conn:
        dump = json.dumps([conn.execute(f"SELECT * FROM {t}").fetchall()
                           for t in ("neai_users", "neai_sessions", "neai_logs")], default=str)
    assert token not in dump and PASSWORD not in dump


def test_a_token_only_works_for_its_own_app(club, deployed):
    notes = deployed("notes")
    token = signup(club, "alice")
    e = err(notes.call("list_notes", token=token))
    assert e["code"] == "AUTH_INVALID" and "issued by" in e["hint"]


def test_signup_rules(club):
    signup(club, "alice")
    cases = [({"username": "alice", "password": PASSWORD}, "USERNAME_TAKEN"),
             ({"username": "Alice", "password": PASSWORD}, "USERNAME_INVALID"),
             ({"username": "a", "password": PASSWORD}, "USERNAME_INVALID"),
             ({"username": "bob", "password": "short"}, "PASSWORD_TOO_SHORT"),
             ({"username": "bob", "password": 12345678}, "PASSWORD_INVALID"),
             ({"username": "bob"}, "PASSWORD_INVALID")]
    for body, code in cases:
        assert club.auth("signup", body)["error"]["code"] == code, body


def test_login_failures_say_nothing_about_which_part_was_wrong(club):
    signup(club, "alice")
    wrong_password = club.auth("login", {"username": "alice", "password": "not the password"})
    no_such_user = club.auth("login", {"username": "zed", "password": PASSWORD})
    assert wrong_password["error"] == no_such_user["error"] == {
        "code": "LOGIN_FAILED", "kind": "platform", "message": "wrong username or password"}


def test_repeated_wrong_passwords_lock_the_account(club):
    signup(club, "alice")
    for _ in range(5):
        assert club.auth("login", {"username": "alice", "password": "nope nope"})["error"]["code"] == "LOGIN_FAILED"
    locked = club.auth("login", {"username": "alice", "password": PASSWORD})
    assert locked["error"]["code"] == "LOGIN_LOCKED" and "unlock" in locked["error"]["hint"]
    Accounts(club.store).unlock("alice")
    assert club.auth("login", {"username": "alice", "password": PASSWORD})["ok"]


def test_a_successful_login_resets_the_failure_count(club):
    signup(club, "alice")
    for _ in range(4):
        club.auth("login", {"username": "alice", "password": "nope nope"})
    assert club.auth("login", {"username": "alice", "password": PASSWORD})["ok"]
    for _ in range(4):
        club.auth("login", {"username": "alice", "password": "nope nope"})
    assert club.auth("login", {"username": "alice", "password": PASSWORD})["ok"]


def test_access_levels(club):
    alice = signup(club, "alice")
    assert ok(club.call("read_notices"))["caller"] is None  # public: anonymous is fine
    assert ok(club.call("read_notices", token=alice))["caller"] == "alice"  # ... and so is a login
    assert err(club.call("whoami"))["code"] == "AUTH_REQUIRED"
    e = err(club.call("post_notice", args={"text": "hi"}, token=alice))
    assert e["code"] == "FORBIDDEN"
    assert e["details"] == {"handler": "post_notice", "required_roles": ["staff"], "your_roles": []}
    assert "neai users club grant alice staff" in e["hint"]
    assert club.store.logs(limit=1)[0]["level"] == "warn"

    manifest = parse_manifest(club.store.get_version(1)["manifest"])
    Accounts(club.store).grant(manifest, "alice", "staff")
    assert ok(club.call("post_notice", args={"text": "hi"}, token=alice))["text"] == "hi"  # same token, new role
    assert ok(club.call("whoami", token=alice))["is_staff"] is True
    assert ok(club.call("either", token=alice)) == "ok"
    assert err(club.call("wipe", token=alice))["code"] == "FORBIDDEN"
    Accounts(club.store).revoke("alice", "staff")
    assert err(club.call("post_notice", args={"text": "hi"}, token=alice))["code"] == "FORBIDDEN"


def test_operator_accounts_for_apps_with_closed_signup(deployed, tmp_path):
    rt = deployed(write_app(tmp_path / "club", {**CLUB, "auth": {"signup": "closed", "roles": ["staff"]},
                                                "handlers": {"whoami": "read"}}, CLUB_CODE))
    e = rt.auth("signup", {"username": "alice", "password": PASSWORD})["error"]
    assert e["code"] == "SIGNUP_CLOSED" and "neai users club add" in e["hint"]
    manifest = parse_manifest(rt.store.get_version(1)["manifest"])
    accounts = Accounts(rt.store)
    accounts.create_user(manifest, "alice", PASSWORD, roles=["staff"])
    token = rt.auth("login", {"username": "alice", "password": PASSWORD})["token"]
    assert ok(rt.call("whoami", token=token))["user"]["roles"] == ["staff"]
    with pytest.raises(NeaiError) as e:
        accounts.grant(manifest, "alice", "wizard")
    assert e.value.code == "ROLE_UNKNOWN" and e.value.details["declared"] == ["staff"]


def test_new_password_or_removal_ends_every_session(club):
    token = signup(club, "alice")
    accounts = Accounts(club.store)
    assert accounts.set_password("alice", "a brand new password") == 1
    assert club.auth("me", token=token)["error"]["code"] == "AUTH_INVALID"
    token = club.auth("login", {"username": "alice", "password": "a brand new password"})["token"]
    accounts.remove("alice")
    assert club.auth("me", token=token)["error"]["code"] == "AUTH_INVALID"
    with pytest.raises(NeaiError) as e:
        accounts.remove("alice")
    assert e.value.code == "USER_NOT_FOUND"


def test_auth_events_are_logged_without_passwords(club):
    signup(club, "alice")
    club.auth("login", {"username": "alice", "password": "wrong password"})
    entries = [(e["event"], e["message"], e.get("caller")) for e in club.store.logs(limit=2)]
    assert entries == [("auth", "login refused: LOGIN_FAILED", "alice"), ("auth", "signup ok", "alice")]


def test_contract_auth_and_access_errors():
    def errors(**changes):
        with pytest.raises(NeaiError) as e:
            parse_manifest({**CLUB, **changes})
        return {x["path"]: x["problem"] for x in e.value.details["errors"]}

    assert "unknown role" in errors(handlers={"h": {"kind": "read", "access": "wizard"}})["handlers.h.access"]
    assert "must be" in errors(handlers={"h": {"kind": "read", "access": []}})["handlers.h.access"]
    assert "access level" in errors(auth={"roles": ["public"]})["auth.roles[0]"]
    assert "twice" in errors(auth={"roles": ["a", "a"]})["auth.roles[1]"]
    assert "open" in errors(auth={"signup": "invite"})["auth.signup"]
    assert "unknown key" in errors(auth={"sso": True})["auth.sso"]
    m = parse_manifest(CLUB)
    assert m.access_for("read_notices") == "public" and m.access_for("whoami") == "user"
    assert m.access_for("wipe") == ("admin",) and parse_manifest(m.to_dict()) == m
    assert m.to_dict()["handlers"]["whoami"] == "read"  # the default stays short


def test_feedback_reads_are_public_and_deletes_need_a_moderator(deployed):
    fb = deployed("feedback")
    alice = signup(fb, "alice")
    post = ok(fb.call("create_post", args={"text": "dark mode"}, token=alice))
    ok(fb.call("vote", args={"post_id": post["id"]}, token=alice))
    [anon_view] = ok(fb.call("list_posts"))
    assert anon_view["votes"] == 1 and anon_view["voted_by_me"] is False
    assert err(fb.call("delete_post", args={"post_id": post["id"]}, token=alice))["code"] == "FORBIDDEN"
    Accounts(fb.store).grant(parse_manifest(fb.store.get_version(1)["manifest"]), "alice", "moderator")
    assert ok(fb.call("delete_post", args={"post_id": post["id"]}, token=alice)) == {"deleted": post["id"]}
    assert ok(fb.call("list_posts")) == []


def neai(home, *args, stdin=None):
    proc = subprocess.run([sys.executable, "-m", "neai", *args], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "NEAI_HOME": str(home)}, input=stdin)
    return json.loads(proc.stdout), proc.returncode


@pytest.fixture
def notes_server(database):
    proc = subprocess.Popen([sys.executable, "-m", "neai", "run", str(APPS / "notes"), "--port", "0", "--quiet"],
                            stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        proc.stdout.readline()
        yield json.loads(proc.stdout.readline())
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_cli_signup_saves_a_private_login_and_call_uses_it(notes_server, home):
    out, code = neai(home, "signup", "notes", "alice", "--password-stdin", stdin=PASSWORD + "\n")
    assert code == 0 and out["status"] == "signed_up" and "token" not in out
    creds = home / "credentials.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert out["saved_to"] == str(creds)

    out, code = neai(home, "call", "notes", "create_note", "--as", "alice", "--args", '{"title": "via cli"}')
    assert code == 0 and out["result"]["owner"] == "alice"
    assert neai(home, "logins")[0]["logins"] == {"notes": ["alice"]}

    out, _ = neai(home, "login", "notes", "alice", "--password-stdin", "--show-token", stdin="wrong pass\n")
    assert out["error"]["code"] == "LOGIN_FAILED"
    out, _ = neai(home, "login", "notes", "alice", "--password-stdin", "--show-token", stdin=PASSWORD + "\n")
    assert out["status"] == "logged_in" and out["token"].startswith("neai_")

    out, code = neai(home, "logout", "notes", "alice")
    assert code == 0 and out["status"] == "logged_out"
    out, code = neai(home, "call", "notes", "list_notes", "--as", "alice")
    assert out["error"]["code"] == "NOT_LOGGED_IN"
    out, code = neai(home, "call", "notes", "list_notes")
    assert out["error"]["code"] == "AUTH_REQUIRED"


def test_cli_without_a_terminal_asks_for_password_stdin(notes_server, home):
    out, code = neai(home, "login", "notes", "alice")
    assert code == 1 and out["error"]["code"] == "PASSWORD_REQUIRED" and "--password-stdin" in out["error"]["hint"]


def test_cli_users_commands(database, home, tmp_path):
    src = write_app(tmp_path / "club", CLUB, CLUB_CODE)
    neai(home, "deploy", str(src))
    out, code = neai(home, "users", "club", "add", "rita", "--role", "staff", "--password-stdin", stdin=PASSWORD + "\n")
    assert code == 0 and out["user"] == {"username": "rita", "roles": ["staff"]}
    assert neai(home, "users", "club", "grant", "rita", "admin")[0]["user"]["roles"] == ["staff", "admin"]
    assert neai(home, "users", "club", "revoke", "rita", "staff")[0]["user"]["roles"] == ["admin"]
    out, _ = neai(home, "users", "club", "list")
    assert out["roles"] == ["staff", "admin"] and out["signup"] == "open"
    assert [(u["username"], u["roles"]) for u in out["users"]] == [("rita", ["admin"])]
    assert neai(home, "users", "club", "grant", "rita", "wizard")[0]["error"]["code"] == "ROLE_UNKNOWN"
    assert neai(home, "users", "club", "grant", "rita")[0]["error"]["code"] == "USAGE_ERROR"
    assert neai(home, "users", "club", "remove", "rita")[0]["status"] == "removed"
    assert neai(home, "users", "club", "list")[0]["users"] == []
