# Building apps on neai: a guide for coding agents

neai runs small data-backed apps. You write **two files**: a JSON contract and a Python file of handlers. The platform stores the data, validates it, runs the handlers on localhost, and tells you what happened. Every command prints JSON. Every error has a stable `code`, a `message`, usually a `hint` saying how to fix it, and `details`.

Read this file top to bottom once. It is the whole API.

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.9+, macOS or Linux
python -m pip install --upgrade pip                  # old pips can't do the next step
pip install -e ".[test]"                             # run from the repo root
neai --version
```

The platform keeps its state (data, versions, logs) in `$NEAI_HOME`, which defaults to `./.neai` **relative to the current directory**. Run every command from the same directory, or `export NEAI_HOME=/abs/path` once. You can also pass `--home` to any command.

## 2. The workflow

```bash
neai new todo --dir apps/todo        # 1. skeleton: apps/todo/app.json + apps/todo/handlers.py
# 2. edit app.json and handlers.py
neai validate apps/todo              # 3. optional dry run: checks everything, activates nothing
neai run apps/todo --port 8710 &     # 4. deploys if changed, then serves on http://127.0.0.1:8710
neai call todo add_item --as alice --args '{"text": "buy milk"}'
neai inspect todo                    # 5. version, handlers, data, recent logs and errors, last rejection
```

* `neai run` stays in the foreground until stopped, so run it in the background or in another shell. Its **first line** of output is the deploy result. Its **second line** is `{"status": "serving", "url": ..., "pid": ..., "version": ..., "fallback": ...}`. Wait for that line before calling. After that it logs one JSON line per request to stderr; `--quiet` turns that off.
* **One server serves exactly one app.** To run two apps at once, start two servers, with `--port 0` so they don't collide. `neai call <app>` finds the right server by itself.
* Stop a server with Ctrl+C, or `kill <pid>`, where the pid is in `neai inspect <app>` under `server.pid`. Both shut it down cleanly.
* To change code while the server runs, edit the files and run `neai deploy apps/todo`. The running server picks up the new version on the next call. There's no need to restart.
* `neai inspect` and `neai logs` read the store directly, so they work even when the server is down.
* Check `ok` in every response. CLI exit codes: `0` means ok, `1` means error, `2` means bad CLI usage.

## 3. app.json: the contract

```json
{
  "name": "todo",
  "description": "optional free text",
  "collections": {
    "items": {
      "owner": {"type": "string"},
      "text":  {"type": "string"},
      "done":  {"type": "boolean", "required": false},
      "tags":  {"type": "list", "items": "string", "required": false}
    }
  },
  "handlers": {
    "add_item": {
      "kind": "write",
      "args": {"text": {"type": "string"}, "tags": {"type": "list", "items": "string", "required": false}}
    },
    "list_items": "read"
  }
}
```

| Key | Rules |
|---|---|
| `name` | lowercase letters, digits, `_` or `-`, starting with a letter. It is the app's identity: the same name means the same app and the same data. |
| `collections` | an object of collection name to fields. Names are lowercase identifiers (`a-z`, `0-9`, `_`). |
| fields | `{"type": T}` or `{"type": T, "required": false}`. Fields are **required by default**. |
| types | `string`, `integer`, `number` (int or float), `boolean`, `list` and `object`. A list may set `"items"` to a scalar type (`{"type": "list", "items": "string"}`); otherwise lists and objects hold any plain JSON. There is no date type: store a date as an ISO string or an integer timestamp. |
| `id` | **reserved**. Every record gets an integer `id` from the platform on insert. Don't declare it, set it or change it. Ids are unique across *all* collections of an app (not per collection), so they aren't contiguous. The id of an insert that was rolled back may be reused. |
| `handlers` | an object of handler name to either a kind (`"read"` or `"write"`), or `{"kind": ..., "args": {...}, "description": "..."}`. Names must be Python identifiers. |
| `args` | optional. It declares the handler's arguments, using the same syntax as fields. If declared, the platform checks every call **before your handler runs**: wrong types, missing required args and unknown args (typos) all fail with `ARGS_INVALID`, naming the argument. `"id"` is a fine argument name. Without `args`, arguments aren't checked at all. **Declare args for every handler that takes any.** |

The JSON must be strict: no comments and no trailing commas. Unknown keys are rejected, which catches typos such as `"handler"` or `"requird"`.

## 4. handlers.py: the code

```python
from neai import AppError


def add_item(ctx, args):                      # every handler is fn(ctx, args)
    text = args["text"]                       # declared as a string: the platform checked it
    if not text.strip():                      # check only what a type can't express
        raise AppError("INVALID_ARGS", "text must not be blank")
    item = ctx.db.insert("items", {"owner": ctx.caller, "text": text, "tags": args.get("tags")})
    ctx.log("item added", id=item["id"])
    return item                               # must be plain JSON


