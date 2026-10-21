# Building apps on neai: a guide for coding agents

neai runs small data-backed apps. You write **two files**: a JSON contract and a Python file of handlers. Optionally, add a workflow contract and its Python when records move through steps (section 5), a `ui/` folder for the app's own web page (section 6), and `tests.json`, example calls that every deploy runs first (section 7). The platform stores the data, validates it, handles sign-up and login for the app's users, runs the handlers on localhost, and tells you what happened. Every command prints JSON. Every error has a stable `code`, a `message`, usually a `hint` saying how to fix it, and `details`.

Read this file top to bottom once. It is the whole API.

## 1. Setup

```bash
docker compose up -d                                 # the database; run from the repo root
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+, macOS or Linux
python -m pip install --upgrade pip
pip install -e ".[test]"
neai --version
```

All state (apps, deployed code, data, users, logs, the list of running servers) lives in **Postgres**, found through `NEAI_DATABASE_URL` (or `--db`). The default is the database `docker compose` starts, so you normally set nothing. App servers keep no state of their own: you can stop, restart or copy them freely.

The CLI keeps one thing locally: the logins it saved, in `$NEAI_HOME/credentials.json` (`./.neai` by default, **relative to the current directory**). Run the CLI from the same directory, or `export NEAI_HOME=/abs/path`.

## 2. The workflow

```bash
neai new todo --dir apps/todo        # 1. skeleton: app.json, handlers.py, an empty workflows.json, tests.json
# 2. edit app.json and handlers.py
neai validate apps/todo              # 3. optional dry run: checks everything, activates nothing
neai run apps/todo --port 8710 &     # 4. deploys if changed, then serves on http://127.0.0.1:8710
printf '%s\n' 'alice-password-1' | neai signup todo alice --password-stdin   # 5. a real account
neai call todo add_item --as alice --args '{"text": "buy milk"}'             #    calls as alice
neai inspect todo                    # 6. version, handlers, data, users, logs and errors, last rejection
```

* **Every call is made by a logged-in user** (or anonymously, for handlers declared `"public"`). There is no way to just claim a name. To test as several people, sign up several accounts: `neai signup <app> <username>`. It saves the login token, and `neai call --as <username>` sends it. Passwords must be at least 8 characters. Without a terminal, pipe the password in with `--password-stdin`.
* Accounts belong to **one app**: alice in `todo` and alice in `notes` are different accounts.
* `neai run` stays in the foreground until stopped, so run it in the background or in another shell. Its **first line** of output is the deploy result. Its **second line** is `{"status": "serving", "url": ..., "pid": ..., "version": ..., "fallback": ...}`. Wait for that line before calling. After that it logs one JSON line per request to stderr; `--quiet` turns that off.
* `neai serve <app>` serves the active version straight from the database, with no folder needed. You can run **several servers for the same app** (`--port 0` picks free ports): they share data and logins. `neai ps` lists them, and `neai call <app>` finds one by itself.
* Stop a server with Ctrl+C, or `kill <pid>` (the pid is in `neai ps`). Both shut it down cleanly.
* To change code while the server runs, edit the files and run `neai deploy apps/todo`. Every running server picks up the new version on its next call. There's no need to restart.
* `neai inspect`, `neai logs` and `neai users` talk to the database directly, so they work even when no server is running.
* Check `ok` in every response. CLI exit codes: `0` means ok, `1` means error, `2` means bad CLI usage.

## 3. app.json: the contract

```json
{
  "name": "todo",
  "description": "optional free text",
  "auth": {"signup": "open", "roles": ["admin"]},
  "collections": {
    "items": {
      "owner":    {"type": "string"},
      "text":     {"type": "string"},
      "done":     {"type": "boolean", "default": false},
      "tags":     {"type": "list", "items": "string", "required": false}
    }
  },
  "unique": {"items": [["owner", "text"]]},
  "handlers": {
    "add_item": {
      "kind": "write",
      "args": {"text": {"type": "string"}, "tags": {"type": "list", "items": "string", "required": false}}
    },
    "list_items": "read",
    "stats": {"kind": "read", "access": "public"},
    "purge_done": {"kind": "write", "access": "admin"}
  }
}
```

