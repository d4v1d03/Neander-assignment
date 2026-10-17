"""tests.json: example calls an app ships with, run by every deploy before anything goes live.

The calls run against an empty scratch copy of the app (a sandbox), using the new version's code,
inside one database transaction that is always rolled back. Real data is never touched, and a
failing test rejects the deploy with the expected and actual values, so a coding agent learns at
once that its change broke behaviour, not just that its code imports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .auth import USERNAME_RE, User
from .database import Database
from .errors import NeaiError
from .loader import TESTS_FILE
from .runtime import LoadedVersion, Runtime
from .schema import IDENT_RE, Manifest, json_type, type_problem
from .store import Store

MAX_TESTS = 200
TEST_KEYS = {"name", "as", "call", "args", "save", "expect"}
EXPECT_KEYS = {"ok", "result", "error"}
AS_KEYS = {"user", "roles"}
REF_RE = re.compile(r"^\$([a-z][a-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)$")


@dataclass(frozen=True)
class AppTest:
    number: int                       # 1-based, as people count
    name: str
    user: str | None                  # None: an anonymous call
    roles: tuple[str, ...] | None     # None: whatever roles this user was given earlier in the run
    call: str
    args: Any
    save: str | None
    expect: dict[str, Any]

    @property
    def label(self) -> str:
        # The same tests[i] path that TESTS_INVALID uses, so both point at the same entry.
        return f"tests[{self.number - 1}]" + (f' "{self.name}"' if self.name else f" ({self.call})")


def parse_tests(text: str | None, manifest: Manifest) -> list[AppTest]:
    if text is None:
        return []
    try:
        raw = json.loads(text)
    except ValueError as e:
        line, col = getattr(e, "lineno", None), getattr(e, "colno", None)
        raise NeaiError("TESTS_INVALID",
                        f"{TESTS_FILE} is not valid JSON: {getattr(e, 'msg', e)}" + (f" (line {line}, column {col})" if line else ""),
                        hint=f"{TESTS_FILE} must be plain JSON: no comments, no trailing commas",
                        details={"file": TESTS_FILE, "line": line, "column": col}) from None
    errors: list[dict[str, str]] = []

    def bad(path: str, problem: str) -> None:
        errors.append({"path": path, "problem": problem})

    tests: list[AppTest] = []
    raw_tests = raw.get("tests") if isinstance(raw, dict) else None
    if isinstance(raw, dict):
        for key in sorted(set(raw) - {"tests"}):
            bad(key, "unknown key (allowed: tests)")
    if not isinstance(raw_tests, list):
        bad("tests", 'must be a list of tests like {"as": "alice", "call": "create_note", "args": {...}}')
        raw_tests = []
    if len(raw_tests) > MAX_TESTS:
        bad("tests", f"has {len(raw_tests)} tests; the limit is {MAX_TESTS}")
    saved: set[str] = set()
    for i, spec in enumerate(raw_tests[:MAX_TESTS]):
        test = _parse_test(i, spec, manifest, saved, bad)
        if test is not None:
            tests.append(test)
            if test.save:
                saved.add(test.save)
    if errors:
        first = errors[0]
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise NeaiError("TESTS_INVALID", f"{TESTS_FILE}: {first['path']}: {first['problem']}{more}",
                        hint="fix every entry in details.errors, then deploy again",
                        details={"file": TESTS_FILE, "errors": errors})
    return tests


def _parse_test(i: int, spec: Any, manifest: Manifest, saved: set[str], bad: Any) -> AppTest | None:
    path = f"tests[{i}]"
    if not isinstance(spec, dict):
        bad(path, 'must be an object like {"as": "alice", "call": "create_note", "args": {...}}')
        return None
    for key in sorted(set(spec) - TEST_KEYS):
        bad(f"{path}.{key}", f"unknown key (allowed: {', '.join(sorted(TEST_KEYS))})")
    call = spec.get("call")
    if call not in manifest.handlers:
        bad(f"{path}.call", f"no handler {call!r} in app.json (handlers: {', '.join(manifest.handlers)})")
    user, roles = None, None
    who = spec.get("as")
    if isinstance(who, dict):
        for key in sorted(set(who) - AS_KEYS):
            bad(f"{path}.as.{key}", "unknown key (allowed: user, roles)")
        user, roles = who.get("user"), who.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            bad(f"{path}.as.roles", "must be a list of role names")
            roles = []
        elif unknown := [r for r in roles if r not in manifest.auth.roles]:
            bad(f"{path}.as.roles", f"unknown role(s): {', '.join(unknown)} "
                                    f"(declared in app.json: {', '.join(manifest.auth.roles) or 'none'})")
        roles = tuple(roles)
    elif who is not None:
        user = who
    if user is not None and (not isinstance(user, str) or not USERNAME_RE.match(user)):
        bad(f"{path}.as", 'must be a username (e.g. "alice"), {"user": "rita", "roles": ["recruiter"]}, '
                          "or null for an anonymous call")
        user = None
    args = spec.get("args", {})
    if not isinstance(args, dict):
        bad(f"{path}.args", "must be an object")
        args = {}
    declared = manifest.handler_args.get(call) if isinstance(call, str) else None
    expects_bad_args = isinstance(spec.get("expect"), dict) and spec["expect"].get("error") == "ARGS_INVALID"
    if declared is not None and not expects_bad_args:  # a test may send bad args on purpose, to expect ARGS_INVALID
        for key, value in args.items():
            if key not in declared:
                bad(f"{path}.args.{key}", f"{call} has no argument '{key}' (it takes: {', '.join(declared) or 'none'})")
            elif not (isinstance(value, str) and REF_RE.match(value)) and value is not None \
                    and (problem := type_problem(declared[key], value)):
                bad(f"{path}.args.{key}", f"doesn't fit {call}'s {declared[key].type} argument: {problem}")
    save = spec.get("save")
    if save is not None and (not isinstance(save, str) or not IDENT_RE.match(save)):
        bad(f"{path}.save", "must be a name such as \"note\" (later tests use it as \"$note.id\")")
        save = None
    expect = spec.get("expect", {})
    if not isinstance(expect, dict):
        bad(f"{path}.expect", 'must be an object: {"result": ...}, {"error": "CODE"} or {"ok": false}')
        expect = {}
    for key in sorted(set(expect) - EXPECT_KEYS):
        bad(f"{path}.expect.{key}", "unknown key (allowed: ok, result, error)")
    if "error" in expect and not isinstance(expect["error"], str):
        bad(f"{path}.expect.error", "must be an error code such as \"NOTE_NOT_FOUND\"")
    if "error" in expect and "result" in expect:
        bad(f"{path}.expect", "can't expect both a result and an error")
    for ref in _refs([args, expect.get("result")]):
        if ref not in saved:
            bad(path, f"refers to ${ref}, but no earlier test has \"save\": \"{ref}\"")
    name = spec.get("name", "")
    if not isinstance(name, str):
        bad(f"{path}.name", "must be a string")
        name = ""
    return AppTest(i + 1, name, user, roles, call, args, save, expect)


def run_tests(db: Database, manifest: Manifest, loaded: LoadedVersion, files: dict[str, str]) -> dict[str, Any]:
    tests = parse_tests(files.get(TESTS_FILE), manifest)
    if not tests:
        return {"total": 0, "passed": 0}
    # "~" can't appear in app names, so the sandbox can never be a real app.
    sandbox = Store(db, f"{manifest.name}~tests")
    with db.transaction(rollback=True):
        sandbox.ensure()
        rt = Runtime.sandbox(db, sandbox, loaded)
        saved: dict[str, Any] = {}
        origins: dict[str, AppTest] = {}  # which test saved each name, so failures can say where a $value came from
        roles_of: dict[str, tuple[str, ...]] = {}
        for test in tests:
            _run_one(rt, test, tests, saved, origins, roles_of)
    return {"total": len(tests), "passed": len(tests)}


def _run_one(rt: Runtime, test: AppTest, tests: list[AppTest], saved: dict[str, Any],
             origins: dict[str, AppTest], roles_of: dict[str, tuple[str, ...]]) -> None:
    if test.user is not None and test.roles is not None:
        roles_of[test.user] = test.roles  # roles given once stick to that user for the rest of the run
    user = User(test.user, roles_of.get(test.user, ())) if test.user else None
    try:
        args = _resolve(test.args, saved)
        expected = _resolve(test.expect.get("result"), saved) if "result" in test.expect else None
    except _MissingRef as e:
        _fail(test, tests, [str(e)], args=test.args, response=None, origins=origins)
    response = rt.call(test.call, user, args)
    problems = _verdict(test, expected, response)
    if problems:
        _fail(test, tests, problems, args=args, response=response, origins=origins)
    if test.save:
        saved[test.save] = response.get("result")
        origins[test.save] = test


def _verdict(test: AppTest, expected: Any, response: dict[str, Any]) -> list[str]:
    got = (f"the call failed with {response['error']['code']}: {response['error']['message']}"
           if not response["ok"] else "the call succeeded")
    if "error" in test.expect:
        if response["ok"] or response["error"]["code"] != test.expect["error"]:
            return [f"expected the error {test.expect['error']}, but {got}"]
        return []
    if response["ok"] != test.expect.get("ok", True):
        return [f"expected {'success' if test.expect.get('ok', True) else 'a failure'}, but {got}"]
    problems: list[str] = []
    if "result" in test.expect:
        _match(expected, response.get("result"), "result", problems)
    return problems


def _match(expected: Any, actual: Any, path: str, problems: list[str]) -> None:
    """Objects match if every expected key matches (extra keys are fine); lists must match item by item."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append(f"{path}: expected an object, got {_show(actual)}")
            return
        for key, value in expected.items():
            if key not in actual:
                problems.append(f"{path}.{key}: missing")
            else:
                _match(value, actual[key], f"{path}.{key}", problems)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            problems.append(f"{path}: expected a list of {len(expected)}, got {_show(actual)}")
            return
        for i, (e, a) in enumerate(zip(expected, actual)):
            _match(e, a, f"{path}[{i}]", problems)
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if expected is not actual:
            problems.append(f"{path}: expected {_show(expected)}, got {_show(actual)}")
    elif expected != actual:
        problems.append(f"{path}: expected {_show(expected)}, got {_show(actual)}")