def list_items(ctx, args):
    return ctx.db.find("items", where={"owner": ctx.caller})
```

### `ctx`

| | |
|---|---|
| `ctx.caller` | the identity the caller declared: a printable string of at most 256 characters, with no leading or trailing spaces. Not authenticated. **Your handlers must scope data to it.** |
| `ctx.db` | the data handle (below). |
| `ctx.log(message, **fields)` | adds a line to the app's logs, visible in `neai inspect` and `neai logs`. It is kept even if the call fails. |
| `ctx.handler`, `ctx.app`, `ctx.version` | the current handler name, app name and active version number. |

`args` is always a dict: whatever the caller sent, or `{}`.
* **With declared args,** it holds only declared arguments of the declared types. Optional ones are absent when not sent, so use `args.get("name")` for those.
* **Without declared args,** validate everything yourself. Remember that in Python `True` is an `int`, so check `isinstance(v, bool)` before accepting an integer.

Either way, check what types can't express in code: blank strings, ranges, allowed values.

Optional fields set to `None` are dropped, so inserting `{"tags": args.get("tags")}` is fine when `tags` is optional.

### `ctx.db`: the entire data API

| Method | Returns | Notes |
|---|---|---|
| `get(coll, id)` | record dict or `None` | `None` also for a non-integer id |
| `find(coll, where=None, limit=None)` | list of records, ordered by id | `where` holds equality filters joined with AND, e.g. `{"owner": ctx.caller, "done": False}` |
| `count(coll, where=None)` | int | same filters as `find` |
| `insert(coll, record)` | the stored record, with `id` | validates the full record |
| `update(coll, id, changes)` | the stored record after the merge | partial update; set an optional field to `None` to remove it; `RECORD_NOT_FOUND` if the id doesn't exist |
| `delete(coll, id)` | `True` if a record was deleted | |

Records come back as plain dicts with `id` first, e.g. `{"id": 3, "owner": "alice", "text": "buy milk"}`. Notes on `find` and `count`:
* Filters only support equality. For anything else (contains, ranges, sorting), `find` and then filter or sort in Python.
* `where` may include `id`. `{"field": None}` matches records where that optional field is missing.
* Filter values must have the field's type. Types are strict: integer fields reject `1.0` and `True`.
* Without `limit`, `find` returns every matching record. With it, `limit` must be between 1 and 10000.
* `get` returns `None` for an id that belongs to a different collection.

### The rules the platform enforces

* **Read handlers can't write.** `insert`, `update` and `delete` in a `"read"` handler fail with `READ_ONLY_VIOLATION`, and the call fails even if you catch that error. Declare a handler `"write"` if it ever writes. `deploy` warns when a read handler calls `ctx.db.insert/update/delete` directly, but a write made through a helper is only caught when it runs, so call every branch at least once.
* **A write handler is all or nothing.** If it raises anything, every write it made is rolled back. You never need to clean up after a failure.
* **Errors from `ctx.db` are exceptions** with `.code`, `.message`, `.hint` and `.details`. If your handler catches one (other than `READ_ONLY_VIOLATION`) and carries on, the call succeeds and keeps its other writes. Prefer checking first (`get`, `count`) over catching.
* **One call runs at a time.** A check followed by an insert in a write handler (e.g. "insert unless it exists") is safe from races.
* **Records must match the schema.** A bad type, a missing required field or an unknown field fails with `VALIDATION_ERROR`, and nothing is written.
* **Handlers have a time limit** (5 s by default, `NEAI_HANDLER_TIMEOUT`). A stuck handler fails with `HANDLER_TIMEOUT` and is rolled back.
* **Return plain JSON**: dict, list, str, int, float, bool or None. Tuples are returned as lists. Anything else (sets, objects, NaN) fails with `HANDLER_BAD_RESULT` and is rolled back.
* `handlers.py` must be **one file** that imports only `neai` and the standard library. Name helper functions `_like_this`; a public function that isn't declared in app.json produces a deploy warning.

## 5. Recipes

**Data private to each caller** (store the owner, filter on it, check it on every lookup by id):

```python
def _own(ctx, coll, record_id):
    rec = ctx.db.get(coll, record_id)
    if rec is None or rec["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", f"no such record: {record_id!r}")   # same answer for "missing" and "not yours"
    return rec
