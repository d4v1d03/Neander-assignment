# Design notes

The detailed reasoning behind `neai`. The [README](README.md) has the short version, [AGENTS.md](AGENTS.md) the complete API, and [docs/v2-plan.md](docs/v2-plan.md) the plan v2 was built from.

**The idea:** an app is a **contract** (`app.json`) plus **code** (`handlers.py`). Three optional parts sit beside them: a workflow contract with its Python (`workflows.json`, `workflows.py`), a UI (`ui/`), and example tests (`tests.json`). One generic runtime runs every app. Nothing in the platform knows about notes, feedback or hiring.

v1 (on `main`) ran each app on one machine, with SQLite and a caller-declared identity. v2 adds:
* stateless servers on Postgres;
* accounts and access rules for each app;
* workflows;
* a UI for every app;
* `tests.json`.

The handler API stayed the same throughout: `fn(ctx, args)` with `ctx.caller` and `ctx.db`.

## How it works, and why

**The contract is data, and the logic is code.**
* `app.json` declares collections with typed fields, handlers marked `read` or `write`, who may call each handler, roles, and `unique` rules.
* Because it's plain JSON, the platform can validate it, compare it with stored data and show it in `inspect` without running any app code. That matters most when the code is broken, which is exactly when an agent needs to inspect.
* All errors are reported at once, each with a JSON path (`collections.notes.title.type`), so the agent can fix them in one pass.
* The same holds for `workflows.json` and `tests.json`. All three are checked against each other at deploy, so a workflow naming an undeclared role, or a test calling a missing handler, is a deploy error.

**Handlers can declare their arguments** with the same field syntax as collections (`"args": {"title": {"type": "string"}}`). Declared arguments are checked before the handler runs: a typo or a wrong type fails with an `ARGS_INVALID` that names the argument. The handler then only checks what types can't express.

**There are six field types:** `string`, `integer`, `number`, `boolean`, `list` (optionally typed) and `object`. Fields are required unless `"required": false`. A `default` fills in missing values. Validation guards a Python trap (`True` is an `int`) and a Postgres one: the NUL character can't be stored.

**The handler context is small.** A handler is `fn(ctx, args)` and gets:
* `ctx.caller`, `ctx.user` and `ctx.has_role()`, filled from a verified login token;
* `ctx.db` with six methods (`get`, `find`, `count`, `insert`, `update`, `delete`);
* `ctx.workflow`;
* `ctx.log()`.

Every insert and update validates the whole resulting record and checks the `unique` rules.

**Read-only is enforced three ways.**
1. A read handler's handle raises `READ_ONLY_VIOLATION` (naming the handler, operation, file and line) before the database is touched.
2. Its transaction is a Postgres `READ ONLY` snapshot that is always rolled back.
3. A caught violation still fails the call, so a broad `except Exception` can't hide the bug.

Deploys also warn when a read handler calls a write method or `ctx.workflow.advance` directly.

**Each write handler is one transaction,** including any workflow moves and their hooks. It commits when the handler returns, and rolls back on any exception, timeout or non-JSON result. A failed call means nothing was written. Logs are written *after* the transaction, so the rollback never erases the line that explains the failure.

**One runtime, one database, data tagged by app.**
* The runtime only knows "a manifest, a module, and maybe workflows".
* All apps share one Postgres database, and every row carries its app's name. Handler code never writes SQL, so the platform's queries, which always filter by app, are the boundary.
* A handle only sees the collections its own app declares.

**A deploy checks everything, then activates.** It runs these in order:
1. **Contracts:** `app.json`, `workflows.json` and `tests.json`, each against the others.
2. **UI files:** types, names, sizes.
3. **Code:** imports `handlers.py` and `workflows.py`, and checks every declared handler and named function exists with the right signature.
4. **The app's own tests,** in a sandbox.
5. **Stored data,** under the app's write lock: the schema, `unique` rules and workflow steps must still fit every stored record.

