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

REPO = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neai-demo-"))
ENV = {**os.environ, "NEAI_HOME": str(WORK / "home")}
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


def neai(*args: str, show: bool = True) -> dict:
    print("$ neai " + " ".join(shlex.quote(a) for a in args))
    proc = subprocess.run(NEAI + list(args), capture_output=True, text=True, env=ENV, timeout=60)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"non-JSON output from neai {args}:\n{proc.stdout}\n{proc.stderr}")
    if show:
        print("  " + json.dumps(out))
    return out


def call(app: str, handler: str, caller: str, args: dict | None = None) -> dict:
    return neai("call", app, handler, "--as", caller, "--args", json.dumps(args or {}))


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
    print(f"Working in {WORK} (NEAI_HOME={ENV['NEAI_HOME']})")
    servers: list[Server] = []
    try:
        section("1. Notes: data scoped to the declared caller")
        srv = Server(notes_src); servers.append(srv)
        check(srv.serving["status"] == "serving", "notes is serving version 1")
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

        section("2. Feedback: same runtime, separate data, repeat votes not counted")
        srv = Server(feedback_src); servers.append(srv)
        post = call("feedback", "create_post", "alice", {"text": "please add dark mode"})["result"]
        first = call("feedback", "vote", "bob", {"post_id": post["id"]})["result"]
        again = call("feedback", "vote", "bob", {"post_id": post["id"]})["result"]
        call("feedback", "vote", "carol", {"post_id": post["id"]})
        check(first["counted"] and not again["counted"] and again["votes"] == 1, "bob's second vote isn't counted")
        posts = call("feedback", "list_posts", "dave")["result"]
        check(posts[0]["votes"] == 2 and posts[0]["voted_by_me"] is False, "the post has exactly 2 votes")
        fb = neai("inspect", "feedback", show=False)
        check(set(fb["data"]) == {"posts", "votes"}, "feedback's store holds only posts and votes, no notes")
        check(fb["app"]["store"] != neai("inspect", "notes", show=False)["app"]["store"],
              "each app has its own database file")
        srv.stop()

        section("3. Restart persistence")
        srv = Server(notes_src); servers.append(srv)
        check(srv.deploy["status"] == "unchanged", "restarting with the same code deploys nothing new")
        alice = call("notes", "list_notes", "alice")["result"]
        check(len(alice) == 2, "alice's notes survived the restart")

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

        print(f"\n\033[1;32mAll {CHECKS} checks passed.\033[0m")
    finally:
        for s in servers:
            s.stop() if s.proc.poll() is None else None
        if keep:
            print(f"Kept {WORK}; inspect it with: NEAI_HOME={ENV['NEAI_HOME']} neai inspect notes")
        else:
            shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
