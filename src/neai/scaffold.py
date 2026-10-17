from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import NeaiError
from .paths import check_app_name

HANDLERS_TEMPLATE = '''"""Handlers for the {name} app.

Every handler is declared in app.json as "read" or "write" and is called as fn(ctx, args):
  ctx.caller  the logged-in user's username (None only in "public" handlers called anonymously)
  ctx.user    {{"username": ..., "roles": [...]}}; ctx.has_role("admin") checks a role
  ctx.db      get / find / count / insert / update / delete  (read handlers can't write)
  ctx.log(message, **fields)
Raise AppError(code, message) for expected failures. See AGENTS.md for the full reference.
"""

from neai import AppError


def add_item(ctx, args):
    text = args["text"]  # app.json declares it as a string; the platform checks that first
    if not text.strip():
        raise AppError("INVALID_ARGS", "text must not be blank")
    item = ctx.db.insert("items", {{"owner": ctx.caller, "text": text}})
    ctx.log("item added", id=item["id"])
    return item


def list_items(ctx, args):
    # Scope every query to the caller: handlers are responsible for this, not the platform.
    return ctx.db.find("items", where={{"owner": ctx.caller}})
'''


WORKFLOWS_TEMPLATE = """{
  "workflows": {}
}
"""


TESTS_TEMPLATE = """{
  "tests": [
    {"name": "alice adds an item", "as": "alice", "call": "add_item", "args": {"text": "hello"},
     "save": "item", "expect": {"result": {"owner": "alice", "text": "hello"}}},
    {"name": "alice sees it", "as": "alice", "call": "list_items", "expect": {"result": [{"text": "hello"}]}},
    {"name": "bob doesn't", "as": "bob", "call": "list_items", "expect": {"result": []}},
    {"name": "blank text is refused", "as": "alice", "call": "add_item", "args": {"text": " "},
     "expect": {"error": "INVALID_ARGS"}}
  ]
}
"""


def skeleton_files(name: str) -> tuple[str, str]:
    manifest = {
        "name": name,
        "description": "Skeleton app created by `neai new`; replace with your own.",
        "collections": {"items": {"owner": {"type": "string"}, "text": {"type": "string"}}},
        "handlers": {
            "add_item": {"kind": "write", "args": {"text": {"type": "string"}}},
            "list_items": "read",
        },
    }
    return json.dumps(manifest, indent=2) + "\n", HANDLERS_TEMPLATE.format(name=name)


def new_app(name: str, directory: str | None = None) -> dict[str, Any]:
    check_app_name(name)
    target = Path(directory or name).resolve()
    if target.exists() and any(target.iterdir()):
        raise NeaiError("DIR_NOT_EMPTY", f"{target} already exists and is not empty",
                        hint="choose another folder with --dir")
    target.mkdir(parents=True, exist_ok=True)
    manifest_text, handlers_text = skeleton_files(name)
    (target / "app.json").write_text(manifest_text)
    (target / "handlers.py").write_text(handlers_text)
    # Empty for now: add a workflow when records need to move through steps (see AGENTS.md).
    (target / "workflows.json").write_text(WORKFLOWS_TEMPLATE)
    # Example calls every deploy runs first; keep them passing as the app grows.
    (target / "tests.json").write_text(TESTS_TEMPLATE)
    return {
        "ok": True,
        "status": "created",
        "app": name,
        "dir": str(target),
        "files": [str(target / f) for f in ("app.json", "handlers.py", "workflows.json", "tests.json")],
        "next": [
            f"neai deploy {target}",
            f"neai run {target}            # serves on http://127.0.0.1:8710",
            f"neai signup {name} alice          # in another terminal; asks for a password "
            f"(no terminal? printf '%s\\n' PASSWORD | neai signup {name} alice --password-stdin)",
            f"neai call {name} add_item --as alice --args '{{\"text\": \"hello\"}}'",
            f"neai inspect {name}",
        ],
    }
