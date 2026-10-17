#!/usr/bin/env python3

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from neai.auth import User
from neai.deploy import deploy
from neai.dotenv import load_dotenv
from neai.errors import NeaiError
from neai.runtime import Runtime
from neai.ui import ai, codecheck

from _scratchdb import scratch_database

DEFAULT_REQUEST = ("a shared shopping list: anyone can add items and mark them bought; "
                   "only the person who added an item can delete it")
CHANGE_REQUEST = "add an optional free-text note field to items, settable when adding an item"
MAX_FIXES = 2
TOKENS = {"requests": 0, "total": 0}


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def ask(request: str, current: dict | None = None, error: dict | None = None) -> dict:
    draft = ai.generate(request, current=current, error=error)
    TOKENS["requests"] += 1
    TOKENS["total"] += (draft.get("usage") or {}).get("total_tokens") or 0
    print(f"  model: {draft['model']}  tokens: {(draft.get('usage') or {}).get('total_tokens')}")
    print(f"  summary: {draft['summary']}")
    return draft


def deploy_with_fixes(home: str, src: Path, request: str, draft: dict) -> tuple[dict, dict]:
    for attempt in range(MAX_FIXES + 1):
        files = draft["files"]
        problems = [p for f in ("handlers.py", "workflows.py") if f in files for p in codecheck.check(files[f])]
        if problems:
            print(f"  safety check flagged: {problems}")
            error = {"code": "UNSAFE_CODE", "message": "the code failed the safety check", "details": {"problems": problems}}
        else:
            src.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                (src / name).write_text(text)
            print(f"  files: {', '.join(files)}")
            result = deploy(home, src)
            print(f"  deploy: {result['status']}" + (f" version {result.get('version')}" if result["ok"] else
                                                     f" -> {result['error']['code']}: {result['error']['message']}"))
            for w in result.get("warnings") or []:
                print(f"  warning: {w}")
            if result["ok"]:
                return result, draft
            error = result["error"]
        if attempt == MAX_FIXES:
            break
        print(f"  asking the model to fix it (attempt {attempt + 1} of {MAX_FIXES})")
        draft = ask(request, current=draft["files"], error=error)
    return {"ok": False}, draft


def run_examples(home: str, name: str, examples: list[dict]) -> str:
    rt = Runtime(home, name)
    ok = refused = failed = 0
    try:
        for ex in examples:
            # In-process calls take the user directly (what a login token would resolve to).
            caller = ex.get("caller", "alice")
            user = User(caller, tuple(ex.get("roles") or ())) if isinstance(caller, str) else None
            r = rt.call(ex.get("handler"), user, ex.get("args") or {})
            if r["ok"]:
                ok, tag = ok + 1, "ok     "
            elif r["error"]["kind"] == "app":
                refused, tag = refused + 1, "refused"
            else:
                failed, tag = failed + 1, "ERROR  "
            shown = json.dumps(r["result"] if r["ok"] else r["error"])[:200]
            who = f"{caller}{' ' + str(ex.get('roles')) if ex.get('roles') else ''}" if caller else "anonymous"
            print(f"  {tag} {who}: {ex.get('handler')}({json.dumps(ex.get('args') or {})}) -> {shown}")
    finally:
        rt.close()
    return f"{ok} ok, {refused} refused by the app on purpose, {failed} errors (of {len(examples)} example calls)"


def main(args: list[str], keep: bool) -> int:
    load_dotenv()
    if not ai.settings()["enabled"]:
        if not sys.stdin.isatty():
            print("DEEPSEEK_API_KEY is not set, and this isn't an interactive terminal to ask in.\n"
                  "Run this script directly in a terminal window, or export DEEPSEEK_API_KEY first.")
            return 1
        key = getpass.getpass("DeepSeek API key (input is hidden; paste it and press Enter): ").strip()
        if not key:
            print("No key entered.")
            return 1
        os.environ["DEEPSEEK_API_KEY"] = key
    request = " ".join(args) or DEFAULT_REQUEST
    work = Path(tempfile.mkdtemp(prefix="neai-try-ai-"))
    home, verdicts = scratch_database("neai_try_ai"), []
    try:
        step(f"1. Generate an app: {request!r}")
        draft = ask(request)
        print("  app.json:\n" + "\n".join("    " + line for line in draft["files"]["app.json"].splitlines()))
        result, draft = deploy_with_fixes(home, work / "app", request, draft)
        if not result["ok"]:
            print("\nRESULT: the generated app could not be deployed, even after fixes.")
            return 1
        name = result["app"]
        verdicts.append(f"generated and deployed '{name}' (version {result['version']})")

        step("2. Call it with the model's example calls")
        verdicts.append("first version: " + run_examples(home, name, draft["examples"]))

        step(f"3. Ask for a change: {CHANGE_REQUEST!r}")
        change = ask(CHANGE_REQUEST, current=draft["files"])
        result2, change = deploy_with_fixes(home, work / "app", CHANGE_REQUEST, change)
        if result2.get("status") == "unchanged":
            verdicts.append("the model returned identical files for the change (nothing new to deploy)")
        elif result2["ok"]:
            verdicts.append(f"change deployed as version {result2.get('version')} "
                            f"(schema changed: {result2.get('schema_changed')}; existing data kept)")
            print("  (these run on the data from step 2, so calls that assume an empty app may be refused)")
            verdicts.append("after the change: " + run_examples(home, name, change["examples"]))
        else:
            verdicts.append("the change could not be deployed")

        print("\nRESULT:")
        for v in verdicts:
            print(f"  - {v}")
        print(f"  - {TOKENS['requests']} DeepSeek requests, about {TOKENS['total']} tokens in total")
        print(f"\nThe generated files are in {work / 'app'} (deleted at exit unless you pass --keep).")
        return 0
    except NeaiError as err:
        print(f"\nRESULT: failed with {err.code}: {err.message}" + (f"\n  hint: {err.hint}" if err.hint else ""))
        return 1
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main([a for a in sys.argv[1:] if a != "--keep"], keep="--keep" in sys.argv))