```

**Shared data** (everyone can read): don't filter by caller, but store who created it (`"author": ctx.caller`) and check it before allowing edits.

**A list on a record:**
* **Short list of plain values** (tags, a poll's option texts): use a `list` field, e.g. `{"type": "list", "items": "string"}`.
* **Items that need their own id, their own updates, or per-item counts** (an order's lines, a poll's options that get votes): use a child collection with a parent id and a position, e.g. `options: {poll_id: integer, position: integer, text: string}`. Read it with `find("options", where={"poll_id": poll["id"]})`, then sort by `position`.

`where` filters can't match inside list or object fields.

**An action that may only count once per caller** (votes, likes, RSVPs): store one record per (thing, caller), check before inserting, and return a result rather than an error:

```python
already = ctx.db.count("votes", where={"post_id": post_id, "voter": ctx.caller}) > 0
if not already:
    ctx.db.insert("votes", {"post_id": post_id, "voter": ctx.caller})
return {"counted": not already, "votes": ctx.db.count("votes", where={"post_id": post_id})}
```

**One per caller, where the latest choice wins** (changing a vote): `find` by (thing, caller) with `limit=1`. If there is a record, `update` it; otherwise `insert`.

**Counts and totals**: compute them from the underlying records with `count`/`find` instead of storing a counter that can drift.

**Expected failures**: raise `AppError("SOME_CODE", "message for the caller", hint=None, details=None)`. `hint` (a string) and `details` (a JSON object, e.g. the valid option ids) are optional and are passed through to the caller. Codes must be UPPER_SNAKE_CASE; anything else is a bug that surfaces as `HANDLER_EXCEPTION`. Let unexpected bugs raise normally: they come back as `HANDLER_EXCEPTION` with the failing lines of `handlers.py`.

## 6. Changing an app

* **Code changes** (handlers.py, or adding, removing or renaming handlers): just `neai deploy <dir>`. The data is kept. If the new definition is broken, it is **rejected** and the previous version keeps running.
* **Schema changes** are accepted only if **every stored record is still valid under the new schema**:
  * accepted: adding a collection; adding an optional field; making a field optional; any change while the app has no data;
  * rejected with `SCHEMA_INCOMPATIBLE`: adding a required field, or removing or retyping a field (or a collection) that holds data. There are no migrations. Make the new field optional, or use a new app name to start with empty data.
* **A rejected deploy activates none of its changes.** That includes code fixes bundled with a bad schema change. Ship schema changes separately from code fixes.
* Deploying files identical to the *active* version is a no-op (`"status": "unchanged"`). Going back to the files of an older version creates a new version number.
* After a rejected deploy, `neai inspect <app>` shows `latest_rejected.error`. Fix the problem and deploy again. `latest_rejected` returns to `null` when a later deploy activates a version, or reports `unchanged` because you restored the active files.

## 7. CLI reference

| Command | What it does |
|---|---|
| `neai new <name> [--dir D]` | creates a skeleton app (`./<name>` by default) and prints the next commands |
| `neai validate <dir>` | full validation including the schema-vs-data check; activates nothing, records nothing. `"status"` is `valid` or `rejected` |
| `neai deploy <dir>` | validates, then activates: `"status"` is `activated`, `unchanged` or `rejected` |
| `neai run <dir> [--port N] [--quiet]` | deploys if changed, then serves on `127.0.0.1` (default port 8710; `--port 0` picks a free one). If the deploy is rejected but an older valid version exists, it serves that version (`"fallback": true`). |
| `neai call <app> <handler> --as <caller> [--args JSON] [--url U]` | calls a handler on the running server (discovered automatically) |
| `neai inspect <app> [--records N] [--logs N]` | app identity, active version + hash, handlers (with their declared args), schema, records (last N per collection), recent logs and errors, recent deploys, `latest_rejected`, server status |
| `neai logs <app> [--errors] [--handler H] [--limit N]` | recent calls, `ctx.log` lines, deploys and server events, newest first. `--errors` shows only bugs |

Every log entry has a `level`:
* `info`: the call succeeded.
* `warn`: the call was refused on purpose (your `AppError`) or by a caller mistake (`CALLER_*`, `ARGS_INVALID`, `UNKNOWN_HANDLER`).
* `error`: a bug or a rejected deploy.

`neai inspect` → `recent_errors` lists only `error` entries. `inspect` and `logs` are operator tools: they show every record and every call's arguments, regardless of your app's access rules.

A deploy result looks like this. `warnings` is always present, and empty when there's nothing to warn about:

```json
{"ok": true, "status": "activated", "app": "todo", "version": 2, "hash": "sha256:…", "previous_version": 1, "schema_changed": false, "warnings": []}
```

A call result looks like this:

```json
{"ok": true, "result": {"id": 1, "owner": "alice", "text": "buy milk"}, "version": 2}
```

## 8. HTTP API (what `neai call` uses)

```
POST /call/<handler>      Content-Type: application/json
                          body: {"caller": "alice", "args": {...}}      ("args" is optional; no other keys)
