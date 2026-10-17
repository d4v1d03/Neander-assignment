"""Workflows: step-by-step processes that a record goes through, such as a job application.

workflows.json is the contract (steps, rules, who may move each step); workflows.py holds the
optional Python it names (hooks and complex conditions). A record's current step lives in one of
its own fields, which only the platform may change: handlers move records with ctx.workflow.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable

from .errors import NeaiError
from .handle import Context, DataHandle
from .schema import ACCESS_LEVELS, IDENT_RE, Field, Manifest, json_type, type_problem

WORKFLOWS_FILE = "workflows.json"
WORKFLOWS_CODE = "workflows.py"
MAX_MOVES_PER_CALL = 50
MAX_NOTE_LENGTH = 1000

TOP_KEYS = {"workflows"}
WORKFLOW_KEYS = {"collection", "field", "steps", "exits", "description"}
STEP_KEYS = {"name", "by", "skip_if", "on_enter", "description"}
OPERATORS = ("==", "!=", ">", ">=", "<", "<=", "in", "not_in")
ORDERED = {">", ">=", "<", "<="}
LOGIC_KEYS = {"all", "any", "not", "function"}


@dataclass(frozen=True)
class Step:
    name: str
    by: tuple[str, ...] = ()          # roles that may move a record out of this step; () = whoever the handler allows
    skip_if: Any = None               # a compiled condition; the step is skipped when it holds
    skip_rule: Any = None             # the same condition, as written (for display)
    on_enter: str | None = None       # a function in workflows.py, run when a record lands here

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.by:
            out["by"] = list(self.by)
        if self.skip_rule is not None:
            out["skip_if"] = self.skip_rule
        if self.on_enter:
            out["on_enter"] = self.on_enter
        return out


@dataclass(frozen=True)
class Workflow:
    name: str
    collection: str
    field: str
    steps: tuple[Step, ...]
    exits: tuple[str, ...] = ()
    description: str = ""

    @property
    def step_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps)

    def is_finished(self, step: Any) -> bool:
        return step == self.steps[-1].name or step in self.exits

    def functions(self) -> set[str]:
        names = {s.on_enter for s in self.steps if s.on_enter}
        for s in self.steps:
            names |= _condition_functions(s.skip_if)
        return names

    def summary(self) -> dict[str, Any]:
        # What stored data depends on: if this changes, deploy re-checks every record's step.
        return {"collection": self.collection, "field": self.field, "steps": list(self.step_names),
                "exits": list(self.exits)}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"collection": self.collection, "field": self.field,
                               "steps": [s.to_dict() for s in self.steps], "exits": list(self.exits)}
        if self.description:
            out["description"] = self.description
        return out


def parse_workflows(text: str | None, manifest: Manifest) -> dict[str, Workflow]:
    if text is None:
        return {}
    try:
        raw = json.loads(text)
    except ValueError as e:
        line, col = getattr(e, "lineno", None), getattr(e, "colno", None)
        raise NeaiError(
            "WORKFLOWS_INVALID",
            f"{WORKFLOWS_FILE} is not valid JSON: {getattr(e, 'msg', e)}" + (f" (line {line}, column {col})" if line else ""),
            hint=f"{WORKFLOWS_FILE} must be plain JSON: no comments, no trailing commas",
            details={"file": WORKFLOWS_FILE, "line": line, "column": col},
        ) from None

    errors: list[dict[str, str]] = []

    def bad(path: str, problem: str) -> None:
        errors.append({"path": path, "problem": problem})

    workflows: dict[str, Workflow] = {}
    if not isinstance(raw, dict):
        bad("$", f'{WORKFLOWS_FILE} must contain an object like {{"workflows": {{...}}}}')
    else:
        for key in sorted(set(raw) - TOP_KEYS):
            bad(key, "unknown key (allowed: workflows)")
        flows = raw.get("workflows", {})
        if not isinstance(flows, dict):
            bad("workflows", "must be an object mapping workflow names to workflows")
            flows = {}
        used: dict[str, str] = {}
        for wname, spec in flows.items():
            wf = _parse_workflow(wname, spec, manifest, bad)
            if wf is None:
                continue
            if wf.collection in used:
                bad(f"workflows.{wname}.collection",
                    f"'{wf.collection}' already has the workflow '{used[wf.collection]}' (one per collection)")
                continue
            used[wf.collection] = wname
            workflows[wname] = wf
    if errors:
        first = errors[0]
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        raise NeaiError("WORKFLOWS_INVALID", f"{WORKFLOWS_FILE}: {first['path']}: {first['problem']}{more}",
                        hint="fix every entry in details.errors, then deploy again",
                        details={"file": WORKFLOWS_FILE, "errors": errors})
    return workflows


def _parse_workflow(wname: str, spec: Any, manifest: Manifest, bad: Callable[[str, str], None]) -> Workflow | None:
    wpath = f"workflows.{wname}"
    if not IDENT_RE.match(wname):
        bad(wpath, "workflow names are lowercase identifiers (a-z, 0-9, _), starting with a letter")
    if not isinstance(spec, dict):
        bad(wpath, 'must be an object like {"collection": ..., "field": ..., "steps": [...]}')
        return None
    for key in sorted(set(spec) - WORKFLOW_KEYS):
        bad(f"{wpath}.{key}", f"unknown key (allowed: {', '.join(sorted(WORKFLOW_KEYS))})")

    collection = spec.get("collection")
    fields = manifest.collections.get(collection) if isinstance(collection, str) else None
    if fields is None:
        bad(f"{wpath}.collection", f"no collection {collection!r} in app.json "
                                   f"(collections: {', '.join(manifest.collections) or 'none'})")
        return None
    field = spec.get("field")
    fspec = fields.get(field) if isinstance(field, str) else None
    if fspec is None:
        bad(f"{wpath}.field", f"'{collection}' has no field {field!r}; declare the field that holds the step in "
                              'app.json, e.g. "stage": {"type": "string", "required": false}')
    elif fspec.type != "string":
        bad(f"{wpath}.field", f"'{field}' is a {fspec.type}; the field that holds the step must be a string")
    elif fspec.default is not None:
        bad(f"{wpath}.field", f"'{field}' has a default in app.json; remove it: the workflow sets this field")

    raw_steps = spec.get("steps")
    steps: list[Step] = []
    if not isinstance(raw_steps, list) or len(raw_steps) < 2:
        bad(f"{wpath}.steps", "must list at least two steps (where records start, and where they finish)")
        raw_steps = []
    for i, raw_step in enumerate(raw_steps):
        step = _parse_step(raw_step, f"{wpath}.steps[{i}]", fields, manifest, bad)
        if step is None:
            continue
        if step.name in (s.name for s in steps):
            bad(f"{wpath}.steps[{i}]", f"the step '{step.name}' is listed twice")
            continue
        steps.append(step)
    if steps and len(steps) == len(raw_steps) and steps[-1].skip_if is not None:
        bad(f"{wpath}.steps[{len(steps) - 1}].skip_if", "the last step is where records finish, so it can't be skipped")

    raw_exits = spec.get("exits", [])
    exits: list[str] = []
    if not isinstance(raw_exits, list):
        bad(f"{wpath}.exits", 'must be a list of names, e.g. ["rejected", "withdrawn"]')
        raw_exits = []
    for i, name in enumerate(raw_exits):
        epath = f"{wpath}.exits[{i}]"
        if not isinstance(name, str) or not IDENT_RE.match(name):
            bad(epath, "exit names are lowercase identifiers")
        elif name in exits or name in (s.name for s in steps):
            bad(epath, f"'{name}' is already a step or an exit")
        else:
            exits.append(name)

    description = spec.get("description", "")
    if not isinstance(description, str):
        bad(f"{wpath}.description", "must be a string")
        description = ""
    if not steps:
        return None
    return Workflow(name=wname, collection=collection, field=field if isinstance(field, str) else "",
                    steps=tuple(steps), exits=tuple(exits), description=description)


def _parse_step(raw: Any, path: str, fields: dict[str, Field], manifest: Manifest,
                bad: Callable[[str, str], None]) -> Step | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        bad(path, 'must be a step name or an object like {"name": "screening", "by": "recruiter"}')
        return None
    for key in sorted(set(raw) - STEP_KEYS):
        bad(f"{path}.{key}", f"unknown key (allowed: {', '.join(sorted(STEP_KEYS))})")
    name = raw.get("name")
    if not isinstance(name, str) or not IDENT_RE.match(name):
        bad(f"{path}.name", "step names are lowercase identifiers (a-z, 0-9, _), starting with a letter")
        return None
    by: tuple[str, ...] = ()
    if "by" in raw:
        wanted = [raw["by"]] if isinstance(raw["by"], str) else raw["by"]
        if not isinstance(wanted, list) or not wanted or not all(isinstance(r, str) for r in wanted):
            bad(f"{path}.by", "must be a role name or a list of role names")
        elif levels := [r for r in wanted if r in ACCESS_LEVELS]:
            bad(f"{path}.by", f"'{levels[0]}' isn't a role: leave \"by\" out so that anyone the handler lets through "
                              "may move the record on (check ownership in the handler)")
        elif unknown := [r for r in wanted if r not in manifest.auth.roles]:
            bad(f"{path}.by", f"unknown role(s): {', '.join(unknown)} (declare roles in app.json auth.roles; "
                              f"declared: {', '.join(manifest.auth.roles) or 'none'})")
        else:
            by = tuple(dict.fromkeys(wanted))
    skip_if = None
    if "skip_if" in raw:
        skip_if = _compile(raw["skip_if"], f"{path}.skip_if", fields, bad)
    on_enter = raw.get("on_enter")
    if on_enter is not None and (not isinstance(on_enter, str) or not IDENT_RE.match(on_enter)):
        bad(f"{path}.on_enter", "must be the name of a function in workflows.py")
        on_enter = None
    return Step(name=name, by=by, skip_if=skip_if, skip_rule=raw.get("skip_if"), on_enter=on_enter)


def _compile(raw: Any, path: str, fields: dict[str, Field], bad: Callable[[str, str], None]) -> Any:
    """Turn a rule as written into a small tree, checking every field name and value type now."""
    example = '{"level": "SDE1", "experience_years": {">=": 3}}'
    if isinstance(raw, dict) and len(raw) == 1 and next(iter(raw)) in LOGIC_KEYS:
        key, value = next(iter(raw.items()))
        if key in ("all", "any"):
            if not isinstance(value, list) or not value:
                bad(f"{path}.{key}", "must be a non-empty list of conditions")
                return None
            return (key, tuple(_compile(c, f"{path}.{key}[{i}]", fields, bad) for i, c in enumerate(value)))
        if key == "not":
            return ("not", _compile(value, f"{path}.not", fields, bad))
        if not isinstance(value, str) or not IDENT_RE.match(value):
            bad(f"{path}.function", "must be the name of a function in workflows.py")
            return None
        return ("function", value)
    if not isinstance(raw, dict) or not raw:
        bad(path, f"must be an object such as {example}")
        return None
    parts = []
    for fname, expected in raw.items():
        fpath = f"{path}.{fname}"
        if fname in LOGIC_KEYS:
            bad(fpath, f"'{fname}' must be the only key in its object, e.g. {{\"{fname}\": [...]}}")
            continue
        spec = fields.get(fname)
        if spec is None:
            bad(fpath, f"unknown field (fields: {', '.join(fields)})")
            continue
        if spec.type in ("list", "object"):
            bad(fpath, f"'{fname}' is a {spec.type}; rules compare string, integer, number and boolean fields")
            continue
        ops = expected if isinstance(expected, dict) else {"==": expected}
        if not ops:
            bad(fpath, 'needs a value or an operator, e.g. {">=": 3}')
        for op, value in ops.items():
            opath = f"{fpath}.{op}" if isinstance(expected, dict) else fpath
            if op not in OPERATORS:
                bad(opath, f"unknown operator (use one of: {', '.join(OPERATORS)})")
                continue
            if op in ("in", "not_in"):
                if not isinstance(value, list) or not value:
                    bad(opath, "needs a non-empty list of values")
                    continue
                problem = next((p for v in value if (p := type_problem(spec, v))), None)
            elif value is None:
                problem = "can't compare with null (a missing field never equals anything)"
            elif op in ORDERED and spec.type == "boolean":
                problem = "booleans can't be compared with < or >"
            else:
                problem = type_problem(spec, value)
            if problem:
                bad(opath, f"doesn't fit the {spec.type} field '{fname}': {problem}")
                continue
            parts.append(("compare", fname, op, value))
    return parts[0] if len(parts) == 1 else ("all", tuple(parts))


def _condition_functions(cond: Any) -> set[str]:
    if not cond:
        return set()
    if cond[0] == "function":
        return {cond[1]}
    if cond[0] in ("all", "any"):
        return set().union(*(_condition_functions(c) for c in cond[1]))
    if cond[0] == "not":
        return _condition_functions(cond[1])
    return set()


def evaluate(cond: Any, record: dict[str, Any], call: Callable[[str, dict[str, Any]], bool]) -> bool:
    kind = cond[0]
    if kind == "all":
        return all(evaluate(c, record, call) for c in cond[1])
    if kind == "any":
        return any(evaluate(c, record, call) for c in cond[1])
    if kind == "not":
        return not evaluate(cond[1], record, call)
    if kind == "function":
        return call(cond[1], record)
    _, field, op, value = cond
    actual = record.get(field)
    if actual is None:
        return op in ("!=", "not_in")  # a missing value equals nothing
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "in":
        return actual in value
    if op == "not_in":
        return actual not in value
    if op == ">":
        return actual > value
    if op == ">=":
        return actual >= value
    if op == "<":
        return actual < value
    return actual <= value


def attach(manifest: Manifest, workflows: dict[str, Workflow]) -> Manifest:
    """The manifest the platform runs: with the workflows, and each workflow's field starting at its first step.

    Giving the field a default reuses the deploy machinery for defaults: new records start at the
    first step, and records that existed before the workflow was added join it there.
    """
    if not workflows:
        return manifest
    collections = {c: dict(fields) for c, fields in manifest.collections.items()}
    for wf in workflows.values():
        spec = collections[wf.collection][wf.field]
        collections[wf.collection][wf.field] = dataclasses.replace(spec, default=wf.steps[0].name)
    return dataclasses.replace(manifest, collections=collections, workflows=workflows)


def check_functions(module: ModuleType | None, workflows: dict[str, Workflow]) -> list[str]:
    needed = sorted(set().union(*(wf.functions() for wf in workflows.values()))) if workflows else []
    if needed and module is None:
        raise NeaiError("WORKFLOW_FUNCTION_MISSING",
                        f"{WORKFLOWS_FILE} names {', '.join(needed)}, but there is no {WORKFLOWS_CODE}",
                        hint=f"create {WORKFLOWS_CODE} next to app.json with `def {needed[0]}(ctx, record):`",
                        details={"missing": needed})
    if module is None:
        return []
    missing = [n for n in needed if not callable(getattr(module, n, None))]
    if missing:
        raise NeaiError("WORKFLOW_FUNCTION_MISSING",
                        f"{WORKFLOWS_FILE} names function(s) not defined in {WORKFLOWS_CODE}: {', '.join(missing)}",
                        hint=f"define `def {missing[0]}(ctx, record):` in {WORKFLOWS_CODE}",
                        details={"missing": missing})
    bad_signature = []
    for name in needed:
        try:
            inspect.signature(getattr(module, name)).bind(object(), {})
        except TypeError:
            bad_signature.append(name)
        except ValueError:
            pass
    if bad_signature:
        raise NeaiError("WORKFLOW_FUNCTION_SIGNATURE",
                        f"workflow function(s) can't be called as fn(ctx, record): {', '.join(bad_signature)}",
                        hint="hooks and conditions take two arguments: def name(ctx, record):",
                        details={"functions": bad_signature})
    return [f"function '{n}' in {WORKFLOWS_CODE} isn't used by {WORKFLOWS_FILE} (rename it to _{n} if it's a helper)"
            for n, v in sorted(vars(module).items())
            if inspect.isfunction(v) and v.__module__ == module.__name__ and not n.startswith("_") and n not in needed]


def static_warnings(code: str, label: str, manifest: Manifest) -> list[str]:
    """Workflow mistakes visible in the code itself, reported at deploy instead of at the first call."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    managed = {wf.collection: wf for wf in manifest.workflows.values()}
    names = ", ".join(manifest.workflows) or "none"
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)):
            continue
        method, target, args = node.func.attr, node.func.value.attr, node.args
        first = args[0].value if args and isinstance(args[0], ast.Constant) else None
        where = f"{label} line {node.lineno}"
        if target == "db" and method in ("insert", "update") and first in managed:
            wf = managed[first]
            data = args[1] if method == "insert" and len(args) > 1 else (args[2] if len(args) > 2 else None)
            if isinstance(data, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == wf.field for k in data.keys):
                out.append(f"{where} sets '{wf.collection}.{wf.field}', which workflow '{wf.name}' manages: the call "
                           "will fail with WORKFLOW_FIELD_MANAGED (move records with ctx.workflow.advance or exit)")
        elif target == "workflow" and method in ("advance", "exit", "state", "history") and isinstance(first, str):
            wf = manifest.workflows.get(first)
            if wf is None:
                out.append(f"{where}: ctx.workflow.{method}('{first}', ...) names no workflow (workflows: {names}); "
                           "the call will fail with WORKFLOW_UNKNOWN")
            elif method == "exit":
                to = args[2] if len(args) > 2 else next((k.value for k in node.keywords if k.arg == "to"), None)
                if isinstance(to, ast.Constant) and to.value not in wf.exits:
                    out.append(f"{where}: '{to.value}' isn't an exit of '{wf.name}' (exits: "
                               f"{', '.join(wf.exits) or 'none'}); the call will fail with WORKFLOW_EXIT_UNKNOWN")
    return out


