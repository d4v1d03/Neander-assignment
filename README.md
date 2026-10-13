# neai: a small app platform for coding agents

You write two files: `app.json`, which declares the data fields and handlers, and `handlers.py`, which holds the code. `neai` stores and validates the data, runs the app on localhost, and lets you inspect what happened. A new version only goes live after it passes validation, so a bad edit never costs data or takes down the version that works.

## Setup

You need Python 3.9 or newer, on macOS or Linux. There are no other runtime dependencies. It's tested on Python 3.9, 3.11, 3.13 and 3.14 on macOS; Windows is untested.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip      # older pips (e.g. macOS's Python 3.9) can't do the next step
pip install -e ".[test]"
neai --version
```

## What you can do

### Run an example app

```bash
neai run apps/notes                 # serves on http://127.0.0.1:8710 (leave it running)
```

In a second terminal:

```bash
neai call notes create_note --as alice --args '{"title": "hello"}'
neai call notes list_notes --as bob     # [] because bob can't see alice's notes
neai inspect notes                      # version, handlers, data, logs, last rejection
```

`apps/feedback` is the second example: shared posts with votes, where a repeat vote isn't counted twice.

### Build your own app

```bash
neai new todo --dir apps/todo           # a working skeleton
# edit apps/todo/app.json and apps/todo/handlers.py
neai deploy apps/todo                   # validated first; rejected with a clear reason if broken
```

[AGENTS.md](AGENTS.md) is the complete guide, written so a coding agent can follow it on its own.

### Use the web UI

```bash
neai ui                                 # then open http://127.0.0.1:8700
```

In the UI you can:
* browse apps and their data;
* start and stop servers;
* call handlers as any caller;
* read logs and rejected deploys;
* edit and deploy code.

### Build apps with AI (DeepSeek)

Put your key in a `.env` file in the project root. Copy `.env.example`, which shows the format; `.env` itself is git-ignored:

```bash
cp .env.example .env && chmod 600 .env   # then paste your key after DEEPSEEK_API_KEY=
neai ui
```

`neai` reads `.env` from the folder you run it in, then from the project root. A variable `export`ed in your shell takes priority. The key stays on your machine: it's never sent to the browser or logged, and `neai ui` only reports which variable *names* it loaded.

Click **New app** (or **Edit** on an app), describe what you want, and click **Generate**. Review the code, then click **Deploy**. If the platform rejects it, click **Fix with AI** to send the error back to the model.

The default model is `deepseek-flash`. To use another model, set `DEEPSEEK_MODEL=deepseek-v4-pro`.

### Run the demo and the tests

```bash
python scripts/demo.py                  # both apps, restart, code update, rejected definitions (22 checks)
pytest                                  # 83 tests
```

## Commands

| Command | What it does |
|---|---|
| `neai new <name>` | create a skeleton app |
| `neai validate <dir>` | check a definition without activating it |
| `neai deploy <dir>` | validate, then activate (or reject, and say why) |
| `neai run <dir>` | deploy if changed, then serve on localhost |
| `neai call <app> <handler> --as <caller> --args '<json>'` | call a handler |
| `neai inspect <app>` | identity, active version, handlers, data, logs, last rejection |
| `neai logs <app>` | recent calls and errors |
| `neai ui` | the web UI |

Every command prints JSON. Every error has a `code`, a `message` and a `hint` saying how to fix it.

## How it works

* **The contract is data.** `app.json` is checked and inspected without running any code.
* **Handlers get `ctx.caller` and `ctx.db`.** Read handlers get a read-only handle: any write fails with `READ_ONLY_VIOLATION`.
* **Handlers can declare their arguments** in `app.json`. The platform checks them before the handler runs, and the error names the bad argument.
* **Each write handler is one transaction.** If it fails, nothing is written.
* **One runtime runs every app,** and each app has its own SQLite file.
* **Deploys validate first:** the manifest, the code import, the handlers, and whether the schema still fits stored data. A rejected deploy leaves the last good version running.

## Design decisions

* **Contract as JSON, code as Python.** The contract stays readable even when the code is broken.
* **A schema change is accepted only if existing data still fits it.** That's one simple rule, with no migrations.
* **A single-threaded server.** Writes are sequential with no locking code.
* **The standard library only.** Setup is one install command.
* **Inspection reads the store directly.** It works when the server is down, and no network endpoint exposes all the data.

**Rejected alternatives:**
* **Decorators in a single Python file.** The contract could only be read by running the code, so a broken file couldn't even be inspected.
* **A SQL table per collection.** Every schema change would become DDL and migration work, and validation happens in Python anyway.
* **JSON files on disk.** There are no transactions, so a failing handler could leave half-finished writes.
* **Hot reload by watching files.** It can pick up half-saved files, and it never tells the agent whether the change was accepted.
* **The platform scoping data by caller automatically.** The brief puts scoping in handlers, and shared data (feedback posts) needs reads across callers.
* **FastAPI or Click.** Good tools, but not needed: the standard library keeps setup to one command.

[DESIGN.md](DESIGN.md) has the full reasoning, the safety measures, and what I'd do next.

## Assumptions

* **What counts as the schema:** only collections and fields. Adding or changing handlers is a code update.
* **Schema changes:** accepted only if all stored data still fits the new schema (e.g. a new optional field). Anything else is rejected with `SCHEMA_INCOMPATIBLE`.
* **Validation:**
  * Every insert and update is validated against the declared fields (string, integer, number, boolean, list, object).
  * Declared handler arguments are checked before the handler runs.
  * Stored data is only re-checked when a deploy changes the schema, since that's the only time old records can stop fitting.
* **A caught read-only violation still fails the call.** A read handler can't hide a write attempt with `try/except`.
* **Caller identity:** any non-empty, printable string of up to 256 characters, sent with each request.
* **Rejections and fallback:** a rejected deploy never stops the running version. `inspect` shows the latest rejection until a later deploy succeeds.
* **Notes privacy:** another caller's note looks exactly like a note that doesn't exist (`NOTE_NOT_FOUND`).
* **Feedback votes:** posts are visible to everyone, but voters aren't. A repeat vote succeeds without counting. There's no un-voting.
* **"One app at a time":** one app per server process. Separate servers can run side by side on different ports.
* **Inspection** is a trusted local operation. It reads the store directly and isn't exposed over HTTP.

## Known limitation

Handler code runs inside the server process. Read-only handles and data separation are API guarantees, not a security sandbox. There's more on this in [DESIGN.md](DESIGN.md).

## Safety

Servers only listen on localhost and refuse cross-site requests. App data is private to your user. Handlers time out and roll back. Secrets are redacted from logs. AI-written code is checked and only runs when you deploy it. [DESIGN.md](DESIGN.md) lists what's built in, and what I'd do with a couple of days.

## Time spent

About 6 hours, within the limit: the assignment arrived at 2:00 PM and was submitted before 8:00 PM on 13 October 2026, including planning, the platform, tests, docs and the demo.