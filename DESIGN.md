# Design notes

The detailed reasoning behind `neai`. The [README](README.md) has the short version.

## How it works, and why

**The app contract is data, and the logic is code.** `app.json` declares the collections, their typed fields (`string`, `integer`, `number`, `boolean`, required unless `"required": false`) and the handlers, each marked `read` or `write`. Because the contract is plain JSON, the platform can validate it, compare it with stored data and show it in `inspect` without running any app code. That matters most when the code is broken, which is exactly when an agent needs to inspect. Manifest errors are all reported at once, each with a JSON path (`collections.notes.title.type`), so the agent can fix them in one pass.

**Handlers can declare their arguments.** The long form is `"create_note": {"kind": "write", "args": {"title": {"type": "string"}}}`, using the same field syntax as collections. Declared arguments are checked before the handler runs, so a typo or a wrong type fails with an `ARGS_INVALID` that names the argument, and the handler code only checks what types can't express. Declaring them is optional: the short form `"list_notes": "read"` passes arguments through unchecked. Argument changes aren't schema changes, because nothing stored depends on them.

**There are six field types:** `string`, `integer`, `number` and `boolean`, plus `list` (optionally typed, `"items": "string"`) and `object`. Lists and objects hold plain JSON. `where` filters only compare scalar fields, which keeps the query API equality-only.

**The handler context is tiny.** A handler is `fn(ctx, args)`. `ctx.caller` is the declared identity. `ctx.db` has six methods (`get`, `find`, `count`, `insert`, `update`, `delete`). `ctx.log()` writes to the app's logs. Every insert and update validates the whole resulting record. That includes a Python-specific trap: `True` is an `int` in Python, so a boolean must not pass as an integer.

**Read-only is enforced by the handle, and backed up by the transaction.** A read handler gets a handle whose write methods raise `READ_ONLY_VIOLATION` (naming the handler, operation and collection) before SQLite is touched. Its transaction is always rolled back. If the handler catches the error and carries on, the call still fails: a swallowed violation is still a bug, and a broad `except Exception` shouldn't hide it. Deploys also warn when a read handler calls `ctx.db.insert/update/delete` directly, so the mistake shows up before that branch ever runs.

**Each write handler is one transaction.** It commits when the handler returns and rolls back on any exception, timeout or non-JSON result. A failed call therefore means nothing was written, so an agent never has to reason about partial writes. Logs are written *after* the transaction, so the rollback never erases the log line that explains the failure.

**There is one runtime, with a store per app.** The runtime only knows "a manifest and a module", so Notes and Feedback run on it unchanged. Each app has its own `app.db`, so separation comes from the layout, not from a `WHERE` clause someone could forget. A handle also only sees the collections its app declares.

**Writes are sequential by construction.** The server is a single-threaded `http.server`, so calls run one at a time with no locking code. That is what makes the check-then-insert in Feedback's `vote` safe. Before each call, the server checks the active version id, so `neai deploy` from another shell takes effect on the next request.

**A deploy validates, then activates.** It parses the manifest, imports `handlers.py` from a fresh snapshot, checks that every declared handler exists with the signature `(ctx, args)`, and checks schema compatibility. Then, in one transaction, it snapshots the files to `versions/<n>/`, moves the active pointer and records the deploy. A rejection never touches records or the pointer, but it is recorded, so `inspect` can report `latest_rejected` until a later deploy succeeds. That includes a broken `app.json` whose name can't even be parsed: the platform falls back to finding the app last deployed from the same folder. `neai run` deploys first and, if the deploy is rejected, serves the last valid version (`"fallback": true`).

**The schema rule is: the new schema must accept the existing data.** The brief allows rejecting every schema change, but agents change schemas constantly while an app is young. One rule with no migration engine covers this. Adding a collection or an optional field works, and any change works while the app is empty. Adding a required field, or removing or retyping a field that holds data, is rejected with `SCHEMA_INCOMPATIBLE`, a field diff and example failing record ids. The rule is a single function, so it's easy to make stricter.

**Inspection is CLI-only and reads the store directly.** It works when the server is down, which is usually when you need it. Logs have three levels, so an agent can tell a bug from the app doing its job:
* `info`: the call succeeded.
* `warn`: a deliberate refusal (`AppError`) or a caller mistake.
* `error`: a handler bug or a rejected deploy.

`recent_errors` shows only the `error` level. It also means no network endpoint exposes the whole app's data; the app server only offers `/call/<handler>` and `/_neai/health`.