Only then does it store the files as a new version and move the active pointer, in one transaction. A rejection changes nothing, but is recorded, so `inspect` reports `latest_rejected` until a later deploy succeeds. `neai run` serves the last valid version if the new one is rejected (`"fallback": true`).

**The schema rule: the new schema must accept the existing data.**
* Agents change schemas constantly while an app is young, and one rule with no migration engine covers it.
* Accepted: adding a collection, an optional field, or a field with a `default` (filled into old records during the deploy), and any change while the app is empty.
* Rejected with `SCHEMA_INCOMPATIBLE`, a field diff and example record ids: removing or retyping a field that holds data, or adding a `unique` rule the data already breaks.

**Inspection and logs.**
* `inspect`, `logs` and `users` are operator tools that read the database directly, so they work when no server is running. No network endpoint exposes the whole app's data.
* Logs have three levels, so an agent can tell a bug from the app doing its job:
  * `info`: the call succeeded;
  * `warn`: a deliberate refusal, an access rule, or a caller mistake;
  * `error`: a bug or a rejected deploy.
* `recent_errors` shows only the `error` level.

**Errors are built for an agent to act on.** Every error has a stable `code`, a `kind` (`app` for an `AppError` raised by handler code, `platform` for everything else), a `message`, a `hint` naming the fix, and `details`: field, path, file and line, traceback lines, available handlers, expected versus actual. CLI usage errors are JSON too.

## Stateless servers: all state in Postgres

**The problem with v1.** v1 servers kept nothing in memory between calls, but their state sat on *their own disk*: `app.db`, the `versions/` folders and `server.json`. Two copies of an app would have split the data, and a load balancer couldn't work. Stateless means the server's machine is disposable.

**What moved where.**
* **Records, deployed code, active version, deploy history, logs, users, sessions and workflow history** are all in one Postgres database.
* **Code is stored as file text in `neai_versions`.** A server imports it straight from memory, registering the source with `linecache` so tracebacks still show the lines. `neai serve <app>` needs no folder at all.
* **The list of running servers** is `neai_instances`, with a heartbeat every 5 s.
* **The only local file left** is the CLI's saved logins, which is client state.

**Writes stay one at a time per app, across threads and servers.**
* Each write handler, and each deploy's check-then-activate, starts with `pg_advisory_xact_lock(hash(app))`.
* So Feedback's check-then-insert vote stays race-free with any number of servers: a test races 3 connections and gets exactly one vote.
* Reads take no lock.

**Queries run in SQL.** `find` and `count` become `data @> {...}` containment queries on a GIN index, with `{"field": None}` meaning "key missing".

| Measured on the same Mac | v1 | v2 |
|---|---|---|
| `list_notes` at 100k notes | 151 ms | 48 ms |
| Feedback `list_posts` at 200 posts / 10k votes | over 5 s (timed out) | 390 ms |
| One write | 0.09 ms | 1.9 ms |

The slower write is the honest cost of a database over a socket, plus the lock and the log write.

**Failure behaviour.**
* **Postgres down:** a call fails with `DATABASE_UNAVAILABLE` (503). Once Postgres is back, the next call works without a restart.
* **Dead idle connections:** one that died while idle is replaced, and the statement retried once. That's safe because it only happens before a transaction has started. One test cuts a TCP pipe to Postgres mid-run; another kills the server's connection from the database side.
* **Runaway queries:** a statement timeout one second past the handler limit stops one.

**Display order.** Records are `jsonb`, for indexing. Contracts, errors and log data are `json`, which keeps the order they were written in (`jsonb` reorders keys).

**`neai proxy`** is a small round-robin load balancer over the live instances. It moves to the next instance only when the connection *couldn't be opened*. A request that reached an instance and then failed isn't retried, because a write might already have run.

## Concurrency: many requests at once in each server

