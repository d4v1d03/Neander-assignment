# neai: a small app platform for coding agents

An app on neai is a **contract** (`app.json`: data, handlers, who may call them) plus **code** (`handlers.py`). Three optional parts sit beside them:
* a workflow (`workflows.json` and `workflows.py`) for records that move through steps;
* a UI (`ui/`);
* example tests (`tests.json`).

neai stores and validates the data, gives every app its own sign-up and login, enforces the contract, runs the app, serves its UI, and tells you what happened. A new version only goes live after it passes every check and its own tests, so a bad edit never costs data or takes down the version that works.

This is **v2**. v1, the original take-home with SQLite, is on `main`. v2 adds the four things the review asked for:
* **Stateless servers.** All state lives in Postgres, so you can run any number of copies of an app behind one URL.
* **Accounts and access rules for each app.** Real logins, plus `public` / `user` / role rules in the contract.
* **Workflows.** Steps, rules about who may move a record, skip rules and hooks, as a third contract file.
* **A UI for every app.** Its own `ui/` folder, or one generated from the contract.

It also adds unique constraints, field defaults, rollback, and `tests.json`. [docs/v2-plan.md](docs/v2-plan.md) is the plan v2 was built from.

## Setup

You need Python 3.11 or newer and Docker, on macOS or Linux. (macOS's built-in `python3` is too old: install one from python.org, Homebrew or pyenv.)

```bash
docker compose up -d                     # Postgres on 127.0.0.1:5433: all state lives here
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[test]"
neai --version
```

`neai` finds the database through `NEAI_DATABASE_URL` (or `--db`); the default is the one `docker compose` starts. `docker compose stop` stops it. Your data stays in a Docker volume until `docker compose down -v`.

## What you can do

### Run an app, with real accounts

```bash
neai run apps/notes                      # serves on http://127.0.0.1:8710; leave it running
```

In a second terminal, create two accounts (each asks for a password), then call as them:

```bash
neai signup notes alice
neai signup notes bob
neai call notes create_note --as alice --args '{"title": "hello"}'
neai call notes list_notes --as bob      # []: bob can't see alice's notes
neai inspect notes                       # version, handlers, data, accounts, logs, last rejection
```

`--as alice` sends the login token that `neai signup` (or `neai login`) saved. Nobody can call as alice without her password, and accounts belong to one app.

### Roles and public data

`apps/feedback` has public posts that anyone can read, even without logging in. Logged-in users post and vote; a repeat vote isn't counted twice, and a `unique` rule backs that up. Only users with the `moderator` role can delete posts. Roles are declared in `app.json`, and the operator grants them:

```bash
neai users feedback grant alice moderator
```

### Workflows: records that move through steps

Many apps have records that go through a **process**:
* an expense claim through manager and finance approval;
* an order through packing and shipping;
* a support ticket through triage;
* a document through review;
* a job application through interview rounds.

`workflows.json` describes such a process as data:
* the steps, in order;
* which role may move a record out of each step;
* rules that skip a step for some records, e.g. "claims under 100 skip finance review" or "urgent tickets skip triage";
* hooks in `workflows.py` that run when a record reaches a step;
* early exits such as `rejected`.

A handler just calls `ctx.workflow.advance(...)`. The platform checks the role, applies the rules, runs the hook and records the history, all in the handler's transaction. The engine is generic: nothing in it knows about any particular process.

`apps/hiring` is the example app that ships with neai: five interview rounds, where SDE1 candidates with 3+ years of experience skip the technical round, and reaching the HR round drafts an offer.

```bash
neai run apps/hiring --port 8720         # leave it running; then, in another terminal:
neai signup hiring rita && neai users hiring grant rita recruiter
neai signup hiring sam
neai call hiring post_job --as rita --args '{"title": "Backend SDE1", "level": "SDE1"}'
neai call hiring apply --as sam --args '{"job_id": 1, "experience_years": 4}'
neai call hiring pass_round --as rita --args '{"id": 2}'      # applied -> screening
neai call hiring pass_round --as rita --args '{"id": 2}'      # screening -> manager, "skipped": ["technical"]
```

### A UI for every app

Every app server is also its app's website. For example, open http://127.0.0.1:8720 for hiring:
* **Hiring has its own `ui/`.** Candidates apply and see a tracker of their rounds, with skipped ones struck through, and their offer. Recruiters and interviewers get a pipeline board, where Pass only works for the rounds their role may pass.
* **Notes has no `ui/`, so it gets a UI generated from its contract:** log in, then one form per handler.

UIs are plain HTML, CSS and JavaScript:
* They use `/_neai/sdk.js` for login and calls.
* They're deployed and versioned with the app, and served with a strict Content-Security-Policy.
* You can write them by hand, paste them into the dashboard editor, or have DeepSeek write one there.

### Tests that guard every deploy

`tests.json` lists example calls and what they should return. Every `neai deploy` and `neai validate` runs them first, against an empty sandbox copy of the app, in a transaction that's always rolled back. If a change breaks a promise, such as a skip rule, a permission or an ownership check, the deploy is rejected with the expected and actual values, and the working version keeps running. For example, after a change to hiring's skip rule:

```
TESTS_FAILED: tests[6] "experienced SDE1 candidates skip the technical round" failed:
              result.to: expected "manager", got "technical"
```

### Stateless servers: many copies, one URL

App servers keep nothing on their own disk. The code itself is loaded from the database. So you can run as many copies as you like, and put the small load balancer in front:

```bash
neai serve notes --port 5001             # (one terminal each)
neai serve notes --port 5002
neai proxy notes --port 5100             # round-robin over every live copy; skips dead ones
neai ps notes                            # the running copies
```

A login made on one copy works on the others. Kill a copy and the proxy keeps answering. Writes to one app still happen one at a time across all copies, using a lock in Postgres.

### Build your own app

```bash
neai new todo --dir apps/todo            # app.json, handlers.py, workflows.json and a passing tests.json
# edit the files
neai deploy apps/todo                    # checked, tested, then activated; or rejected, with the reason
```

[AGENTS.md](AGENTS.md) is the complete guide, written so a coding agent can follow it on its own.

### The dashboard, and building apps with AI

```bash
neai ui                                  # then open http://127.0.0.1:8700
```

In the dashboard you can:
* browse apps, their data, workflows and logs;
* start and stop servers, and open an app's own UI;
* sign in as an app's user and call handlers as them;
* manage accounts and roles;
* edit every file of an app in tabs, and deploy it.

**To build with DeepSeek,** copy `.env.example` to `.env` and put your key after `DEEPSEEK_API_KEY=`. `.env` is git-ignored; run `chmod 600 .env` too.
* **Generate** writes the backend: app.json, handlers.py, tests.json, and the workflow files when the app has a process.
* **Generate UI** writes `ui/index.html`.
* **Fix with AI** sends a rejected deploy's error back to the model.

The key stays on your machine: it's never sent to the browser or written to logs. The default model is `deepseek-flash`; set `DEEPSEEK_MODEL` to use another.

### Run the demo and the tests

```bash
python scripts/demo.py                   # every feature, end to end, in its own scratch database (44 checks)
pytest                                   # 165 tests, in a separate neai_test database
```

## Commands

| Command | What it does |
|---|---|
| `neai new <name>` | create a skeleton app |
| `neai validate <dir>` / `neai deploy <dir>` | check (contract, code, tests, stored data) / then activate, or reject with the reason |
| `neai run <dir>` | deploy if changed, then serve on localhost |
| `neai serve <app>` / `neai ps` / `neai proxy <app>` | serve from the database (any number of copies) / list them / one URL in front of them |
| `neai signup`, `login`, `logout <app> <user>` / `neai logins` | accounts; the login is saved for `neai call` |
| `neai call <app> <handler> --as <user> --args '<json>'` | call a handler as that user (leave out `--as` for a public handler) |
| `neai users <app> list\|add\|grant\|revoke\|passwd\|unlock\|remove` | the operator's account and role tools |
| `neai inspect <app>` / `neai logs <app>` | everything about an app / recent calls and errors |
| `neai versions <app>` / `neai rollback <app> --to N` | list versions / deploy an old one again, with every check |
| `neai ui` | the dashboard |

Every command prints JSON. Every error has a stable `code`, a `message` and a `hint` saying how to fix it.

## How it works

* **The contracts are data.** `app.json`, `workflows.json` and `tests.json` are checked against each other at deploy, without running app code. Errors name the path: `workflows.hiring.steps[2].skip_if.level`.
* **Handlers get `ctx.caller`, `ctx.user`, `ctx.db` and `ctx.workflow`.** The caller comes from a verified login token, never from the request. Access rules (`public`, `user`, a role) are checked before the handler runs.
* **Each write handler is one transaction,** including workflow moves and their hooks. If it fails, nothing is written. Read handlers get a read-only handle, and a read-only database transaction too.
* **Workflows own the step field.** Handlers call `ctx.workflow.advance(...)`; the platform checks the step's role, applies the skip rules, runs hooks and records the history.
* **Servers are stateless.** Data, code, users, sessions, logs and the list of running servers all live in Postgres. Every row is tagged with its app.
* **Deploys check everything first:** the contracts, the code import and the functions it must define, the UI files, the app's own tests in a sandbox, and whether stored data still fits. A rejected deploy leaves the working version running.

## Design decisions

* **Contracts as JSON, code as Python.** The contract stays readable and checkable even when the code is broken.
* **A schema change is accepted only if stored data still fits it,** or a `default` makes it fit. One rule, no migration engine.
* **The platform runs accounts; apps never see passwords.** Bearer tokens, stored only as hashes, work the same way for the browser, the CLI and agents.
* **Rules first as data, then Python.** Skip rules are JSON, checked at deploy. `{"function": ...}` is the escape hatch.
* **Synchronous workflows.** A move is part of the handler's transaction, not a background job: stronger guarantees, far less machinery.
* **UIs are untrusted.** Each runs on its own origin, under a strict Content-Security-Policy, talking to its own server only through the helper library.
* **Scale by copies, not threads.** Stateless single-threaded servers, plus a per-app write lock in Postgres.

[DESIGN.md](DESIGN.md) has the full reasoning, the rejected alternatives, the safety measures and what I'd do next.

## Assumptions

* **What counts as the schema:** collections, fields, `unique` rules and workflow steps. Changing handlers, access rules or tests is a code update.
* **Accounts belong to one app,** and the operator is whoever holds the database credentials. There's no separate platform login.
* **Ownership** ("only your notes") is decided in handler code; **roles** go in the contract.
* **Notes privacy:** another user's note looks exactly like a note that doesn't exist (`NOTE_NOT_FOUND`).
* **Feedback votes:** posts are public, voters aren't. A repeat vote succeeds without counting. There's no un-voting.
* **Workflows:** one per collection. Steps go forward in order (with skips and early exits); a branch is two steps with opposite skip rules. Who may reject or withdraw is up to the handler.
* **Inspection** (`inspect`, `logs`, `users`) is a trusted operator tool that reads the database directly.

## Known limitations

Handler code runs inside the server process. Access rules, read-only handles and per-app data are enforced by the platform's API, but they are not a security sandbox against hostile code. Workflow steps can't run on a timer (e.g. "auto-reject after 14 days"), and apps can't call outside services. More in [DESIGN.md](DESIGN.md).

## Safety

* **Network:** servers only listen on localhost, check the Host header, and only accept JSON POSTs.
* **Passwords and tokens:** passwords are stored as scrypt hashes and tokens as SHA-256 hashes, so a database dump reveals neither. Five wrong passwords lock an account for 15 minutes.
* **Handlers** time out and roll back.
* **Secrets are redacted** from logs.
* **AI-written code** is checked, shown to you, and only runs when you deploy it.
* **App UIs** can only load their own files and talk to their own server.

[DESIGN.md](DESIGN.md) lists every protection.

## Time spent

* **v1** took about 6 hours: the assignment arrived at 2:00 PM and was submitted before 8:00 PM on 13 October 2026.
* **v2** was built from 15 to 17 October 2026, in six phases, each ending with the tests passing.
