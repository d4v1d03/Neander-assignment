#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _scratchdb import scratch_database

REPO = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neai-demo-"))
ENV = {**os.environ, "NEAI_DATABASE_URL": scratch_database("neai_demo"), "NEAI_HOME": str(WORK / "home")}
PASSWORD = "demo password 123"
NEAI = [sys.executable, "-m", "neai"]
CHECKS = 0


def section(title: str) -> None:
    print(f"\n\033[1m=== {title} ===\033[0m")


def check(condition: bool, what: str) -> None:
    global CHECKS
    if not condition:
        raise SystemExit(f"\nDEMO FAILED: {what}")
    CHECKS += 1
    print(f"  \033[32m✓\033[0m {what}")


def neai(*args: str, show: bool = True, stdin: str | None = None) -> dict:
    print("$ neai " + " ".join(shlex.quote(a) for a in args))
    proc = subprocess.run(NEAI + list(args), capture_output=True, text=True, env=ENV, timeout=60, input=stdin)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"non-JSON output from neai {args}:\n{proc.stdout}\n{proc.stderr}")
    if show:
        print("  " + json.dumps(out))
    return out


def call(app: str, handler: str, caller: str | None, args: dict | None = None) -> dict:
    who = ["--as", caller] if caller else []
    return neai("call", app, handler, *who, "--args", json.dumps(args or {}))


def signup(app: str, *usernames: str) -> dict[str, str]:
    """Create real accounts (v2 has no way to just claim a name); returns each user's token."""
    tokens = {}
    for name in usernames:
        out = neai("signup", app, name, "--password-stdin", "--show-token", stdin=PASSWORD + "\n", show=False)
        if not out.get("ok"):
            raise SystemExit(f"signup failed: {out}")
        print(f"  signed up {name} in {app}; login saved for the CLI")
        tokens[name] = out["token"]
    return tokens


class Server:
    def __init__(self, source: Path):
        print(f"$ neai run {source} --port 0 &")
        self.proc = subprocess.Popen(NEAI + ["run", str(source), "--port", "0", "--quiet"],
                                     stdout=subprocess.PIPE, text=True, env=ENV)
        self.deploy = json.loads(self.proc.stdout.readline())
        print("  " + json.dumps(self.deploy))
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit("server exited before serving")
        self.serving = json.loads(line)
        print("  " + json.dumps(self.serving))

    def stop(self) -> None:
        if self.proc.poll() is None:
            print("$ kill %neai   # stop the server")
            self.proc.terminate()
            self.proc.wait(timeout=10)


class Serve(Server):
    """`neai serve <app>` (or `neai proxy <app>`): no folder, the code comes from the database."""

    def __init__(self, app: str, command: str = "serve"):
        print(f"$ neai {command} {app} --port 0 &")
        self.proc = subprocess.Popen(NEAI + [command, app, "--port", "0", "--quiet"],
                                     stdout=subprocess.PIPE, text=True, env=ENV)
        line = self.proc.stdout.readline()
        if not line:
            raise SystemExit(f"neai {command} exited before serving")
        self.serving = json.loads(line)
        print("  " + json.dumps(self.serving))