**The problem.** v2's servers handled one request at a time, so a slow request held up every other one. The dashboard showed it worst: while DeepSeek worked on a Generate (up to two minutes), the app list wouldn't even load. Stateless copies behind the proxy were the only way to scale.

**Now each server runs requests on threads.** It handles up to 16 at once (`NEAI_THREADS`). When all of them are busy, new connections wait in the operating system's queue rather than piling up threads. The dashboard works the same way. Threads help because almost all of a request's time is spent *waiting*, on Postgres or on DeepSeek. Heavy Python work still runs one thread at a time (the GIL), and copies behind the proxy are still the way to use more cores.

**What had to change, and why.** Shared *data* was already safe, because v2's copies are separate processes and Postgres enforces the per-app write lock across all of them. What threads add is things shared *inside one process*:
* **The database connection became a pool** (10 per server, `NEAI_DB_POOL_SIZE`). A transaction is tied to the connection it runs on, so two threads on one connection would mix their transactions; one thread's rollback could undo another's write. Now a statement borrows a connection and returns it, and a transaction keeps its own until COMMIT. When all are in use, a request waits up to 10 s, then fails with `DATABASE_BUSY` (503). Results are read in full before the connection goes back.
* **Loading a new version takes a lock.** After a deploy, every thread notices the new version at once. One loads it, and the others wait a moment and reuse it, instead of each compiling its own copy. The usual call takes no lock, since a loaded version never changes.
* **The time limit works on worker threads.** `SIGALRM` only reaches the main thread. On a worker, a watchdog thread raises `HANDLER_TIMEOUT` into the handler instead (CPython's `PyThreadState_SetAsyncExc`). A lock between the two makes sure it can never fire after the handler has finished, into the platform's own code. It lands between Python instructions, so it can't cut a `time.sleep` short. For that, Postgres ends any transaction left idle half a second past the limit, which releases the app's write lock.
* **The heartbeat has its own thread,** so a server whose request threads are all busy isn't mistaken for a dead one.
* **The dashboard locks per app.**
  * Start and Stop: two clicks must not start two servers ("is it running? no, start it" from two threads at once).
  * Saving and deploying a folder: two tabs must not mix their files.
  * Different apps never wait for each other, and no lock is held while waiting on DeepSeek.
* **DeepSeek calls are limited** to 2 at once (`NEAI_AI_CONCURRENCY`). More wait their turn, up to 90 s, then get `AI_BUSY`.

**Races fixed on the way.** Threads made these easy to hit, but they could already happen across copies:
* **Logins lock the account's row** (`SELECT ... FOR UPDATE`). Otherwise a burst of parallel wrong passwords would all read "not locked yet" and get past the 5-try lockout. A test fires 10 at once and gets exactly 5 `LOGIN_FAILED` and 5 `LOGIN_LOCKED`.
* **Role grants lock the row too,** so two grants at the same moment both stick.
* **A deploy compares with the active version as it is under the write lock,** not as it was before its tests ran. Another deploy may have gone live in between.

**Measured** on the same Mac: 32 clients sending 800 mixed `create_note`/`list_notes` calls to one notes server took 1.7 s with 1 thread (466 req/s) and 0.9 s with 16 (902 req/s), with no errors.

## Accounts and access rules

**Accounts belong to an app, but the platform runs them.** alice in notes and alice in feedback are different accounts. Sign-up, login, hashing and tokens are platform code, so apps, especially AI-written ones, never touch a password.

**Tokens, not cookies.**
* Login returns a random 32-byte token. The database stores only its SHA-256 hash, so any server copy can check it, and logout really revokes it.
* Cookies are ambient credentials that browsers send to every port on `127.0.0.1`: notes' cookie would also reach feedback.
* A bearer header works the same way for the browser, the CLI and agents, and needs no CSRF defence.

**The caller can't be asserted.**
* `ctx.caller` and `ctx.user` come from the token. A body naming a `"caller"` is refused with a hint.
* There's no `--as` bypass: the CLI's `--as alice` sends alice's saved token.
* Only in-process callers (tests, the platform itself) pass a user directly. That API isn't reachable over HTTP.

**Access is declared in the contract and enforced before the handler runs:** `public`, `user` (the default), or roles from `auth.roles`.
* Ownership ("only your notes") stays in handler code, because it depends on the data. Roles are about who, and belong in the contract.
* Roles are read on every call, so a grant takes effect on the next request.

**Passwords.**
* They're stored as scrypt hashes (n=2^14, r=8, p=1) with a random salt, and compared in constant time.
* A login for an unknown username still computes a hash, so timing reveals nothing, and the error is identical.
* Five wrong passwords lock the account for 15 minutes. The counter lives in the database, so every server sees it.
* A password change or an account removal ends that user's sessions.

**The operator** is whoever holds the database credentials. They use `neai users`, or the dashboard's Users card. A separate platform login was left out on purpose: its only users would be the people who can already read the database.

## Workflows

**What a workflow is here:** the process one record goes through, with business rules about its path. For example:
* an expense claim through manager and finance approval ("claims under 100 skip finance review");
* an order through packing and shipping ("digital orders skip shipping");
* a ticket through triage;
* a job application through interview rounds (`apps/hiring`).

That logic otherwise spreads across handlers as `if`s on a status field, where it's easy to get wrong and impossible to inspect. The engine is generic: a workflow names a collection, a field and a list of steps, and nothing in the platform knows any particular process.

**Contract plus code, like the app itself.**
* `workflows.json` declares the steps, who may move a record out of each one (`by`), skip rules, hooks (`on_enter`) and early exits.
* `workflows.py` holds only the Python the contract names.
* Deploy checks it all against `app.json`:
  * the collection and the step field exist, and the field is a string with no default;
  * every role in `by` is declared;
  * every field in a rule exists, and each value fits its type;
  * every named function exists as `fn(ctx, record)`;
  * the last step has no skip rule.

**Rules are data first.**
* `{"amount": {"<": 100}}` or `{"priority": "urgent"}`, with `== != > >= < <= in not_in` combined by `all`, `any` and `not`, covers most business rules. A typo in a field name is a deploy error, not a silent "never skips".
* `{"function": "name"}` calls Python with a **read-only** ctx: a rule may look at other records, but can't write or move anything.

**Synchronous and transactional.**
* `ctx.workflow.advance` runs inside the calling handler's write transaction, under the app's lock.
* The checks, the skips, the step change, the hook's writes and the history row commit together or not at all.
* A failing hook undoes the move, and its traceback points into `workflows.py`.
* Nothing runs in the background: no queue, no worker, no "eventually".

**The step field belongs to the platform.**
* Handlers can't set it (`WORKFLOW_FIELD_MANAGED`).
* The workflow gives the field a default of its first step. That reuses the defaults machinery: new records start there, and records that existed before the workflow join it there in the deploy transaction.
* A deploy that drops a step which records are still in is rejected (`WORKFLOW_INCOMPATIBLE`).

**Guarantees and limits.**
* A call stops after 50 moves (`WORKFLOW_LOOP`).
* Every move is recorded: who, when, from, to, what was skipped, and a note.
* `inspect` and the dashboard show how many records are at each step.
* One workflow per collection. Steps go forward in order; a branch is two steps with opposite skip rules. Sending a record *back* to an earlier step ("return to the employee for changes") isn't supported yet. It would be a small addition: a declared `back_to` on a step, checked like `by`.
* Who may reject or withdraw is up to the handler, because it usually depends on the record.

## A UI for every app

**Every app server is also its app's website.**
* `/` serves `ui/index.html` if the app has one. Otherwise it serves a UI generated from `/_neai/contract`: a login box and a form per handler. So every app is usable from its first deploy.
* The other `ui/` files are served by exact name.
* `ui/` is part of the version: checked, stored, deployed to every running server at once, and rolled back with the code.

**UIs are treated as untrusted (often AI-written), so they're boxed in.**
* **One origin per app (its own port).** The login token lives in that origin's storage, out of reach of every other app's page.
* **A strict Content-Security-Policy:** only the app's own files, `connect-src 'self'`, no objects, no frames, `form-action 'self'`, `base-uri 'none'`. Inline scripts are allowed, because a single generated file is the common case.
* **No filesystem behind the URLs:** an exact lookup in the version's files, so `/../app.json` can't escape.
* **The server enforces access and workflow rules on every call,** whatever the page does.
* **The public contract holds nothing secret:** no code, no skip rules, no hook names.
* **Deploy checks `ui/`:** text types only, at most 50 files and 2 MB, strict names, no symlinks, `index.html` required. It also warns when the page calls a handler that doesn't exist.

**`/_neai/sdk.js` is the only way a UI talks to its app.** It offers `contract()`, `me()`, `signup()`, `login()`, `logout()`, `call()` and `onAuthChange()`. Errors are thrown with the platform's `code`, `message` and `hint`.

## Example tests: tests.json

**Why:** a deploy that imports cleanly can still break behaviour. For example, someone "tidies" a skip rule and records silently stop skipping a step, or loosens an ownership check. `tests.json` lets an app ship its promises as example calls: who calls what, with which args, and what should come back. Every deploy and every `neai validate` runs them, so an agent learns about the break at once, with the expected and actual values.

**How it runs:**
* The calls run in order against an empty sandbox copy of the app, named `<app>~tests` (a name no real app can have), with the new version's code.
* The whole run is inside one Postgres transaction that is always rolled back. Real data is never visible or touched, and test users exist only during the run (`"as": "rita"` needs no password).
* It's the same runtime as real calls, so access rules, workflow `by` roles, skip rules, hooks and `unique` rules all apply.

**What a test can say:**
* `save` keeps a result for later tests (`"$job.id"`).
* `expect` is a partial match: listed keys must match, extra keys are fine, and lists match item by item. It can also expect an error code.

**How it fits deploy:**
* Tests run outside the app's write lock, so they never block live writes, and before the stored-data checks, which need the lock.
* `neai validate` runs them even on files that are already active.
* `neai new` starts every app with a passing `tests.json`.
* DeepSeek is asked to write tests along with the app, so a generated app is checked by its own examples at deploy, and **Fix with AI** gets the failing test.

## The dashboard and AI generation

**`neai ui` is a thin client over the same functions as the CLI.** It reads state like `inspect`, deploys through the same pipeline (every file of the app, edited in tabs), signs in through the app's own server, and calls handlers with that user's token. It lives in its own package, and the core never imports it.

**DeepSeek** is called through `POST /chat/completions` in JSON output mode, with the default model `deepseek-flash`.
* **Generate** writes the backend: app.json, handlers.py, tests.json, and the workflow files when the app has a process.
* **Generate UI** writes `ui/index.html` in a separate call. The files are large, and the UI is easiest to judge once the backend is settled.
* **The prompt is `AGENTS.md` itself,** plus a few rules. Tests keep `AGENTS.md`'s examples working, so the model is given accurate documentation.
* **Fix with AI** sends the platform's structured error back.
* **Generated code never runs unseen.** It's shown in the editor, `handlers.py` and `workflows.py` pass a static check (an import allow-list, no `open`/`eval`/`exec`, no dunder access), and nothing deploys without a click.

## Rejected alternatives

* **One Python file with decorators.** The contract could only be read by importing the code, so a broken file couldn't even be inspected.
* **A SQL table per collection.** Every schema change would become DDL and migrations. JSON documents validated by the platform, with indexed containment queries, are enough here.
* **Hot reload by watching files.** It picks up half-saved files and gives no verdict. An explicit deploy returns accepted or rejected, with the reason.
* **The platform scoping data by caller automatically.** Shared data needs reads across callers, so it would need an opt-out anyway. Ownership stays in handlers; *who* may call goes in the contract.
* **Keeping SQLite as a second backend in v2.** That's two code paths to keep identical. v1 stays on `main`.
* **A schema and a database role per app.** Stronger isolation, but dynamic DDL. It becomes the right step once handlers run out of process.
* **Cookies, signed stateless tokens (JWT-like), or one platform-wide account.** Cookies leak across ports on `127.0.0.1`. Signed tokens can't be revoked before they expire. A platform-wide account couples apps and isn't what "each app requires authorization" asked for.
* **Letting handlers implement login.** Every app, and every AI, would reinvent password storage.
* **Workflows as handlers with a status field and `if`s, or as Python only.** Rules would be hidden, unchecked and unrecorded. JSON first, Python as the escape hatch.
* **An async workflow engine with queues and retries.** It's right for long jobs that call outside services, but these apps only touch their own data, so synchronous gives stronger guarantees with far less machinery.
* **React or another framework with a build step; a declarative JSON UI contract.** Plain files are what agents write most reliably, and they keep the CSP strict. The generated UI already covers "zero effort".
* **All apps under one origin (`/apps/<name>/`).** Every app's page could read every other app's login.
* **An automatic generate, deploy and fix loop with no human step.** That would run model-written code repeatedly without anyone looking. One click per step keeps a person in the loop.

## Known limitations

* **Handler code runs inside the server process.** Access rules, read-only handles and per-app data are enforced by the platform's API, not by a sandbox. Hostile handler code could import a database driver, allocate unbounded memory, or block in C code where the time limit can't interrupt it. On a worker thread, it could also catch the timeout (`except BaseException`) and carry on. A stuck handler holds one of its server's threads until it returns. The time limit needs CPython, and Unix for the main thread's `SIGALRM`.
* **Workflow steps can't run on a timer,** and apps can't call outside services: there's no worker process.
* **Steps are linear,** one workflow per collection.
* **There are no migrations** beyond defaults. A field that holds data can't be removed.
* **`handlers.py` and `workflows.py` are single files.**
* **Ids are unique per app,** not per collection.
* **Scaling has limits:** all apps share one database, and writes to one app are serialised. That's correct, but it caps one app's write throughput at what one lock-holder can do.
* **Python 3.11 or newer,** tested on macOS with 3.11, 3.13 and 3.14. macOS's built-in Python 3.9 can't work: it has no `hashlib.scrypt`. Windows is untested (no `SIGALRM`).

## If an assumption changes

In every case the handler API (`fn(ctx, args)` with `ctx.caller`, `ctx.db`, `ctx.workflow`) stays the same, because the platform sits underneath it.

* **Untrusted handler code.** This is the biggest change.
  * Run handlers in a separate worker process, with no database credentials, talking to a platform-owned data handle over a pipe.
  * Read-only and separation then become real boundaries.
  * Add OS resource limits and a hard kill on timeout, then a real sandbox (seccomp/nsjail, or WASM).
  * Give each app a Postgres role limited to its own schema.
* **Timers and outside services in workflows.**
  * Add a `neai_jobs` table (run_at, attempts, last error) and a `neai worker` process that claims jobs with `FOR UPDATE SKIP LOCKED`, so any number of workers can run.
  * Events are written in the same transaction as the move that causes them (an outbox), so a rolled-back move never fires a job.
  * Add per-app secrets for outside calls.
* **One very large app.** Partition its records by collection or by key, move hot counts to maintained counters, and replace the per-app lock with row-level locks plus unique indexes.
* **Schema migrations.** Add an explicit `migrate(record) -> record` step per collection. Run it as a dry run that reports which records change, then apply it in the activation transaction. The compatibility rule stays as the fast path.
* **Several regions.** Postgres with replicas: reads go to the nearest replica, and writes and locks to the primary. Servers are already stateless.
* **TypeScript handlers.** The contracts, store, deploy pipeline and CLI don't depend on the language. Only the loader and the call boundary change (a Node worker speaking JSON), which is the same step as isolating untrusted code.

## Safety considerations

The risks are buggy agent-written or model-written code, and other software on the machine, including web pages in a browser, reaching the localhost servers. These protections are built in:

| Protection | Why |
|---|---|
| Servers bind to `127.0.0.1` only; Postgres is published on `127.0.0.1` only | never reachable from the network |
| The `Host` header must be `localhost`/`127.0.0.1` | blocks DNS rebinding, where a web page reaches a localhost server |
| POST bodies must be `application/json`; logins are bearer tokens, not cookies | a web page can't forge a call: no simple cross-site posts, no ambient credentials |
| Passwords stored as scrypt hashes, tokens as SHA-256 hashes; equal-time login failures; a lockout after 5 wrong passwords | a database dump reveals neither; no username probing; no online guessing |
| The caller comes only from the token; access rules are checked before the handler runs | no impersonation, and no handler runs for someone the contract excludes |
| App UIs: one origin per app, a strict CSP, files served by exact name | a page can't read another app's login, load outside code, send data elsewhere, or reach the filesystem |
| No HTTP endpoint exposes an app's data in bulk; the public contract has no code or rules | `inspect` is an operator tool |
| 1 MiB request cap, 1 MB file cap, 2 MB UI cap, 10 s socket timeout | one request can't exhaust memory or stall a server |
| Handler time limit (5 s, also at import), plus a Postgres statement timeout | an infinite loop or a runaway query can't hang a server; its writes are rolled back |
| Strict names; unknown keys rejected everywhere | no traversal through names; typos fail loudly |
| Every write handler is atomic, including workflow moves and hooks | no partial writes |
| Deploy runs the app's tests in a rolled-back sandbox | a behaviour regression can't go live, and tests can't touch real data |
| Sensitive argument keys (`password`, `token`, `secret`, …) redacted in logs; 1,000 log entries per app; NUL characters made safe | logs can't quietly collect secrets or grow without bound |
| The DeepSeek key stays in the environment or a git-ignored `.env`; `.neai/credentials.json` is `0600` | secrets never reach the browser, the logs or git |
| AI-written code is shown, statically checked, and deployed only on a click | no model output runs unseen |

**What's left to do, in priority order:**
1. Isolate handler code (see "Untrusted handler code" above).
2. Per-user rate limits on calls and sign-ups, stored in the database so every server sees them.
3. A separate operator credential for the dashboard, instead of "whoever can open it on this machine".
4. Opt-in argument logging and a retention period. Tracebacks go only to developer tooling once callers aren't the developer's own agent.
5. Automatic backups before schema changes, and an append-only deploy history (who deployed which hash, and whether it was model-written).
6. Size quotas per field, collection and app.
7. Adversarial tests: fuzzing the validators and the request parser, plus deliberately hostile handlers.

## Testing the docs with fresh agents

Before each submission, a fresh coding agent with no access to the source, tests or README was given only `AGENTS.md` and a new app to build. This is a rehearsal of the follow-up session.

**v1, run 1: a poll app** (2–5 options, one vote per caller with re-voting, creator-only close). The agent got it working on the first deploy, and the platform's errors were enough to fix every deliberate mistake. It found twelve gaps, all addressed:
* **Docs:** how ids work, how to model lists, how to stop a server, what `recent_errors` means, and `find` edge cases.
* **Code:** `AppError` gained `hint` and `details`; `warnings` is always present; deploys warn when a read handler writes.

**v1, run 2: an RSVP app.** The agent built it correctly, and made seven deliberate mistakes, each reported clearly. Its main finding, `recent_errors` mixing bugs with expected refusals, was fixed in code by adding the `warn` level.

**v2, run 3: an expense-claims app.** It was a different process from the shipped hiring example, and the agent never saw that app:
* draft, then manager review, then finance review (skipped under 100), then paid;
* reject and withdraw exits;
* 26 tests;
* a UI.

The agent built it from `AGENTS.md` alone. All 26 of its tests passed on the first validate, every refusal came back with the right code, and its UI worked in the browser. It made eleven deliberate mistakes. Every error was enough to fix the problem (for example, `skip_if.amout: unknown field (fields: owner, amount, ...)`), but several only showed up at run time. Fixed after the run:
* **Clearer errors and docs:** `"by": "user"` now explains to leave `by` out and check ownership in the handler, instead of pointing at `auth.roles`. The shapes of `exit` and `history` are documented, as is the fact that role grants apply to existing logins.
* **More caught at deploy, from the code itself:** a handler setting a workflow's step field, `ctx.workflow.*` naming a workflow that doesn't exist, and `exit` to an undeclared exit. `tests.json` args are now checked against the handler's declared args.
* **More information for handlers:** `ctx.workflow.state()` now says who may pass the current step (`by`, `can_advance`), so handlers don't copy the role map.
* **Consistent and corrected output:**
  * `TESTS_INVALID` and `TESTS_FAILED` both name a test as `tests[i]`;
  * `neai validate` reported "no version activated" for a broken definition of an app that has one, and now reports the active version;
  * `inspect` shows records in declared field order and marks the workflow's field as managed;
  * hints for `--password-stdin` and "did you mean" for UI handler typos.

One finding was kept as a decision: deploy *warnings* don't block a deploy. The docs now say to treat every warning as a bug.

## Tests

`pytest` runs 191 tests in about 40 seconds, against a separate `neai_test` database that is emptied before each test. `scripts/demo.py` runs every feature end to end in its own scratch database (44 checks).

| File | What it covers |
|---|---|
| `test_validation.py` | manifest errors with paths; every field type, including bool-vs-int, NaN and typed lists; declared args |
| `test_runtime.py` | caller scoping; read-only violations, including swallowed ones and ones inside helpers; rollback with logs kept; time limits; log levels; per-app data |
| `test_deploy.py` | restarts, no-op redeploys, live updates, compatible and incompatible schema changes, every kind of rejection, dry runs, fallback |
| `test_data.py` | unique rules, defaults, rollback, SQL queries, code loaded from the database, a 3-connection race, a cut database pipe |
| `test_auth.py` | hashing, tokens, sessions, lockout, timing-equal failures, access levels, roles, closed sign-up, the CLI's saved logins |
| `test_workflows.py` | your hiring example, step roles, exits, hooks and their rollback, Python conditions, the loop guard, every contract error, step-removal protection |
| `test_appui.py` | UI serving, CSP, traversal attempts, the generated UI, the public contract, UI versioning and rollback, `ui/` checks |
| `test_apptests.py` | the `tests.json` runner: sandbox isolation, failure details, matching, references, sticky roles, contract checks |
| `test_e2e.py` | the real CLI and HTTP servers: tokens, two stateless copies, the proxy surviving a dead copy, reconnecting after a database outage |
| `test_ui.py` | the dashboard: deploys of every file, conflict prompts, sign-in, users, the static check, and DeepSeek's request formats (against a fake DeepSeek) |
| `test_docs.py` | every example in `AGENTS.md` deploys and behaves as documented |
| `test_concurrency.py` | many requests at once: parallel writes and `unique` rules, loading a new version once, time limits on worker threads, a sleeping handler not holding the write lock, a full pool, login bursts, parallel grants, overlapping deploys, a slow request not holding up others, the dashboard during a slow DeepSeek call, the DeepSeek limit, double Start clicks, per-app save locks |