| Key | Rules |
|---|---|
| `name` | lowercase letters, digits, `_` or `-`, starting with a letter. It is the app's identity: the same name means the same app, the same data and the same accounts. |
| `auth` | optional. `"signup"`: `"open"` (default: anyone can create an account) or `"closed"` (only the operator creates accounts, with `neai users`). `"roles"`: the role names this app uses (lowercase identifiers; `public` and `user` are reserved). |
| `collections` | an object of collection name to fields. Names are lowercase identifiers (`a-z`, `0-9`, `_`). |
| fields | `{"type": T}`, plus optionally `"required": false` and/or `"default": V`. Fields are **required by default**. A `default` fills the field when an insert leaves it out, and is written into existing records when a deploy adds the field. It must match the type, and can't be null. |
| types | `string`, `integer`, `number` (int or float), `boolean`, `list` and `object`. A list may set `"items"` to a scalar type (`{"type": "list", "items": "string"}`); otherwise lists and objects hold any plain JSON. There is no date type: store a date as an ISO string or an integer timestamp. Strings can't contain the NUL character. |
| `id` | **reserved**. Every record gets an integer `id` from the platform on insert. Don't declare it, set it or change it. Ids are unique across *all* collections of an app (not per collection), so they aren't contiguous. |
| `unique` | optional. Per collection, a list of field sets that may not repeat, e.g. `{"votes": [["post_id", "voter"]]}` or `{"users": ["email"]}`. A second record with the same values fails with `UNIQUE_VIOLATION`. Records where one of the fields is missing don't count. Only scalar fields can be unique. |
| `handlers` | an object of handler name to either a kind (`"read"` or `"write"`), or `{"kind": ..., "args": {...}, "access": ..., "description": "..."}`. Names must be Python identifiers. |
| `args` | optional. It declares the handler's arguments, using the same syntax as fields (including `default`). If declared, the platform checks every call **before your handler runs**: wrong types, missing required args and unknown args (typos) all fail with `ARGS_INVALID`, naming the argument. `"id"` is a fine argument name. Without `args`, arguments aren't checked at all. **Declare args for every handler that takes any.** |
| `access` | optional. Who may call the handler, checked by the platform before it runs: `"public"` (anyone, even without logging in), `"user"` (the default: any logged-in user), a role (`"admin"`), or a list of roles (`["staff", "admin"]`: any one is enough). Roles must be declared in `auth.roles`. |

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


def stats(ctx, args):                         # "public": ctx.caller is None when nobody is logged in
    return {"items": ctx.db.count("items")}


def purge_done(ctx, args):                    # "access": "admin": only admins get this far
    done = ctx.db.find("items", where={"done": True})
    for item in done:
        ctx.db.delete("items", item["id"])
    return {"deleted": len(done)}