GET  /_neai/health        {"ok": true, "app": "todo", "version": 2}
```

```bash
curl -s -X POST http://127.0.0.1:8710/call/add_item \
     -H 'Content-Type: application/json' \
     -d '{"caller": "alice", "args": {"text": "buy milk"}}'
```

The `Content-Type: application/json` header is required, and the request must be addressed to `127.0.0.1` or `localhost`. HTTP status codes: 200 when ok. 400 for caller mistakes and for every `AppError`, whatever its code. 404 for an unknown handler. 500 for handler bugs: exceptions, `READ_ONLY_VIOLATION`, wrong use of `ctx.db`. 504 for timeouts. Error responses also carry the `version` that answered. Always check the `ok` field.

## 9. Error codes

Errors look like `{"ok": false, "error": {"code", "kind", "message", "hint"?, "details"?}}`. `kind` is `"app"` for an `AppError` raised by your handler, and `"platform"` for everything else.

| Code | Meaning | What to do |
|---|---|---|
| `MANIFEST_INVALID` | app.json is broken. `details.errors` lists every problem with a JSON path. A JSON syntax error is reported alone, with a line and column; other problems show up once the file parses. | fix every listed path, then deploy again |
| `DEFINITION_NOT_FOUND` | the folder lacks `app.json` or `handlers.py` | check the path, or use `neai new` |
| `HANDLER_IMPORT_ERROR` | handlers.py doesn't import: a syntax error or an exception at import time (`details.line`, `details.exception`) | fix that line; only import `neai` and the standard library |
| `HANDLER_MISSING` | a handler declared in app.json isn't defined in handlers.py (`details.missing`) | define it or remove it from app.json |
| `HANDLER_SIGNATURE` | a handler can't be called as `fn(ctx, args)` | use `def name(ctx, args):` |
| `SCHEMA_INCOMPATIBLE` | the new schema rejects stored records (`details.schema_diff`, `details.examples`) | make new fields optional, keep fields that hold data, or use a new app name |
| `VALIDATION_ERROR` | a record doesn't match its collection (`details.errors[].field/problem`) | fix the record your handler builds, or validate `args` first |
| `READ_ONLY_VIOLATION` | a read handler tried to write. `details.line` is the line in handlers.py, even inside a helper | declare the handler `"write"`, or move the write |
| `UNKNOWN_COLLECTION` | the collection isn't declared in this app | add it to app.json, or fix the name |
| `INVALID_QUERY` | a bad `where` field or value type, or a bad `limit` | filter on declared fields, with values of the field's type |
| `RECORD_NOT_FOUND` | `update` on an id that doesn't exist | `get` first, then raise your own `AppError` |
| `UNKNOWN_HANDLER` | there is no such handler (`details.available`) | check the name, and that the latest deploy was `activated` |
| `CALLER_REQUIRED` / `CALLER_INVALID` | the caller is missing (HTTP only; the CLI without `--as` gives `USAGE_ERROR`), or isn't a clean non-empty string | pass `--as <name>` or `"caller"` |
| `ARGS_INVALID` | the arguments don't match the handler's declared `args` (`details.errors` names each one; `hint` lists what the handler takes), or `args` isn't a JSON object, or `--args` isn't valid JSON | send what the hint lists, quoted for the shell |
| `HANDLER_EXCEPTION` | your handler raised an unexpected exception (`details.traceback` shows handlers.py lines); its writes were rolled back | fix the bug, or raise `AppError` for expected cases |
| `HANDLER_TIMEOUT` | the handler ran past the time limit; rolled back | look for an infinite loop |
| `HANDLER_BAD_RESULT` | the return value isn't plain JSON; rolled back | return dicts, lists or scalars |
| `NO_ACTIVE_VERSION` | the app has never been deployed successfully | deploy a valid definition |
| `APP_NOT_FOUND` | no such app in this `NEAI_HOME` (`details.known_apps`); also what `neai call` says for a misspelled app name | check the name and `NEAI_HOME`, or deploy |
| `SERVER_NOT_RUNNING` | `neai call` found no live server | start `neai run <dir>` with the same `NEAI_HOME` |
| `SERVER_ALREADY_RUNNING` / `PORT_IN_USE` | a server for this app, or something else on that port, is already running | reuse it, stop it, or pass `--port 0` |
| `BAD_REQUEST` / `UNSUPPORTED_MEDIA_TYPE` / `FORBIDDEN_HOST` / `BODY_TOO_LARGE` | a malformed HTTP request | send JSON `{"caller", "args"}` with `Content-Type: application/json` to 127.0.0.1, under 1 MiB |
| `USAGE_ERROR` | bad CLI arguments | `neai <command> --help` |
| *your own codes* | an `AppError` raised by your handler (`kind: "app"`) | as your app defines |