class WorkflowHandle:
    """ctx.workflow: moves records through their workflow, enforcing the contract's rules."""

    def __init__(self, ctx: Context, db: DataHandle, manifest: Manifest, module: ModuleType | None):
        self._ctx = ctx
        self._db = db
        self._manifest = manifest
        self._module = module
        self._moves = 0

    def advance(self, workflow: str, id: int, note: str | None = None) -> dict[str, Any]:
        """Pass the record's current step and move to the next one, skipping steps whose rule holds."""
        wf = self._workflow(workflow)
        self._db._check_writable("advance", wf.collection, call=f"ctx.workflow.advance({workflow!r}, ...)")
        record, step = self._current(wf, id)
        if step.by and not set(self._roles()) & set(step.by):
            raise NeaiError(
                "WORKFLOW_FORBIDDEN",
                f"only {' or '.join(step.by)} can move a record out of '{step.name}'; "
                f"{self._ctx.caller or 'an anonymous caller'} has " + (", ".join(self._roles()) or "no roles"),
                hint=f"the operator grants roles: neai users {self._manifest.name} grant <username> {step.by[0]}",
                details={"workflow": workflow, "id": id, "step": step.name, "required_roles": list(step.by),
                         "your_roles": self._roles()},
            )
        return self._land(wf, record, wf.step_names.index(step.name) + 1, "advance", _check_note(note))

    def exit(self, workflow: str, id: int, to: str, note: str | None = None) -> dict[str, Any]:
        """End the record's workflow early, at one of the declared exits (e.g. "rejected")."""
        wf = self._workflow(workflow)
        self._db._check_writable("exit", wf.collection, call=f"ctx.workflow.exit({workflow!r}, ...)")
        if to not in wf.exits:
            raise NeaiError("WORKFLOW_EXIT_UNKNOWN", f"'{workflow}' has no exit {to!r}",
                            hint=f"declared exits: {', '.join(wf.exits) or 'none'} (add it to {WORKFLOWS_FILE})",
                            details={"workflow": workflow, "exits": list(wf.exits)})
        record, step = self._current(wf, id)
        self._move(wf, record, step.name, to, "exit", [], _check_note(note))
        return self._result(wf, id, step.name, to, [])

    def state(self, workflow: str, id: int) -> dict[str, Any]:
        wf = self._workflow(workflow)
        record = self._record(wf, id)
        step = record.get(wf.field)
        finished = wf.is_finished(step)
        current = None if finished else next((s for s in wf.steps if s.name == step), None)
        by = list(current.by) if current else []
        return {"workflow": workflow, "id": id, "step": step, "finished": finished, "by": by,
                "can_advance": current is not None and (not by or bool(set(by) & set(self._roles()))),
                "steps": list(wf.step_names), "exits": list(wf.exits)}

    def history(self, workflow: str, id: int) -> list[dict[str, Any]]:
        wf = self._workflow(workflow)
        self._record(wf, id)
        return self._db._store.workflow_history(workflow, id)

    def _start(self, collection: str, record: dict[str, Any]) -> dict[str, Any]:
        # Called by ctx.db.insert: a new record enters its workflow at the first step.
        wf = next(w for w in self._manifest.workflows.values() if w.collection == collection)
        return self._land(wf, record, 0, "start", None)["record"]

    def _land(self, wf: Workflow, record: dict[str, Any], index: int, action: str, note: str | None) -> dict[str, Any]:
        came_from = record.get(wf.field) if action != "start" else None
        skipped = []
        while index < len(wf.steps) - 1 and wf.steps[index].skip_if is not None \
                and self._holds(wf, wf.steps[index], record):
            skipped.append(wf.steps[index].name)
            index += 1
        target = wf.steps[index]
        self._move(wf, record, came_from, target.name, action, skipped, note)
        if target.on_enter:
            hook = getattr(self._module, target.on_enter)
            hook(self._ctx, self._db.get(wf.collection, record["id"]))
        return self._result(wf, record["id"], came_from, target.name, skipped)

    def _move(self, wf: Workflow, record: dict[str, Any], came_from: str | None, to: str, action: str,
              skipped: list[str], note: str | None) -> None:
        self._moves += 1
        if self._moves > MAX_MOVES_PER_CALL:
            raise NeaiError("WORKFLOW_LOOP", f"more than {MAX_MOVES_PER_CALL} workflow moves in one call",
                            hint="an on_enter hook probably keeps advancing the record; the call was rolled back",
                            details={"workflow": wf.name, "id": record["id"]})
        data = {k: v for k, v in record.items() if k != "id"}
        data[wf.field] = to
        store = self._db._store
        store.replace_record(wf.collection, record["id"], data)
        record[wf.field] = to
        store.add_workflow_history(wf.name, record["id"], action=action, from_step=came_from, to_step=to,
                                   skipped=skipped, by_user=self._ctx.caller, note=note, version=self._ctx.version)

    def _holds(self, wf: Workflow, step: Step, record: dict[str, Any]) -> bool:
        return evaluate(step.skip_if, record, lambda name, rec: self._call_condition(wf, step, name, rec))

    def _call_condition(self, wf: Workflow, step: Step, name: str, record: dict[str, Any]) -> bool:
        # Conditions only look: they get a read-only ctx, so they can't write or move records.
        ro_db = DataHandle(self._db._store, self._manifest, readonly=True,
                           handler=f"{name} (skip rule of {wf.name}.{step.name})", sources=self._db._sources)
        ro_ctx = Context(None, ro_db, self._ctx.handler, self._ctx.app, self._ctx.version)
        ro_ctx.caller, ro_ctx.user, ro_ctx.logs = self._ctx.caller, self._ctx.user, self._ctx.logs
        ro_ctx.workflow = WorkflowHandle(ro_ctx, ro_db, self._manifest, self._module)
        result = getattr(self._module, name)(ro_ctx, dict(record))
        if ro_db.violations:
            raise ro_db.violations[0]
        if not isinstance(result, bool):
            raise NeaiError("WORKFLOW_BAD_CONDITION", f"{name} returned {json_type(result)}, not true or false",
                            hint=f"a condition in {WORKFLOWS_CODE} must return True or False",
                            details={"workflow": wf.name, "step": step.name, "function": name})
        return result

    def _result(self, wf: Workflow, id: int, came_from: str | None, to: str, skipped: list[str]) -> dict[str, Any]:
        return {"workflow": wf.name, "id": id, "from": came_from, "to": to, "skipped": skipped,
                "finished": wf.is_finished(to), "record": self._db.get(wf.collection, id)}

    def _current(self, wf: Workflow, id: int) -> tuple[dict[str, Any], Step]:
        record = self._record(wf, id)
        current = record.get(wf.field)
        if wf.is_finished(current):
            raise NeaiError("WORKFLOW_FINISHED", f"record {id} has already finished '{wf.name}' (at '{current}')",
                            hint="finished records can't move; check ctx.workflow.state() first",
                            details={"workflow": wf.name, "id": id, "step": current})
        step = next((s for s in wf.steps if s.name == current), None)
        if step is None:
            raise NeaiError("WORKFLOW_STATE_INVALID", f"record {id} is in an unknown step {current!r}",
                            details={"workflow": wf.name, "id": id, "steps": list(wf.step_names)})
        return record, step

    def _record(self, wf: Workflow, id: int) -> dict[str, Any]:
        record = self._db.get(wf.collection, id)
        if record is None:
            raise NeaiError("RECORD_NOT_FOUND", f"no record with id {id!r} in '{wf.collection}'",
                            hint="check the id with ctx.db.get() first, and raise your own AppError if it's missing",
                            details={"collection": wf.collection, "id": id})
        return record

    def _workflow(self, name: str) -> Workflow:
        wf = self._manifest.workflows.get(name) if isinstance(name, str) else None
        if wf is None:
            raise NeaiError("WORKFLOW_UNKNOWN", f"app '{self._manifest.name}' has no workflow {name!r}",
                            hint=f"workflows: {', '.join(self._manifest.workflows) or 'none'} (see {WORKFLOWS_FILE})")
        return wf

    def _roles(self) -> list[str]:
        return list(self._ctx.user["roles"]) if self._ctx.user else []


def _check_note(note: Any) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str) or "\x00" in note or len(note) > MAX_NOTE_LENGTH:
        raise NeaiError("WORKFLOW_BAD_NOTE", f"a note must be a string of at most {MAX_NOTE_LENGTH} characters")
    return note