```

### `ctx`

| | |
|---|---|
| `ctx.caller` | the logged-in user's username, taken by the platform from a verified login token. It can't be faked by the request. It is `None` only in a `"public"` handler called without logging in. **Your handlers must scope private data to it.** |
| `ctx.user` | `{"username": ..., "roles": [...]}`, or `None` when anonymous. |
| `ctx.has_role(name)` | `True` if the user has that role. Use it for checks inside a handler, e.g. "the author or an admin may edit". |
| `ctx.db` | the data handle (below). |
| `ctx.log(message, **fields)` | adds a line to the app's logs, visible in `neai inspect` and `neai logs`. It is kept even if the call fails. |
| `ctx.handler`, `ctx.app`, `ctx.version` | the current handler name, app name and active version number. |

**Never handle passwords or tokens in handlers.** The platform does sign-up and login; handlers only see who is calling.

`args` is always a dict: whatever the caller sent, or `{}`.
* **With declared args,** it holds only declared arguments of the declared types, with defaults filled in. Optional ones without a default are absent when not sent, so use `args.get("name")` for those.
* **Without declared args,** validate everything yourself. Remember that in Python `True` is an `int`, so check `isinstance(v, bool)` before accepting an integer.

Either way, check what types can't express in code: blank strings, ranges, allowed values.

Optional fields set to `None` are dropped, so inserting `{"tags": args.get("tags")}` is fine when `tags` is optional.

### `ctx.db`: the entire data API

| Method | Returns | Notes |
|---|---|---|
| `get(coll, id)` | record dict or `None` | `None` also for a non-integer id |
| `find(coll, where=None, limit=None)` | list of records, ordered by id | `where` holds equality filters joined with AND, e.g. `{"owner": ctx.caller, "done": False}` |
| `count(coll, where=None)` | int | same filters as `find` |
| `insert(coll, record)` | the stored record, with `id` | fills defaults, validates the full record, checks `unique` |
| `update(coll, id, changes)` | the stored record after the merge | partial update; set an optional field to `None` to remove it; `RECORD_NOT_FOUND` if the id doesn't exist |
| `delete(coll, id)` | `True` if a record was deleted | |

Records come back as plain dicts with `id` first, then the fields in the order app.json declares them, e.g. `{"id": 3, "owner": "alice", "text": "buy milk", "done": false}`. Notes on `find` and `count`:
* Filters only support equality, and run in the database. For anything else (contains, ranges, sorting), `find` and then filter or sort in Python.
* `where` may include `id`. `{"field": None}` matches records where that optional field is missing.
* Filter values must have the field's type. Types are strict: integer fields reject `1.0` and `True`.
* Without `limit`, `find` returns every matching record. With it, `limit` must be between 1 and 10000.
* `get` returns `None` for an id that belongs to a different collection.

### The rules the platform enforces

* **Access is checked before your handler runs.** No login for a non-public handler fails with `AUTH_REQUIRED`; a missing role fails with `FORBIDDEN`. Your handler never sees those calls.
* **Read handlers can't write.** `insert`, `update` and `delete` in a `"read"` handler fail with `READ_ONLY_VIOLATION`, and the call fails even if you catch that error. Declare a handler `"write"` if it ever writes. `deploy` warns when a read handler calls `ctx.db.insert/update/delete` directly, but a write made through a helper is only caught when it runs, so call every branch at least once.
* **A write handler is all or nothing.** If it raises anything, every write it made is rolled back. You never need to clean up after a failure.
* **Errors from `ctx.db` are exceptions** with `.code`, `.message`, `.hint` and `.details`. If your handler catches one (other than `READ_ONLY_VIOLATION`) and carries on, the call succeeds and keeps its other writes. Prefer checking first (`get`, `count`) over catching.
* **Calls run at the same time.** A server handles several requests at once, on threads, and several servers can share one app. So keep no state in Python globals: a module-level counter would lose updates, and each server would have its own. Anything that must last goes in `ctx.db`.
* **Writes to one app happen one at a time,** across every thread and every server (the platform takes a per-app lock in the database). A check followed by an insert in a write handler (e.g. "insert unless it exists") is safe from races. Read handlers don't wait for writes.
* **Records must match the schema.** A bad type, a missing required field or an unknown field fails with `VALIDATION_ERROR`, and nothing is written.
* **Handlers have a time limit** (5 s by default, `NEAI_HANDLER_TIMEOUT`). A stuck handler fails with `HANDLER_TIMEOUT` and is rolled back. Don't `time.sleep` in a handler: on a server, a sleep is only stopped when it ends, though Postgres ends its transaction half a second past the limit, so it can't hold up other writes.
* **Return plain JSON**: dict, list, str, int, float, bool or None. Tuples are returned as lists. Anything else (sets, objects, NaN) fails with `HANDLER_BAD_RESULT` and is rolled back.
* `handlers.py` must be **one file** that imports only `neai` and the standard library. Name helper functions `_like_this`; a public function that isn't declared in app.json produces a deploy warning.

## 5. Workflows: records that move through steps (optional)

Use a workflow when a record goes through a **process**: an ordered list of steps, where certain people may move it on, some steps are skipped for some records, and something should happen when a record reaches a step. For example:

| Process | Steps | A rule |
|---|---|---|
| expense claims | manager review → finance review → payout → paid; or rejected | claims under 100 skip finance review |
| orders | placed → packed → shipped → delivered; or cancelled | digital orders skip packing and shipping |
| support tickets | new → triage → in progress → resolved | urgent tickets skip triage |
| publishing | draft → review → legal → published | only contracts go through legal |
| job applications | five interview rounds → hired; or rejected | experienced candidates skip a round (see `apps/hiring`) |

Describe the process in **`workflows.json`** instead of coding it as `if`s on a status field. The platform keeps the record's current step in one of its fields, enforces who may move it, applies the skip rules, runs hooks and keeps a history. Here is the expense-claims example:

```json
{
  "workflows": {
    "approval": {
      "collection": "claims",
      "field": "step",
      "steps": [
        {"name": "manager_review", "by": "manager"},
        {"name": "finance_review", "by": "finance", "skip_if": {"amount": {"<": 100}}},
        {"name": "payout", "by": "finance", "on_enter": "create_payout"},
        "paid"
      ],
      "exits": ["rejected", "withdrawn"]
    }
  }
}
```

It needs, in app.json, `"auth": {"roles": ["manager", "finance"]}` and a collection `claims` with an `amount` (number) and a `step` field (`{"type": "string", "required": false}`).

| Key | Rules |
|---|---|
| `collection` | a collection in app.json. One workflow per collection. |
| `field` | the field holding the step: declare it in app.json as `{"type": "string", "required": false}`, with no default. **Only the platform writes it**: an insert or update that sets it fails with `WORKFLOW_FIELD_MANAGED`. |
| `steps` | at least two, in order. A step is a name or `{"name", "by"?, "skip_if"?, "on_enter"?}`. New records start at the first step; the **last step is where they finish** (it can't have `skip_if`). |
| `by` | a role (or list of roles, from `auth.roles`) that may move a record **out of** this step. Leave `by` out to let anyone the handler allows move it on, e.g. "the employee who owns the claim sends it": check ownership in the handler. (`user` and `public` are access levels, not roles.) |
| `skip_if` | a rule: when it holds as the record **reaches** the step, the step is skipped (and so on, for the steps after it). |
| `on_enter` | a function in `workflows.py`, run as `fn(ctx, record)` when a record lands on the step (not when it's skipped). |
| `exits` | other ways to finish early, e.g. `rejected`. Exits have no `by`: any handler that may call `ctx.workflow.exit` can use them, so check permissions in the handler (`ctx.workflow.state()` tells you who may pass the current step). |

**Rules** (`skip_if`) are JSON, checked against the collection's fields at deploy:
* `{"priority": "urgent"}` is equality; several keys must all hold: `{"kind": "digital", "country": "IN"}`.
* Operators: `{"amount": {"<": 100}}`, with `==`, `!=`, `>`, `>=`, `<`, `<=`, `in`, `not_in` (the last two take a list: `{"type": {"in": ["contract", "nda"]}}`). A missing (optional) field equals nothing, so only `!=` and `not_in` hold for it.
* Combine with `{"all": [...]}`, `{"any": [...]}` and `{"not": {...}}`.
* For logic JSON can't express (anything that needs other records), `{"function": "has_open_claims"}` calls `def has_open_claims(ctx, record)` in `workflows.py`. It must return `True` or `False`, and it gets a **read-only** `ctx` (it may `find`/`count`, not write or move records).

**`workflows.py`** holds the hooks and condition functions the contract names. It may import only `neai` and the standard library, like handlers.py. Name helpers `_like_this`.

```python
def create_payout(ctx, claim):             # on_enter of "payout": the same ctx as the handler that moved it
    ctx.db.insert("payouts", {"claim_id": claim["id"], "employee": claim["employee"], "amount": claim["amount"]})