**Errors are built for an agent to act on.** Every error has a stable `code`, a `kind` (`app` for an `AppError` raised by handler code, `platform` for everything else), a `message`, a `hint` naming the fix, and `details` (field, path, line number, the handlers.py traceback lines, available handlers, and so on). CLI usage errors are JSON too.

## The web UI and AI generation (optional extras)

`neai ui` is a thin client over the same functions the CLI uses. It reads state like `neai inspect`, deploys through the same pipeline as `neai deploy`, and calls handlers through the app's running server like `neai call`. It lives in its own package (`neai.ui`), and the core never imports it.

With `DEEPSEEK_API_KEY` set, the editor can ask DeepSeek (`POST /chat/completions`, OpenAI-compatible, JSON output mode, default model `deepseek-flash`) for an app's two files. The prompt is `AGENTS.md` itself, the same documentation a coding agent gets, plus a few extra rules. When a deploy is rejected, **Fix with AI** sends the platform's structured error back to the model. That exercises the claim that the errors are something an agent can act on.

Generated code is never executed automatically. It is shown in the editor, and only runs when you click Deploy. Before it runs, the UI checks it statically (`ui/codecheck.py`) and refuses imports outside a small allow-list, `open`/`eval`/`exec`/`__import__` and similar, and dunder attribute access. This is a speed bump, not a sandbox (see the safety section below).

The UI asks before overwriting anything: replacing an existing app from New app, or a source folder holding edits that were never deployed. Page content is built with DOM APIs, never `innerHTML`, because record data comes from callers and must not be able to inject markup. The page is served with a strict Content-Security-Policy (no external scripts, no framing).

## Rejected alternatives

* **Putting everything in one Python file, with decorators.** It's concise, but the contract could only be read by importing the code. When the code doesn't import, the platform couldn't show the handlers or compare schemas.
* **A SQL table per collection, with generated DDL.** Every schema change would become DDL and migration work, and validation happens in Python anyway. JSON documents in one table, validated in Python, are enough at this scale.
* **Plain JSON files on disk.** There are no transactions, so a crash or a failing handler could leave half-finished writes.
* **Hot reload by watching files.** It can pick up half-saved files, and it gives the agent no verdict. An explicit `deploy` returns accepted or rejected, with the reason, synchronously.
* **Having the platform filter queries by caller automatically.** The brief puts scoping in the handlers, and shared data (feedback posts) needs reads across callers, so it would need an opt-out anyway. Instead, the docs give the agent a scoping recipe and a checklist.
* **FastAPI or Click.** Both are good tools, but the standard library keeps setup to one command, and a single-threaded server gives sequential writes for free.
* **`neai call` running handlers in-process, with no server.** It would give two ways of executing calls. Calls go through the running server, so what the agent tests is what clients get.
* **A React or other framework UI with a build step.** One HTML file with no dependencies is enough for a developer console, and it keeps "pip install, done" true.
* **An automatic generate, deploy and fix loop with no human step.** It would run model-written code repeatedly without anyone looking at it. One click per step keeps a person in the loop at negligible cost.

## Known limitation

**Handler code runs inside the server process.** As the brief allows, read-only handles and data separation are properties of the API, not security boundaries. Handler code could `import sqlite3` and open `app.db` directly, allocate unbounded memory, or block in C code where the `SIGALRM` time limit can't interrupt it. The time limit also only works on Unix, in the main thread (which is where the server and CLI run handlers).

Other limits worth knowing:
* `find` scans the collection in Python, which is fine for prototype data sizes and wouldn't be for large ones.
* There are no migrations (see the schema rule above).
* There is one app per server process, and one server per app.
* `handlers.py` must be a single file, because helper modules aren't part of the version snapshot.
* Identity is whatever the caller claims.
* Ids are unique per app, not per collection, and the id of a rolled-back insert can be reused.
* **The platform is built for Unix.** The handler time limit uses `SIGALRM`, so it is off on Windows. There, the "is the server alive" check probes the port, because `os.kill(pid, 0)` on Windows would send the server a Ctrl+C rather than check it. Tested on macOS with Python 3.9, 3.11, 3.13 and 3.14; Windows is untested.

## If an assumption changes

The follow-up session discusses one changed assumption. These are the likely ones, and what would change. In every case, the handler API (`fn(ctx, args)` with `ctx.caller` and `ctx.db`) stays the same.