def http_call(url: str, handler: str, token: str, args: dict | None = None) -> dict:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{url}/call/{handler}", method="POST",
                                 data=json.dumps({"args": args or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    print(f"$ curl -X POST {url}/call/{handler} -H 'Authorization: Bearer <token>'")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        out = json.loads(e.read())
    print("  " + json.dumps(out))
    return out


def fetch(url: str) -> dict:
    import urllib.request

    print(f"$ curl {url}")
    with urllib.request.urlopen(url, timeout=10) as resp:
        return {"body": resp.read().decode(), "headers": dict(resp.headers)}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not found in {path}"
    path.write_text(text.replace(old, new))
    print(f"  (edited {path.name}: {old.strip()[:50]!r} -> {new.strip()[:50]!r})")


def main() -> None:
    keep = "--keep" in sys.argv
    notes_src, feedback_src = WORK / "notes", WORK / "feedback"
    shutil.copytree(REPO / "apps" / "notes", notes_src)
    shutil.copytree(REPO / "apps" / "feedback", feedback_src)
    print(f"Working in {WORK}; state goes to the scratch database {ENV['NEAI_DATABASE_URL'].rsplit('/', 1)[1]}")
    servers: list[Server] = []
    try:
        section("1. Notes: real accounts, data scoped to the logged-in caller")
        srv = Server(notes_src); servers.append(srv)
        check(srv.serving["status"] == "serving", "notes is serving version 1")
        r = call("notes", "list_notes", None)
        check(r["error"]["code"] == "AUTH_REQUIRED", "without a login, notes refuses to answer")
        notes_tokens = signup("notes", "alice", "bob", "carol")
        groceries = call("notes", "create_note", "alice", {"title": "groceries", "body": "milk, eggs"})["result"]
        call("notes", "create_note", "alice", {"title": "ideas"})
        call("notes", "create_note", "bob", {"title": "bob's plans"})
        alice = call("notes", "list_notes", "alice")["result"]
        check([n["title"] for n in alice] == ["groceries", "ideas"], "alice lists only her own two notes")
        bob = call("notes", "list_notes", "bob")["result"]
        check([n["title"] for n in bob] == ["bob's plans"], "bob lists only his own note")
        r = call("notes", "get_note", "bob", {"id": groceries["id"]})
        check(r["error"]["code"] == "NOTE_NOT_FOUND", "bob can't read alice's note (same error as a missing note)")
        r = call("notes", "update_note", "bob", {"id": groceries["id"], "title": "mine now"})
        check(r["error"]["code"] == "NOTE_NOT_FOUND", "bob can't update alice's note")
        r = call("notes", "create_note", "alice", {"title": "x", "body": 42})
        check(r["error"]["code"] == "ARGS_INVALID" and r["error"]["details"]["errors"][0]["field"] == "body",
              "a body that isn't a string is rejected by the declared args, before the handler runs")
        srv.stop()

        section("2. Feedback: same runtime, separate data and accounts, repeat votes not counted")
        srv = Server(feedback_src); servers.append(srv)
        r = call("feedback", "create_post", "alice", {"text": "x"})
        check(r["error"]["code"] == "NOT_LOGGED_IN", "alice's notes login doesn't work in feedback (accounts are per app)")
        signup("feedback", "alice", "bob", "carol", "dave")
        post = call("feedback", "create_post", "alice", {"text": "please add dark mode"})["result"]
        first = call("feedback", "vote", "bob", {"post_id": post["id"]})["result"]
        again = call("feedback", "vote", "bob", {"post_id": post["id"]})["result"]
        call("feedback", "vote", "carol", {"post_id": post["id"]})
        check(first["counted"] and not again["counted"] and again["votes"] == 1, "bob's second vote isn't counted")
        posts = call("feedback", "list_posts", "dave")["result"]
        check(posts[0]["votes"] == 2 and posts[0]["voted_by_me"] is False, "the post has exactly 2 votes")
        r = call("feedback", "list_posts", None)
        check(r["ok"] and r["result"][0]["votes"] == 2, "list_posts is public: it works without logging in")
        r = call("feedback", "delete_post", "dave", {"post_id": post["id"]})
        check(r["error"]["code"] == "FORBIDDEN", "deleting a post needs the moderator role")
        fb = neai("inspect", "feedback", show=False)
        check(set(fb["data"]) == {"posts", "votes"}, "feedback's store holds only posts and votes, no notes")
        check("posts" not in neai("inspect", "notes", show=False)["data"],
              "apps share the database server, not data: notes sees no posts")
        srv.stop()

        section("3. Restart persistence")
        srv = Server(notes_src); servers.append(srv)
        check(srv.deploy["status"] == "unchanged", "restarting with the same code deploys nothing new")
        alice = call("notes", "list_notes", "alice")["result"]
        check(len(alice) == 2, "alice's notes survived the restart, and so did her login (sessions live in Postgres)")

        section("4. Same-schema code update while the server is running")
        edit(notes_src / "handlers.py",
             '    return ctx.db.find("notes", where={"owner": ctx.caller})',
             '    notes = ctx.db.find("notes", where={"owner": ctx.caller})\n'
             '    term = args.get("contains", "").lower()\n'
             '    return [n for n in notes if term in n["title"].lower()]')
        r = neai("deploy", str(notes_src))
        check(r["status"] == "activated" and r["version"] == 2 and not r["schema_changed"], "version 2 activated")
        r = call("notes", "list_notes", "alice", {"contains": "groc"})
        check(r["version"] == 2 and [n["title"] for n in r["result"]] == ["groceries"],
              "the running server uses the new code on the next call")
        check(len(call("notes", "list_notes", "alice")["result"]) == 2, "data is unchanged by the update")

        section("5. Rejected definitions keep the last valid version serving")
        edit(notes_src / "handlers.py", "def get_note(ctx, args):", "def get_note(ctx, args)")
        r = neai("deploy", str(notes_src))
        check(r["status"] == "rejected" and r["error"]["code"] == "HANDLER_IMPORT_ERROR",
              f"a syntax error is rejected: {r['error']['message']}")
        check(r["active_version"] == 2, "version 2 stays active")
        r = call("notes", "list_notes", "alice")
        check(r["ok"] and r["version"] == 2 and len(r["result"]) == 2, "the server still serves version 2")
        edit(notes_src / "handlers.py", "def get_note(ctx, args)", "def get_note(ctx, args):")

        (notes_src / "tests.json").unlink()
        print("  (removed tests.json, so this step shows the stored-data check on its own; section 9 shows tests)")
        manifest = json.loads((notes_src / "app.json").read_text())
        manifest["collections"]["notes"]["priority"] = {"type": "integer"}
        (notes_src / "app.json").write_text(json.dumps(manifest, indent=2))
        print('  (edited app.json: added a required field "priority" to notes)')
        r = neai("deploy", str(notes_src))
        check(r["error"]["code"] == "SCHEMA_INCOMPATIBLE",
              "adding a required field to a collection that holds data is rejected")
        report = neai("inspect", "notes", show=False)
        print("  inspect -> " + json.dumps({"active_version": report["active_version"],
                                            "latest_rejected": report["latest_rejected"]}))
        check(report["active_version"]["version"] == 2 and
              report["latest_rejected"]["error"]["code"] == "SCHEMA_INCOMPATIBLE",
              "inspect shows the active version and why the latest definition was rejected")
        srv.stop()

        srv = Server(notes_src); servers.append(srv)
        check(srv.deploy["status"] == "rejected" and srv.serving["fallback"] is True and srv.serving["version"] == 2,
              "restarting with the rejected definition falls back to version 2")
        check(len(call("notes", "list_notes", "alice")["result"]) == 2, "and alice's notes are all still there")
        srv.stop()

        section("6. Stateless servers: two copies of notes, one URL in front")
        a, b = Serve("notes"), Serve("notes")
        servers.extend([a, b])
        proxy = Serve("notes", command="proxy")
        servers.append(proxy)
        check(len(neai("ps", "notes")["instances"]) == 2, "neai ps shows two running copies of notes")
        carol = notes_tokens["carol"]
        r = http_call(a.serving["url"], "create_note", carol, {"title": "written through copy A"})
        r = http_call(b.serving["url"], "list_notes", carol)
        check([n["title"] for n in r["result"]] == ["written through copy A"],
              "copy B sees what copy A wrote, and accepts the same login: both live in Postgres")
        a.stop()
        r = http_call(proxy.serving["url"], "list_notes", carol)
        check(r["ok"] and len(r["result"]) == 1, "after killing copy A, the proxy's URL keeps answering (via B)")
        b.stop()
        proxy.stop()

        section("7. Workflows: a five-round hiring process with a skip rule")
        srv = Server(REPO / "apps" / "hiring"); servers.append(srv)
        signup("hiring", "rita", "ivan", "sam", "jo")
        neai("users", "hiring", "grant", "rita", "recruiter")
        neai("users", "hiring", "grant", "ivan", "interviewer")
        job = call("hiring", "post_job", "rita", {"title": "Backend SDE1", "level": "SDE1"})["result"]
        sam = call("hiring", "apply", "sam", {"job_id": job["id"], "experience_years": 4})["result"]
        jo = call("hiring", "apply", "jo", {"job_id": job["id"], "experience_years": 1})["result"]
        check(sam["stage"] == "applied", "a new application starts at the first round, set by the platform")
        r = call("hiring", "pass_round", "ivan", {"id": sam["id"]})
        check(r["error"]["code"] == "WORKFLOW_FORBIDDEN", "an interviewer can't pass the recruiter's round")
        call("hiring", "pass_round", "rita", {"id": sam["id"]})
        r = call("hiring", "pass_round", "rita", {"id": sam["id"]})["result"]
        check(r["skipped"] == ["technical"] and r["to"] == "manager",
              "sam (SDE1, 4 years) skips the technical round: screening -> manager")
        call("hiring", "pass_round", "rita", {"id": jo["id"]})
        r = call("hiring", "pass_round", "rita", {"id": jo["id"]})["result"]
        check(r["skipped"] == [] and r["to"] == "technical", "jo (SDE1, 1 year) goes to the technical round")
        call("hiring", "pass_round", "ivan", {"id": sam["id"]})
        mine = call("hiring", "my_applications", "sam")["result"][0]
        check(mine["stage"] == "hr" and mine["offer"]["salary"] > 0,
              "reaching the hr round ran the prepare_offer hook in workflows.py")
        r = call("hiring", "reject", "ivan", {"id": jo["id"], "note": "needs more experience"})["result"]
        check(r["to"] == "rejected" and r["finished"], "jo is rejected: an exit ends the workflow early")
        moves = call("hiring", "history", "sam", {"id": sam["id"]})["result"]["moves"]
        check([m["to"] for m in moves] == ["applied", "screening", "manager", "hr"],
              "the history records every move, who made it, and what was skipped")

        section("8. A UI for every app")
        page = fetch(srv.serving["url"] + "/")
        check("<title>Hiring</title>" in page["body"] and "Content-Security-Policy" in page["headers"],
              "hiring serves its own ui/index.html at /, with a strict Content-Security-Policy")
        check("window.neai" in fetch(srv.serving["url"] + "/_neai/sdk.js")["body"],
              "every app serves the sdk.js helper its UI uses for login and calls")
        contract = json.loads(fetch(srv.serving["url"] + "/_neai/contract")["body"])
        check([s["name"] for s in contract["workflows"]["hiring"]["steps"]][2] == "technical"
              and "skip_if" not in json.dumps(contract),
              "the public contract shows handlers and rounds, but no code and no rule details")
        srv.stop()
        srv = Server(notes_src); servers.append(srv)
        check("Generated by neai" in fetch(srv.serving["url"] + "/")["body"],
              "notes has no ui/ folder, so it gets a UI generated from its contract")
        srv.stop()

        section("9. tests.json: the app's own examples guard every deploy")
        hiring_src = WORK / "hiring"
        shutil.copytree(REPO / "apps" / "hiring", hiring_src)
        r = neai("validate", str(hiring_src))
        check(r["status"] == "valid" and r["tests"]["passed"] == 14,
              "neai validate runs hiring's 14 tests in a sandbox (deploy runs them too, before going live)")
        edit(hiring_src / "workflows.json", '"experience_years": {">=": 3}', '"experience_years": {">=": 30}')
        r = neai("deploy", str(hiring_src))
        check(r["status"] == "rejected" and r["error"]["code"] == "TESTS_FAILED"
              and "skip the technical round" in r["error"]["message"],
              f"breaking the skip rule is caught: {r['error']['message']}")
        check(r["error"]["details"]["problems"][0] == 'result.to: expected "manager", got "technical"',
              "the failure says what was expected and what happened")
        check(r["active_version"] == neai("inspect", "hiring", show=False)["active_version"]["version"],
              "the working version stays active; the tests never touched real data")

        print(f"\n\033[1;32mAll {CHECKS} checks passed.\033[0m")
    finally:
        for s in servers:
            s.stop() if s.proc.poll() is None else None
        if keep:
            print(f"Kept {WORK}; inspect it with: NEAI_DATABASE_URL={ENV['NEAI_DATABASE_URL']} neai inspect notes")
        else:
            shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