```

**Moving records, from a write handler** (`ctx.workflow`):

| Call | Does |
|---|---|
| `ctx.workflow.advance(workflow, id, note=None)` | passes the current step (checking its `by`), moves to the next step, skipping steps whose rule holds, then runs the landing step's `on_enter`. Returns `{"workflow", "id", "from", "to", "skipped": [...], "finished", "record"}` |
| `ctx.workflow.exit(workflow, id, to, note=None)` | finishes early at one of the `exits`. Returns the same shape as `advance` |
| `ctx.workflow.state(workflow, id)` | `{"workflow", "id", "step", "finished", "by": [roles that may pass the current step], "can_advance": bool (may this caller?), "steps": [names], "exits": [names]}` (fine in read handlers) |
| `ctx.workflow.history(workflow, id)` | every move, oldest first: `{"at", "action": "start"/"advance"/"exit", "from", "to", "skipped": [...], "by": "<username who made the move>", "note"}`; `from` and `note` are left out when empty (fine in read handlers) |

```python
def approve(ctx, args):                    # "access": ["manager", "finance"]; each step's "by" decides who passes it
    claim = ctx.db.get("claims", args["id"])
    if claim is None:
        raise AppError("CLAIM_NOT_FOUND", f"no claim with id {args['id']!r}")
    return ctx.workflow.advance("approval", claim["id"], note=args.get("note"))
```

A claim of 50 goes `manager_review` → `payout` (`"skipped": ["finance_review"]`); a claim of 500 goes through `finance_review`. Every move is in `ctx.workflow.history`.

**What the platform guarantees:**
* A move, its skips, its hook's writes and its history entry are one transaction with the rest of the handler: all or nothing. A failing hook undoes the move.
* `advance` and `exit` on a finished record fail with `WORKFLOW_FINISHED`; a caller without the step's `by` role gets `WORKFLOW_FORBIDDEN`. Both are refusals (`warn` in logs).
* Read handlers can't move records (`READ_ONLY_VIOLATION`).
* A hook may advance the record again, but a call stops after 50 moves (`WORKFLOW_LOOP`).
* Inserting a record starts it at the first step, applying skip rules there too, and writes a `start` entry to the history.

**Deploy warns** when the code itself shows a workflow mistake: a handler setting the step field, `ctx.workflow.*` naming a workflow that doesn't exist, or `exit` naming an undeclared exit. Treat every deploy warning as a bug: each one names a call that will fail.

**Changing a workflow:**
* Adding a workflow to a collection that already has records: they join it at the first step.
* Removing or renaming a step is rejected with `WORKFLOW_INCOMPATIBLE` while any record is in that step (`details.examples`). Move those records first.
* Changing `by`, `skip_if` or `on_enter` affects only future moves.

## 6. A UI for the app (optional)

Every running app server is also a website, at its own URL (e.g. `http://127.0.0.1:8710/`):
* **Without a `ui/` folder,** `/` shows a UI generated from the contract: a login box, one form per handler (from its declared `args`) and results as tables. Actions the user may not call are listed separately with the reason. This page is always at `/_neai/ui` too.
* **With `ui/index.html`,** `/` serves it instead. Other files in `ui/` are served by name: `ui/style.css` is `/style.css`, `ui/js/app.js` is `/js/app.js`.

`ui/` is part of the app's files: it is deployed, versioned and rolled back with app.json and handlers.py. A deploy changes the UI of every running server on its next request.

**Rules for `ui/`:**
* Text files only: `.html`, `.css`, `.js`, `.json`, `.svg`, `.txt`, `.md` (inline small images as SVG or `data:` URLs).
* At most 50 files and 2 MB in total, and at most 3 folders deep. Names use letters, digits, `.`, `_` and `-`. Hidden files (`.DS_Store`) are ignored.
* Anything else is `UI_INVALID`. Deploy also warns when the UI calls `neai.call("x")` for a handler `x` that app.json doesn't declare.

**Pages are plain HTML, CSS and JavaScript**, with no build step. They are served with a strict Content-Security-Policy: scripts and styles only from the app itself (inline is fine), requests only to the app's own server, no frames, no plugins. **Scripts, fonts or images from other sites are blocked.**

**Talk to the app through `/_neai/sdk.js`**, never with raw `fetch`. It handles login tokens; each app has its own origin, so one app's page can't see another's login.

```html
<script src="/_neai/sdk.js"></script>
<script>
  // await neai.contract()                 {app, description, auth, handlers: [{name, kind, access, args}], workflows}
  // await neai.me()                       {username, roles}, or null when nobody is logged in
  // await neai.signup(username, password) / await neai.login(username, password) / await neai.logout()
  // await neai.call("create_note", {title: "hi"})   returns the handler's result;
  //                                                  a failure throws an error with .code, .message, .hint
  // neai.onAuthChange(fn)                 run fn after login, logout, or an expired login
  async function render() {
    const user = await neai.me();
    const list = document.getElementById("notes");
    list.replaceChildren();
    if (!user) return;
    for (const note of await neai.call("list_notes")) {
      const li = document.createElement("li");
      li.textContent = note.title;            // textContent, never innerHTML: data comes from users
      list.append(li);
    }
  }
  neai.onAuthChange(render);
  render();
</script>
```