* **Concurrent writers instead of sequential ones.**
  * Switch to a threaded or async server, with `BEGIN IMMEDIATE` per write handler (already used) and SQLite's busy timeout. SQLite then serialises writers safely.
  * Check-then-insert patterns, like Feedback's vote, stay correct inside one transaction.
  * The `SIGALRM` time limit only works in the main thread, so it would move to a per-call worker with a deadline.
  * Unique constraints would become worth adding as a second line of defence.
* **Several apps hosted at once.**
  * Route `/apps/<name>/call/<handler>` to one `Runtime` per app. Storage is already per app.
  * `server.json` becomes one record listing the apps it hosts.
  * Nothing in the contract changes.
* **Untrusted handler code.** This is the biggest change. Handlers move into a separate worker process with no filesystem access to `app.db`, and talk to a platform-owned data handle over a pipe. Read-only and separation then become real boundaries, with OS resource limits and a hard kill on timeout. See the safety plan below.
* **Real authentication.**
  * Verify identity at the HTTP boundary (signed tokens, or OS identity locally) and fill `ctx.caller` from it. Callers can no longer assert any identity they like.
  * Inspection gets its own admin credential.
* **Schema migrations required.**
  * Add an explicit `migrate` step: a versioned function `(old_record) -> new_record` per collection.
  * Run it as a dry run first, reporting the records that would change. Then apply it, inside the same transaction that activates the version, after an automatic backup of `app.db`.
  * The current compatibility rule stays as the fast path, for changes that need no migration.
* **Larger data.** Replace the Python scan in `find` with SQLite `json_extract` expression indexes on declared fields. The query API stays equality-only.
* **TypeScript handlers.** The manifest, store, deploy pipeline and CLI are language-neutral. Only the loader and the call boundary change: run handlers in a Node worker and speak JSON over stdin/stdout. That is the same isolation step as for untrusted code.

## Safety considerations

The brief describes a trusted local prototype, so the main risks are buggy agent-written (or model-written) code, and other software on the same machine, including web pages in a browser, reaching the localhost servers. These protections were cheap and are built in now:

