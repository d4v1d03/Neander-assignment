# neai v2: a small app platform for coding agents

An app is a **contract** (`app.json`: data, handlers, who may call them) plus **code** (`handlers.py`). neai validates it, runs it, gives it logins and a UI, and refuses any change that would break it. v1, the original take-home, is on `main`.

## What's new in v2

1. **Stateless servers.** All state lives in Postgres: data, deployed code, users, logs and running servers. You can run many copies of an app behind one URL, and kill any of them.
2. **Accounts and access rules for each app.** Real sign-up and login, with passwords hashed. The contract says who may call each handler: `public`, `user` or a role.
3. **Workflows.** A third file, `workflows.json`, describes the steps a record goes through: who may move it on, rules that skip steps, and hooks. Handlers just call `ctx.workflow.advance(...)`.
4. **A UI for every app.** An app's own `ui/` folder (plain HTML/JS), or a UI generated from its contract.

Also new:
* `tests.json`: example calls that run before every deploy;
* `unique` rules and field defaults;
* `neai rollback`;
* the dashboard can edit every file, and DeepSeek can write workflows and UIs.
* **threads:** each server, and the dashboard, handles several requests at once (a slow one no longer holds up the rest), with a pool of database connections. `NEAI_THREADS` and `NEAI_DB_POOL_SIZE` tune it; see [DESIGN.md](DESIGN.md#concurrency-many-requests-at-once-in-each-server).

## Setup

You need Python 3.11+ and Docker.

```bash
docker compose up -d            # Postgres on 127.0.0.1:5433
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
```

## Try it

Start each server in its own terminal, and run the other commands in another. `neai signup` asks for a password.

**1. Stateless servers:** two copies of one app, with a load balancer in front.

```bash
neai deploy apps/notes
neai serve notes --port 5001
neai serve notes --port 5002
neai proxy notes --port 5100
neai signup notes alice
neai call notes create_note --as alice --args '{"title": "hi"}' --url http://127.0.0.1:5001
neai call notes list_notes --as alice --url http://127.0.0.1:5002   # copy 2 sees it, and alice's login
curl -s 127.0.0.1:5100/_neai/health                                  # the pid alternates between copies
```

Kill one copy and the proxy keeps answering. Run `docker compose stop` and calls fail with `DATABASE_UNAVAILABLE`; after `docker compose start` they work again, with no restart.

**2. Accounts and roles:** in feedback, reading is public, posting needs a login, and deleting needs the `moderator` role.

```bash
neai run apps/feedback --port 5011
neai call feedback list_posts                                       # works without a login
neai signup feedback bob
neai call feedback create_post --as bob --args '{"text": "dark mode"}'
neai call feedback delete_post --as bob --args '{"post_id": 1}'     # FORBIDDEN
neai users feedback grant bob moderator                             # (or the dashboard's Users card)
neai call feedback delete_post --as bob --args '{"post_id": 1}'     # works now
```

**3. Workflows:** hiring has five rounds, and SDE1 candidates with 3+ years skip the technical round (see `apps/hiring/workflows.json`).

```bash
neai run apps/hiring --port 5021
neai signup hiring rita && neai users hiring grant rita recruiter
neai signup hiring sam
neai call hiring post_job --as rita --args '{"title": "Backend", "level": "SDE1"}'
neai call hiring apply --as sam --args '{"job_id": 1, "experience_years": 4}'
neai call hiring pass_round --as rita --args '{"id": 2}'            # applied -> screening
neai call hiring pass_round --as rita --args '{"id": 2}'            # -> manager, "skipped": ["technical"]
neai call hiring history --as sam --args '{"id": 2}'
```

**4. A UI for every app:**
* **Hiring's own UI:** open http://127.0.0.1:5021. Candidates see their rounds, with skipped ones struck through. Recruiters and interviewers get a pipeline board.
* **A generated UI:** open http://127.0.0.1:5002. Notes has no `ui/`, so its UI is generated from the contract.

**5. Tests on every deploy:** change `">=": 3` to `">=": 30` in a copy of hiring's `workflows.json`, then deploy it. The deploy is refused with `TESTS_FAILED: ... expected "manager", got "technical"`, and the live version keeps running.

**The dashboard:** `neai ui`, then open http://127.0.0.1:8700. To use DeepSeek (Generate, Generate UI, Fix with AI), copy `.env.example` to `.env` and add your key.

## Tests

```bash
pytest                     # 191 tests (they use their own neai_test database)
python scripts/demo.py     # every feature end to end (44 checks)
```

[docs/v2-manual-tests.md](docs/v2-manual-tests.md) has a longer step-by-step test plan.

## More

* [AGENTS.md](AGENTS.md): the full API. A coding agent can build an app from it alone.
* [DESIGN.md](DESIGN.md): the design decisions, the rejected alternatives, safety, and what's next.
* [docs/v2-plan.md](docs/v2-plan.md): the plan v2 was built from.