* Build the page with `document.createElement` and `textContent`, never `innerHTML` with data.
* Show a login (and, if `auth.signup` is `"open"`, sign-up) form when `neai.me()` is `null`.
* Only offer actions the user may call (handler `access` vs the user's roles), and show `error.message` when a call fails. The platform enforces access anyway; the UI should just not invite errors.
* `apps/hiring/ui/` is a complete example: candidates see a tracker of their rounds (including skipped ones) and their offer; recruiters and interviewers get a pipeline board where Pass is only enabled for the rounds their role may pass.

## 7. Example tests: tests.json (optional, recommended)

`tests.json` lists calls and what they should return. **Every deploy and every `neai validate` runs them first**, against an empty scratch copy of the app (a sandbox), with the new version's code, inside a database transaction that is always rolled back. Real data is never touched. If a test fails, the deploy is rejected with `TESTS_FAILED`, and the active version keeps serving.

```json
{
  "tests": [
    {"name": "alice creates a note", "as": "alice", "call": "create_note",
     "args": {"title": "groceries"}, "save": "note", "expect": {"result": {"owner": "alice"}}},
    {"name": "bob can't read it", "as": "bob", "call": "get_note",
     "args": {"id": "$note.id"}, "expect": {"error": "NOTE_NOT_FOUND"}},
    {"as": "alice", "call": "list_notes", "expect": {"result": [{"title": "groceries"}]}},
    {"name": "anonymous callers are refused", "call": "list_notes", "expect": {"error": "AUTH_REQUIRED"}}
  ]
}
```

| Key | Meaning |
|---|---|
| `call`, `args` | the handler and its arguments (checked against app.json at deploy) |
| `as` | a username (no password needed: test users exist only during the run), `{"user": ..., "roles": [...]}` (roles from `auth.roles`; they stick to that user for the rest of the run), or leave it out for an anonymous call |
| `save` | keep this call's result under a name; later tests refer to it as `"$note.id"` (or `"$note.tags.0"`) anywhere in `args` or `expect` |
| `expect` | `{"result": ...}`: the call succeeds and its result matches; `{"error": "CODE"}`: it fails with that code; nothing: it just succeeds; `{"ok": false}`: any failure |
| `name` | optional; shown when the test fails |

**Matching:** an expected object matches if each key it lists matches (extra keys in the result are fine); lists must have the same length and match item by item; numbers compare by value; booleans are strict.

**Tests run in order**, each seeing what the earlier ones wrote, starting from an empty app: ids start at 1. Errors name a test by its position, counting from 0, the same way in `TESTS_INVALID` and `TESTS_FAILED`: `tests[6]`. `args` are checked against the handler's declared args at deploy (unless the test expects `ARGS_INVALID`). On failure, `details` has `problems` (e.g. `result.to: expected "payout", got "finance_review"`), the `response`, the resolved `args`, and `passed_before`.

**Write them for:** the main path, every refusal that matters (a user who may not do something, a step someone may not pass), and every rule (`skip_if`, unique, ownership): for a skip rule, one record that skips the step and one that doesn't. A rule change that breaks a promise then fails the deploy instead of shipping. `apps/hiring/tests.json` covers a whole workflow this way.

## 8. Recipes

**Data private to each user** (store the owner, filter on it, check it on every lookup by id):

```python
def _own(ctx, coll, record_id):
    rec = ctx.db.get(coll, record_id)
    if rec is None or rec["owner"] != ctx.caller:
        raise AppError("NOT_FOUND", f"no such record: {record_id!r}")   # same answer for "missing" and "not yours"
    return rec
```

**Shared data** (everyone can read): don't filter by caller, but store who created it (`"author": ctx.caller`) and check it before allowing edits. If people who aren't logged in may read it, declare the read handler `"access": "public"` and remember `ctx.caller` can be `None` there.

**Privileged actions** (moderators, admins, staff): declare the role in `auth.roles` and put it in the handler's `access`. The platform refuses everyone else. For "the owner **or** an admin", leave `access` at `"user"` and check in code: `if rec["author"] != ctx.caller and not ctx.has_role("admin"): raise AppError(...)`. The operator grants roles with `neai users <app> grant <username> <role>`. Roles are read on every call, so a grant or revoke applies to existing logins at once.

**A list on a record:**
* **Short list of plain values** (tags, a poll's option texts): use a `list` field, e.g. `{"type": "list", "items": "string"}`.
* **Items that need their own id, their own updates, or per-item counts** (an order's lines, a poll's options that get votes): use a child collection with a parent id and a position, e.g. `options: {poll_id: integer, position: integer, text: string}`. Read it with `find("options", where={"poll_id": poll["id"]})`, then sort by `position`.

`where` filters can't match inside list or object fields.

**An action that may only count once per user** (votes, likes, RSVPs): store one record per (thing, user), declare that pair in `unique`, check before inserting, and return a result rather than an error:

```python
already = ctx.db.count("votes", where={"post_id": post_id, "voter": ctx.caller}) > 0
if not already:
    ctx.db.insert("votes", {"post_id": post_id, "voter": ctx.caller})
return {"counted": not already, "votes": ctx.db.count("votes", where={"post_id": post_id})}
```

The check gives a friendly result; `unique` guarantees it even if a later version of the code forgets the check.

**One per user, where the latest choice wins** (changing a vote): `find` by (thing, user) with `limit=1`. If there is a record, `update` it; otherwise `insert`.

**Counts and totals**: compute them from the underlying records with `count`/`find` instead of storing a counter that can drift.

**Expected failures**: raise `AppError("SOME_CODE", "message for the caller", hint=None, details=None)`. `hint` (a string) and `details` (a JSON object, e.g. the valid option ids) are optional and are passed through to the caller. Codes must be UPPER_SNAKE_CASE; anything else is a bug that surfaces as `HANDLER_EXCEPTION`. Let unexpected bugs raise normally: they come back as `HANDLER_EXCEPTION` with the failing lines of `handlers.py`.

## 9. Changing an app

* **Code changes** (handlers.py, adding, removing or renaming handlers, changing `access`, `auth` or `args`): just `neai deploy <dir>`. The data and the accounts are kept. If the new definition is broken, it is **rejected** and the previous version keeps running.
* **Schema changes** (collections, fields, `unique`) are accepted only if **every stored record is still valid under the new schema**:
  * accepted: adding a collection; adding an optional field; adding a field **with a `default`** (required or not; existing records get the default); making a field optional; any change while the app has no data;
  * rejected with `SCHEMA_INCOMPATIBLE`: adding a required field without a default; removing or retyping a field (or a collection) that holds data; adding a `unique` rule that stored records already break (`details.duplicates` lists them). There are no migrations. Give the new field a default, make it optional, or use a new app name to start with empty data.
* **A rejected deploy activates none of its changes.** That includes code fixes bundled with a bad schema change. Ship schema changes separately from code fixes.
* Deploying files identical to the *active* version is a no-op (`"status": "unchanged"`).
* **Going back:** `neai versions <app>` lists versions; `neai rollback <app> --to N` deploys version N's files again as a new version, with the same checks (it is rejected if stored data no longer fits version N's schema).
* After a rejected deploy, `neai inspect <app>` shows `latest_rejected.error`. Fix the problem and deploy again. `latest_rejected` returns to `null` when a later deploy activates a version, or reports `unchanged` because you restored the active files.

## 10. CLI reference

| Command | What it does |
|---|---|
| `neai new <name> [--dir D]` | creates a skeleton app (`./<name>` by default) and prints the next commands |
| `neai validate <dir>` | full validation including tests.json and the schema-vs-data check; activates nothing, records nothing. `"status"` is `valid` or `rejected`; `"tests"` says how many passed |
| `neai deploy <dir>` | validates (contract, code, tests, then stored data), then activates: `"status"` is `activated`, `unchanged` or `rejected` |
| `neai run <dir> [--port N] [--quiet]` | deploys if changed, then serves on `127.0.0.1` (default port 8710; `--port 0` picks a free one). If the deploy is rejected but an older valid version exists, it serves that version (`"fallback": true`). |
| `neai serve <app> [--port N] [--quiet]` | serves the active version from the database; run as many as you like. Each handles up to `NEAI_THREADS` requests at once (default 16), sharing `NEAI_DB_POOL_SIZE` database connections (default 10) |
| `neai ps [app]` | running servers: app, url, pid |
| `neai proxy <app> [--port N]` | one URL in front of every running server of the app (round-robin; skips dead ones) |
| `neai signup <app> <username> [--password-stdin] [--show-token]` | creates an account through the running server and saves its login |
| `neai login <app> <username> [--password-stdin] [--show-token]` | logs in and saves the login (`--show-token` also prints the token, e.g. for curl) |
| `neai logout <app> <username>` / `neai logins` | ends a saved login / lists saved logins |
| `neai call <app> <handler> [--as <username>] [--args JSON] [--token T] [--url U]` | calls a handler as that user (their saved login). Leave out `--as` for a public handler |
| `neai users <app> list\|add\|remove\|grant\|revoke\|passwd\|unlock [username] [role]` | the operator's account tools: e.g. `neai users todo add rita --role admin`, `neai users todo grant alice admin`. Changing a password logs that user out everywhere |
| `neai inspect <app> [--records N] [--logs N]` | app identity, active version + hash, versions, handlers (with args and access), schema, accounts summary, workflows (with how many records are in each step), records (last N per collection), recent logs and errors, recent deploys, `latest_rejected`, running servers |
| `neai logs <app> [--errors] [--handler H] [--limit N]` | recent calls, sign-ins, `ctx.log` lines, deploys and server events, newest first. `--errors` shows only bugs |
| `neai versions <app>` / `neai rollback <app> --to N` | lists versions / deploys version N again |

Every log entry has a `level`:
* `info`: the call succeeded.
* `warn`: the call was refused on purpose (your `AppError`), by an access rule (`AUTH_*`, `FORBIDDEN`), by a `unique` rule, or by a caller mistake (`ARGS_INVALID`, `UNKNOWN_HANDLER`). Failed logins are `warn` too.
* `error`: a bug or a rejected deploy.

`neai inspect` → `recent_errors` lists only `error` entries. `inspect`, `logs` and `users` are operator tools: they show every record and every call's arguments (never passwords or tokens), regardless of your app's access rules.

A deploy result looks like this. `warnings` is always present, and empty when there's nothing to warn about:

```json
{"ok": true, "status": "activated", "app": "todo", "version": 2, "hash": "sha256:…", "previous_version": 1, "schema_changed": false, "warnings": []}
```

A call result looks like this:

```json
{"ok": true, "result": {"id": 1, "owner": "alice", "text": "buy milk", "done": false}, "version": 2}
```

## 11. HTTP API (what `neai call`, `neai login` and UIs use)

```
POST /auth/signup         body: {"username": "alice", "password": "..."}  -> {"ok", "user", "token", "expires_at"}
POST /auth/login          body: {"username": "alice", "password": "..."}  -> {"ok", "user", "token", "expires_at"}
POST /auth/logout         header: Authorization: Bearer <token>
GET  /auth/me             header: Authorization: Bearer <token>            -> {"ok", "user": {"username", "roles"}}
POST /call/<handler>      header: Authorization: Bearer <token>  (leave it out for a public handler)
                          body: {"args": {...}}      ("args" is optional; no other keys)
GET  /_neai/health        {"ok": true, "app": "todo", "version": 2, "instance": "...", "pid": ...}
GET  /_neai/contract      the public contract (handlers with args and access, auth, workflow steps); no code
GET  /_neai/sdk.js        the helper for UIs
GET  /                    the app's UI: ui/index.html, or the generated one (also at /_neai/ui)
GET  /<file>              ui/<file>
```

```bash
TOKEN=$(printf '%s\n' 'alice-password-1' | neai login todo alice --password-stdin --show-token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -X POST http://127.0.0.1:8710/call/add_item \
     -H 'Content-Type: application/json' -H "Authorization: Bearer $TOKEN" \
     -d '{"args": {"text": "buy milk"}}'
```

* The body never names the caller: the platform takes it from the token (a body with `"caller"` is refused).
* A token works on every server of its app (sessions are in the database), lasts 30 days, and ends at logout, a password change or account removal. A token from one app doesn't work in another.
* Five wrong passwords in a row lock the account for 15 minutes (`LOGIN_LOCKED`; the operator can `neai users <app> unlock <username>`).
* The `Content-Type: application/json` header is required, and the request must be addressed to `127.0.0.1` or `localhost`.
* HTTP status codes: 200 when ok. 400 for caller mistakes and for every `AppError`, whatever its code. 401 for a missing or bad login, 403 for a missing role, 404 for an unknown handler, 409 for `UNIQUE_VIOLATION`, 429 for a locked account. 500 for handler bugs: exceptions, `READ_ONLY_VIOLATION`, wrong use of `ctx.db`. 503 when the database is unreachable, 504 for timeouts. Error responses also carry the `version` that answered. Always check the `ok` field.

## 12. Error codes

Errors look like `{"ok": false, "error": {"code", "kind", "message", "hint"?, "details"?}}`. `kind` is `"app"` for an `AppError` raised by your handler, and `"platform"` for everything else.

| Code | Meaning | What to do |
|---|---|---|
| `MANIFEST_INVALID` | app.json is broken. `details.errors` lists every problem with a JSON path. A JSON syntax error is reported alone, with a line and column; other problems show up once the file parses. | fix every listed path, then deploy again |
| `DEFINITION_NOT_FOUND` / `DEFINITION_INVALID` | the folder lacks `app.json` or `handlers.py`, or a file isn't UTF-8 text | check the path, or use `neai new` |
| `HANDLER_IMPORT_ERROR` | handlers.py doesn't import: a syntax error or an exception at import time (`details.line`, `details.exception`) | fix that line; only import `neai` and the standard library |
| `HANDLER_MISSING` | a handler declared in app.json isn't defined in handlers.py (`details.missing`) | define it or remove it from app.json |
| `HANDLER_SIGNATURE` | a handler can't be called as `fn(ctx, args)` | use `def name(ctx, args):` |
| `SCHEMA_INCOMPATIBLE` | the new schema rejects stored records (`details.schema_diff`, `details.examples`, `details.duplicates`) | give new fields a default or make them optional, keep fields that hold data, or use a new app name |
| `VALIDATION_ERROR` | a record doesn't match its collection (`details.errors[].field/problem`) | fix the record your handler builds, or validate `args` first |
| `UNIQUE_VIOLATION` | an insert or update would repeat a `unique` field set (`details.existing_id`) | check first with `find`/`count`, then return a result or an `AppError` |
| `READ_ONLY_VIOLATION` | a read handler (or a workflow condition) tried to write or move a record. `details.file` and `details.line` point at the call, even inside a helper | declare the handler `"write"`, or move the write |
| `TESTS_FAILED` | a test in tests.json failed; the deploy was rejected (`details.problems`, `details.response`, `details.test`) | fix the app, or the test if the behaviour change is intended |
| `TESTS_INVALID` | tests.json is broken: an unknown handler or role, a `$reference` no earlier test saved (`details.errors` with paths) | fix every listed path |
| `UI_INVALID` | a file in `ui/` breaks the rules (type, name, size, no `index.html`, a symlink) | see section 6 |
| `WORKFLOWS_INVALID` | workflows.json is broken (`details.errors`, each with a path such as `workflows.approval.steps[1].skip_if.amount`) | fix every listed path |
| `WORKFLOW_FUNCTION_MISSING` / `WORKFLOW_FUNCTION_SIGNATURE` | workflows.json names a function workflows.py doesn't define, or one that isn't `fn(ctx, record)` | define it with that signature |
| `WORKFLOW_INCOMPATIBLE` | the new workflow drops a step that stored records are in (`details.examples`) | keep the step until no record is in it |
| `WORKFLOW_FORBIDDEN` | the caller lacks the current step's `by` role (`details.required_roles`) | the right role passes it; or grant the role |
| `WORKFLOW_FINISHED` | the record already reached the last step or an exit | check `ctx.workflow.state()` first |
| `WORKFLOW_FIELD_MANAGED` | a handler tried to set the workflow's field | leave it out; use `ctx.workflow.advance`/`exit` |
| `WORKFLOW_UNKNOWN` / `WORKFLOW_EXIT_UNKNOWN` | no such workflow, or no such exit | check workflows.json |
| `WORKFLOW_BAD_CONDITION` / `WORKFLOW_LOOP` / `WORKFLOW_BAD_NOTE` | a condition function didn't return a bool / more than 50 moves in one call / a note that isn't a short string | fix the function or hook |
| `UNKNOWN_COLLECTION` | the collection isn't declared in this app | add it to app.json, or fix the name |
| `INVALID_QUERY` | a bad `where` field or value type, or a bad `limit` | filter on declared fields, with values of the field's type |
| `RECORD_NOT_FOUND` | `update` on an id that doesn't exist | `get` first, then raise your own `AppError` |
| `UNKNOWN_HANDLER` | there is no such handler (`details.available`) | check the name, and that the latest deploy was `activated` |
| `AUTH_REQUIRED` | the handler isn't public and the call had no login | `neai signup`/`neai login`, then `neai call --as <username>` |
| `AUTH_INVALID` | the token is unknown, expired, logged out, or from another app | log in again |
| `FORBIDDEN` | the user lacks the role the handler needs (`details.required_roles`, `details.your_roles`) | the operator grants it: `neai users <app> grant <username> <role>` |
| `NOT_LOGGED_IN` | the CLI has no saved login for `--as <username>` in that app | `neai login <app> <username>` (or `neai signup`) |
| `LOGIN_FAILED` / `LOGIN_LOCKED` | wrong username or password / too many wrong passwords (`details.locked_until`) | check them / wait, or `neai users <app> unlock <username>` |
| `USERNAME_INVALID` / `USERNAME_TAKEN` / `PASSWORD_TOO_SHORT` / `PASSWORD_INVALID` | sign-up input problems. Usernames are 2-32 characters: lowercase letters, digits, `.`, `_`, `-` | pick another username or a longer password |
| `SIGNUP_CLOSED` | the app's `auth.signup` is `"closed"` | the operator creates the account: `neai users <app> add <username>` |
| `ROLE_UNKNOWN` / `USER_NOT_FOUND` | `neai users` named a role app.json doesn't declare, or a user that doesn't exist | check `neai users <app> list` |
| `PASSWORD_REQUIRED` / `PASSWORD_MISMATCH` | there was no terminal to type the password in / the two entries differed | use `--password-stdin` / type it again |
| `CALLER_INVALID` | an in-process caller wasn't a clean username (internal API) | pass a username |
| `ARGS_INVALID` | the arguments don't match the handler's declared `args` (`details.errors` names each one; `hint` lists what the handler takes), or `args` isn't a JSON object, or `--args` isn't valid JSON | send what the hint lists, quoted for the shell |
| `HANDLER_EXCEPTION` | your handler raised an unexpected exception (`details.traceback` shows handlers.py lines); its writes were rolled back | fix the bug, or raise `AppError` for expected cases |
| `HANDLER_TIMEOUT` / `QUERY_TIMEOUT` | the handler (or one database query) ran past the time limit; rolled back | look for an infinite loop or a huge query |
| `HANDLER_BAD_RESULT` | the return value isn't plain JSON; rolled back | return dicts, lists or scalars |
| `NO_ACTIVE_VERSION` | the app has never been deployed successfully | deploy a valid definition |
| `APP_NOT_FOUND` / `VERSION_NOT_FOUND` | no such app (`details.known_apps`; also what `neai call` says for a misspelled app name) / no such version (`details.versions`) | check the name, or deploy |
| `SERVER_NOT_RUNNING` | `neai call`/`signup`/`login` found no live server for the app | start `neai serve <app>` or `neai run <dir>` |
| `PORT_IN_USE` | something is already listening on that port | pass another `--port`, or `--port 0` |
| `DATABASE_UNAVAILABLE` | Postgres can't be reached (the hint says where it looked) | `docker compose up -d`, or check `NEAI_DATABASE_URL`; servers reconnect by themselves |
| `DATABASE_BUSY` | every database connection of that server stayed busy for 10 s: it's overloaded | retry shortly; run more copies behind `neai proxy`, or raise `NEAI_DB_POOL_SIZE` |
| `NO_INSTANCE` / `INSTANCE_FAILED` | (proxy) no server of the app is running / one stopped mid-request (not retried, since it may have run) | start a server / check `neai ps` |
| `BAD_REQUEST` / `UNSUPPORTED_MEDIA_TYPE` / `FORBIDDEN_HOST` / `BODY_TOO_LARGE` | a malformed HTTP request | send JSON `{"args": ...}` with `Content-Type: application/json` to 127.0.0.1, under 1 MiB |
| `USAGE_ERROR` | bad CLI arguments | `neai <command> --help` |
| *your own codes* | an `AppError` raised by your handler (`kind: "app"`) | as your app defines |