| Protection | Why |
|---|---|
| Servers bind to `127.0.0.1` only; there is no option to change it | never reachable from the network |
| `Host` header must be `localhost`/`127.0.0.1` | blocks DNS-rebinding attacks, where a web page reaches a localhost server |
| POST bodies must be `application/json` | a web page can't trigger a write with a simple cross-site form post, because JSON forces a CORS preflight that the server doesn't approve |
| No HTTP endpoint on the app server exposes data in bulk | `inspect` is CLI/UI-only and reads the store with the user's own file permissions |
| `NEAI_HOME` and app folders are `0700`; `app.db` and `server.json` are `0600` | other users on the machine can't read app data or logs |
| 1 MiB request cap; 1 MB cap on `app.json` / `handlers.py`; 10 s socket timeout | a single request can't exhaust memory or stall the single-threaded server |
| Handler time limit (5 s, `NEAI_HANDLER_TIMEOUT`), also on import | an infinite loop can't hang the server, and its writes are rolled back |
| Strict names for apps, collections, fields and handlers | an app name becomes a directory name, so `../x` is rejected: no path traversal |
| Strict manifests and request bodies (unknown keys rejected) | typos fail loudly instead of being silently ignored |
| Every write handler is atomic; a non-JSON result rolls back | no partial writes |
| Sensitive-looking argument keys (`password`, `token`, `secret`, `api_key`, …) are redacted in logs; values truncated; 1,000 entries per app | logs can't quietly collect secrets or grow without bound |
| The DeepSeek key is read by the local server only, from the environment or a git-ignored `.env` (warned about if it's readable by other users) | it never reaches the browser or the logs, and can't be committed by accident |
| AI-written code is shown before it runs, statically checked, and deployed only on a click | no model output executes unseen |
| UI page: DOM APIs only (no `innerHTML`), strict CSP, `frame-ancestors 'none'` | stored data can't inject script into the developer's UI; no clickjacking |

### With a couple of days instead of a few hours, in priority order

1. **Isolate handler code from the platform.** Run each app's handlers in a separate worker process that has no access to `app.db`. The worker would talk to the platform-owned data handle over a pipe, so read-only and separation become enforced boundaries rather than API conventions. Apply OS resource limits (`RLIMIT_AS`, `RLIMIT_CPU`, open files) and a hard kill on timeout. That also fixes the C-code timeout gap and makes the static check for AI code a second line of defence rather than the first. After that, consider a real sandbox (macOS `sandbox-exec`, seccomp/nsjail on Linux, or running handlers in WASM) that denies network and filesystem access.
2. **Real caller identity.** Replace the caller-asserted string with a verified one: local signed tokens issued by `neai`, or OS identity for a local tool. The handler API stays the same (`ctx.caller`), and only the HTTP boundary changes. Add per-caller rate limits at the same time.
3. **Authenticate developer operations.** Put the UI and any HTTP inspection or deploys behind a per-server admin token (written to a `0600` file and checked with a constant-time comparison), kept separate from caller identity.
4. **Limit what logs and errors expose.** Make argument logging opt-in per handler, use a configurable redaction list, and add a retention period. Return tracebacks only to developer tooling, not to callers, once callers aren't the developer's own agent.
5. **Protect data integrity across changes.** Back up `app.db` automatically before any schema change is activated (SQLite's backup API), offer `neai rollback <app> <version>` for code, and support explicit, reviewed migrations with a dry run that reports exactly which records change.
6. **Bound the data.** Add per-field size limits, per-collection record limits and per-app database size quotas, with clear errors when they're reached.
7. **Test adversarially.** Add property-based or fuzz tests for the validator and request parser, and a suite of deliberately hostile handlers (fork bombs, huge allocations, direct database access) to confirm the isolation from item 1 holds.
8. **Prove where deploys came from.** Keep a signed or append-only deploy history (who deployed which hash, and when, and whether it was model-written), so it's possible to show which code produced which data.

## Testing the docs with fresh agents

Twice before submitting, a fresh coding agent with no access to the source, tests or README was given only `AGENTS.md` and a new app to build. This is a rehearsal of the follow-up session.

**Run 1: a poll app** (2–5 options, one vote per caller with re-voting, creator-only close). The agent got it working on the first deploy, and the platform's errors were enough to fix every deliberate mistake. It found twelve gaps, all addressed:
* **Docs:** how ids work, how to model lists, how to stop a server, what `recent_errors` means, that a rejected deploy blocks everything shipped with it, and `find` edge cases.
* **Code:** `AppError` now takes `hint`/`details` and enforces UPPER_SNAKE_CASE codes; `warnings` is always present; deploys warn when a read handler writes; log ordering was fixed.

**Run 2: an RSVP app** (capacity limits, changing your answer, a host-only guest list, cancellation). The agent built it correctly using the updated docs, tested it with six callers, and made seven deliberate mistakes, each reported clearly. Its main finding was the one run 1 had also hit: `recent_errors` mixed real bugs with expected refusals. Since both agents tripped on it, it was fixed in the code, not just the docs:
* **Logs:** the `warn` level was added (see "Inspection" above).
* **Other fixes:**
  * restoring the active files now clears `latest_rejected`;
  * `neai call` reports `APP_NOT_FOUND` for a misspelled app;
  * handler bugs return HTTP 500;
  * `READ_ONLY_VIOLATION` reports the offending line, even inside a helper;
  * error responses carry the version.

## Tests

`pytest` runs 83 tests in about 4 seconds, using temporary directories and fixed inputs. A fresh copy of the repo, set up by following the README exactly, passes every test and the demo on Python 3.9, 3.11, 3.13 and 3.14.

* `test_validation.py`: manifest errors with paths; every field type, including bool-vs-int, NaN, typed lists and objects; required, unknown and reserved fields; declared handler args checked before the handler runs; the long handler form.
* `test_runtime.py`: caller scoping; repeat votes; read-only violations, including a swallowed one and one inside a helper (with its line); rollback with logs kept; non-JSON results; time limits; `AppError` hint/details; log levels; separate stores and `UNKNOWN_COLLECTION`.
* `test_deploy.py`: restart persistence; no-op redeploys; live code updates; compatible and incompatible schema changes; every kind of rejected definition (asserting data intact, old version serving, `inspect` explains); dry runs; restart fallback; the read-handler warning.
* `test_e2e.py`: the real CLI and HTTP server, including the cross-site and DNS-rebinding protections, and the Windows-style liveness check.
* `test_ui.py`: the static code check; the exact DeepSeek request format and error mapping, against a fake DeepSeek server; that the key is never exposed; UI deploys, conflict prompts, start/call/stop; the UI page's protections.

## What I'd do next

The first steps would be items 1–3 of the safety list. After that:
* Indexed `find` using `json_extract` expression indexes, to replace full scans.
* `neai rollback` to return to an earlier version.