def _fail(test: AppTest, tests: list[AppTest], problems: list[str], *, args: Any, response: Any,
          origins: dict[str, AppTest]) -> None:
    # Where each $value came from. A common mistake is using a record that another user created.
    refs = {}
    for name in sorted(_refs([test.args, test.expect.get("result")])):
        origin = origins.get(name)
        if origin is not None:
            refs[f"${name}"] = {"saved_by": f"tests[{origin.number - 1}]", "as": origin.user}
    other_users = [f"{n} was saved by {r['saved_by']}, which called as {r['as'] or 'nobody'}"
                   for n, r in refs.items() if r["as"] != test.user]
    hint = (f"the deploy was rejected and nothing changed; fix the app (or the test in {TESTS_FILE}) and deploy "
            "again (tests run against an empty copy of the app, in order)")
    if other_users:
        who = test.user or "an anonymous caller"
        hint = (f"this test calls as {who}, but " + "; ".join(other_users) + f": if that record isn't {who}'s, "
                f"the app is right to refuse and the test needs fixing. Otherwise, " + hint)
    raise NeaiError(
        "TESTS_FAILED", f"{test.label} failed: {problems[0]}", hint=hint,
        details={"test": f"tests[{test.number - 1}]", "name": test.name or None, "as": test.user, "call": test.call,
                 "args": args, "refs": refs, "problems": problems, "response": response,
                 "passed_before": test.number - 1, "total": len(tests)},
    )


class _MissingRef(Exception):
    pass


def _resolve(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, str) and (m := REF_RE.match(value)):
        current = saved.get(m.group(1))
        for part in m.group(2).split(".")[1:]:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                raise _MissingRef(f"{value}: the saved result has no '{part}' (it was {_show(current)})")
        return current
    if isinstance(value, dict):
        return {k: _resolve(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, saved) for v in value]
    return value


def _refs(value: Any) -> set[str]:
    if isinstance(value, str) and (m := REF_RE.match(value)):
        return {m.group(1)}
    if isinstance(value, dict):
        return set().union(*(_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_refs(v) for v in value)) if value else set()
    return set()


def _show(value: Any) -> str:
    text = json.dumps(value, default=repr)
    return text if len(text) <= 120 else text[:117] + "..." + f" ({json_type(value)})"
