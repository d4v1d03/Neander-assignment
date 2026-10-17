import json

import pytest

from neai.auth import User
from neai.deploy import deploy
from neai.errors import NeaiError
from neai.inspection import inspect_app
from neai.loader import parse_definition, read_handler_writes
from neai.workflow import evaluate

from conftest import APPS, err, ok

RITA = User("rita", ("recruiter",))       # recruiter
IVAN = User("ivan", ("interviewer",))     # interviewer


@pytest.fixture
def hiring(deployed):
    rt = deployed("hiring")
    job1 = ok(rt.call("post_job", RITA, {"title": "Backend SDE1", "level": "SDE1"}))
    job2 = ok(rt.call("post_job", RITA, {"title": "Backend SDE2", "level": "SDE2"}))
    return rt, job1["id"], job2["id"]


def apply(rt, who, job, years):
    return ok(rt.call("apply", who, {"job_id": job, "experience_years": years}))


def passed(rt, who, app_id):
    return ok(rt.call("pass_round", who, {"id": app_id}))


def test_new_applications_start_at_the_first_round(hiring):
    rt, sde1, _ = hiring
    app = apply(rt, "sam", sde1, 1)
    assert app["stage"] == "applied"
    moves = ok(rt.call("history", "sam", {"id": app["id"]}))["moves"]
    assert [(m["action"], m["to"], m["skipped"], m["by"]) for m in moves] == [("start", "applied", [], "sam")]


def test_experienced_sde1_candidates_skip_the_technical_round(hiring):
    rt, sde1, _ = hiring
    app = apply(rt, "sam", sde1, 4)
    passed(rt, RITA, app["id"])                       # applied -> screening
    move = passed(rt, RITA, app["id"])                # screening -> (technical skipped) -> manager
    assert (move["from"], move["to"], move["skipped"]) == ("screening", "manager", ["technical"])
    assert move["record"]["stage"] == "manager"


def test_the_rule_needs_both_conditions(hiring):
    rt, sde1, sde2 = hiring
    junior = apply(rt, "jo", sde1, 1)                 # SDE1, but under 3 years
    senior = apply(rt, "max", sde2, 9)                # 3+ years, but SDE2
    for app in (junior, senior):
        passed(rt, RITA, app["id"])
        move = passed(rt, RITA, app["id"])
        assert (move["to"], move["skipped"]) == ("technical", [])


def test_each_round_says_who_may_pass_it(hiring):
    rt, sde1, _ = hiring
    app = apply(rt, "jo", sde1, 1)
    e = err(rt.call("pass_round", IVAN, {"id": app["id"]}))   # "applied" is the recruiter's round
    assert e["code"] == "WORKFLOW_FORBIDDEN"
    assert e["details"]["step"] == "applied" and e["details"]["required_roles"] == ["recruiter"]
    passed(rt, RITA, app["id"])
    passed(rt, RITA, app["id"])                                # now at "technical"
    assert err(rt.call("pass_round", RITA, {"id": app["id"]}))["code"] == "WORKFLOW_FORBIDDEN"
    assert passed(rt, IVAN, app["id"])["to"] == "manager"
    assert err(rt.call("pass_round", "sam", {"id": app["id"]}))["code"] == "FORBIDDEN"  # the handler's own rule
    assert rt.store.logs(limit=1)[0]["level"] == "warn"


def test_the_whole_path_to_hired_runs_the_offer_hook_once(hiring):
    rt, sde1, _ = hiring
    app = apply(rt, "sam", sde1, 6)
    for who in (RITA, RITA, IVAN):                    # applied, screening (technical skipped), manager
        passed(rt, who, app["id"])
    [mine] = ok(rt.call("my_applications", "sam"))
    assert mine["stage"] == "hr" and mine["offer"]["salary"] == 1_320_000   # SDE1 band + 10% for 5+ years
    final = passed(rt, RITA, app["id"])
    assert (final["to"], final["finished"]) == ("hired", True)
    e = err(rt.call("pass_round", RITA, {"id": app["id"]}))
    assert e["code"] == "WORKFLOW_FINISHED" and e["details"]["step"] == "hired"
    moves = ok(rt.call("history", "sam", {"id": app["id"]}))["moves"]
    assert [(m["action"], m.get("from"), m["to"], m["skipped"], m["by"]) for m in moves] == [
        ("start", None, "applied", [], "sam"),
        ("advance", "applied", "screening", [], "rita"),
        ("advance", "screening", "manager", ["technical"], "rita"),
        ("advance", "manager", "hr", [], "ivan"),
        ("advance", "hr", "hired", [], "rita"),
    ]
    report = inspect_app(rt.db.url, "hiring")["workflows"]["hiring"]
    assert report["records_per_step"]["hired"] == 1 and report["steps"][2]["skip_if"]["level"] == "SDE1"


def test_exits_end_the_workflow_early(hiring):
    rt, sde1, _ = hiring
    a = apply(rt, "sam", sde1, 1)
    b = apply(rt, "jo", sde1, 1)
    move = ok(rt.call("reject", RITA, {"id": a["id"], "note": "not this time"}))
    assert (move["to"], move["finished"]) == ("rejected", True)
    assert ok(rt.call("history", RITA, {"id": a["id"]}))["moves"][-1]["note"] == "not this time"
    assert err(rt.call("withdraw", "sam", {"id": b["id"]}))["code"] == "APPLICATION_NOT_FOUND"  # not sam's
    assert ok(rt.call("withdraw", "jo", {"id": b["id"]}))["to"] == "withdrawn"
    assert err(rt.call("reject", RITA, {"id": b["id"]}))["code"] == "WORKFLOW_FINISHED"
    assert [x["id"] for x in ok(rt.call("list_applications", IVAN, {"stage": "withdrawn"}))] == [b["id"]]


PROBE_APP = {
    "name": "probe",
    "auth": {"roles": ["boss"]},
    "collections": {"tasks": {"title": {"type": "string"}, "points": {"type": "integer", "default": 0},
                              "vip": {"type": "boolean", "required": False},
                              "step": {"type": "string", "required": False}}},
    "handlers": {"add": "write", "move": "write", "set_step": "write", "peek": "read", "sneaky_move": "read"},
}
PROBE_CODE = '''
def add(ctx, args):
    return ctx.db.insert("tasks", args)

def move(ctx, args):
    return ctx.workflow.advance("flow", args["id"])

def set_step(ctx, args):
    return ctx.db.update("tasks", args["id"], {"step": "done"})

def peek(ctx, args):
    return ctx.workflow.state("flow", args["id"])

def sneaky_move(ctx, args):
    return ctx.workflow.advance("flow", args["id"])
'''


def probe(tmp_path, deployed, steps, code="", exits=()):
    src = tmp_path / "probe"
    src.mkdir(exist_ok=True)
    (src / "app.json").write_text(json.dumps(PROBE_APP))
    (src / "handlers.py").write_text(PROBE_CODE)
    (src / "workflows.json").write_text(json.dumps(
        {"workflows": {"flow": {"collection": "tasks", "field": "step", "steps": steps, "exits": list(exits)}}}))
    if code:
        (src / "workflows.py").write_text(code)
    return deployed(src)


def test_the_step_field_belongs_to_the_platform(tmp_path, deployed):
    rt = probe(tmp_path, deployed, ["todo", "done"])
    e = err(rt.call("add", "a", {"title": "x", "step": "done"}))
    assert e["code"] == "WORKFLOW_FIELD_MANAGED" and "ctx.workflow.advance" in e["hint"]
    task = ok(rt.call("add", "a", {"title": "x"}))
    assert err(rt.call("set_step", "a", {"id": task["id"]}))["code"] == "WORKFLOW_FIELD_MANAGED"
    assert ok(rt.call("peek", "a", {"id": task["id"]}))["step"] == "todo"


def test_read_handlers_cant_move_records(tmp_path, deployed):
    rt = probe(tmp_path, deployed, ["todo", "done"])
    task = ok(rt.call("add", "a", {"title": "x"}))
    e = err(rt.call("sneaky_move", "a", {"id": task["id"]}))
    assert e["code"] == "READ_ONLY_VIOLATION" and "ctx.workflow.advance" in e["message"]
    assert e["details"]["file"] == "handlers.py"
    assert ok(rt.call("peek", "a", {"id": task["id"]}))["step"] == "todo"
    warnings = read_handler_writes(PROBE_CODE, parse_definition({"app.json": json.dumps(PROBE_APP)}))
    assert any("sneaky_move" in w and "workflow.advance" in w for w in warnings)


def test_a_failing_hook_undoes_the_whole_move(tmp_path, deployed):
    code = "def boom(ctx, task):\n    ctx.db.insert('tasks', {'title': 'side effect'})\n    raise RuntimeError('hook broke')\n"
    rt = probe(tmp_path, deployed, ["todo", {"name": "doing", "on_enter": "boom"}, "done"], code)
    task = ok(rt.call("add", "a", {"title": "x"}))
    e = err(rt.call("move", "a", {"id": task["id"]}))
    assert e["code"] == "HANDLER_EXCEPTION"
    assert any(line.startswith("workflows.py line 3") for line in e["details"]["traceback"])
    assert ok(rt.call("peek", "a", {"id": task["id"]}))["step"] == "todo"
    assert rt.store.count_records("tasks") == 1
    assert len(rt.store.workflow_history("flow", task["id"])) == 1  # only the start


def test_rules_can_call_python_that_only_reads(tmp_path, deployed):
    code = '''
def big(ctx, task):
    return task["points"] >= ctx.db.count("tasks")

def chatty(ctx, task):
    return "yes"

def writer(ctx, task):
    ctx.db.insert("tasks", {"title": "nope"})
    return True
'''
    steps = ["todo", {"name": "review", "skip_if": {"any": [{"function": "big"}, {"vip": True}]}}, "done"]
    rt = probe(tmp_path, deployed, steps, code)
    small = ok(rt.call("add", "a", {"title": "small", "points": 0}))
    assert ok(rt.call("move", "a", {"id": small["id"]}))["to"] == "review"
    vip = ok(rt.call("add", "a", {"title": "vip", "vip": True}))
    assert ok(rt.call("move", "a", {"id": vip["id"]}))["skipped"] == ["review"]
    large = ok(rt.call("add", "a", {"title": "large", "points": 99}))
    assert ok(rt.call("move", "a", {"id": large["id"]}))["skipped"] == ["review"]

    rt2 = probe(tmp_path, deployed, ["todo", {"name": "review", "skip_if": {"function": "chatty"}}, "done"], code)
    t = ok(rt2.call("add", "a", {"title": "x"}))
    assert err(rt2.call("move", "a", {"id": t["id"]}))["code"] == "WORKFLOW_BAD_CONDITION"
    rt3 = probe(tmp_path, deployed, ["todo", {"name": "review", "skip_if": {"function": "writer"}}, "done"], code)
    t = ok(rt3.call("add", "a", {"title": "x"}))
    e = err(rt3.call("move", "a", {"id": t["id"]}))
    assert e["code"] == "READ_ONLY_VIOLATION" and e["details"]["file"] == "workflows.py"


def test_skip_rules_apply_at_the_start_too(tmp_path, deployed):
    rt = probe(tmp_path, deployed, [{"name": "triage", "skip_if": {"vip": True}}, "work", "done"])
    assert ok(rt.call("add", "a", {"title": "vip", "vip": True}))["step"] == "work"
    assert ok(rt.call("add", "a", {"title": "normal"}))["step"] == "triage"


def test_a_runaway_chain_of_moves_is_stopped(tmp_path, deployed):
    code = "def again(ctx, task):\n    ctx.workflow.advance('flow', task['id'])\n"
    steps = ["s0"] + [{"name": f"s{i}", "on_enter": "again"} for i in range(1, 60)] + ["end"]
    rt = probe(tmp_path, deployed, steps, code)
    task = ok(rt.call("add", "a", {"title": "x"}))
    assert err(rt.call("move", "a", {"id": task["id"]}))["code"] == "WORKFLOW_LOOP"
    assert ok(rt.call("peek", "a", {"id": task["id"]}))["step"] == "s0"


def test_rule_semantics():
    def check(cond, record):
        return evaluate(cond, record, lambda name, rec: False)

    assert check(("compare", "x", "!=", 1), {}) and not check(("compare", "x", "==", 1), {})
    assert check(("compare", "x", "in", [1, 2]), {"x": 2}) and check(("compare", "x", "not_in", [1]), {})
    assert check(("not", ("compare", "x", ">", 5)), {"x": 5})
    assert check(("any", (("compare", "x", "<", 0), ("compare", "y", "==", "a"))), {"x": 1, "y": "a"})


def contract_errors(steps=None, **flow):
    files = {"app.json": json.dumps(PROBE_APP), "handlers.py": "",
             "workflows.json": json.dumps({"workflows": {"flow": {"collection": "tasks", "field": "step",
                                                                  "steps": steps or ["a", "b"], **flow}}})}
    with pytest.raises(NeaiError) as e:
        parse_definition(files)
    assert e.value.code == "WORKFLOWS_INVALID"
    return {x["path"]: x["problem"] for x in e.value.details["errors"]}


def test_contract_mistakes_are_reported_with_paths():
    assert "no collection" in contract_errors(collection="nope")["workflows.flow.collection"]
    assert "must be a string" in contract_errors(field="points")["workflows.flow.field"]
    assert "unknown role" in contract_errors(steps=[{"name": "a", "by": "ceo"}, "b"])["workflows.flow.steps[0].by"]
    errors = contract_errors(steps=["a", {"name": "b", "skip_if": {"vip": True}}])
    assert "can't be skipped" in errors["workflows.flow.steps[1].skip_if"]
    errors = contract_errors(steps=[{"name": "a", "skip_if": {"pointz": 1, "points": {"~": 1}, "title": {">": 2}}}, "b"])
    assert "unknown field" in errors["workflows.flow.steps[0].skip_if.pointz"]
    assert "unknown operator" in errors["workflows.flow.steps[0].skip_if.points.~"]
    assert "doesn't fit" in errors["workflows.flow.steps[0].skip_if.title.>"]
    assert "twice" in contract_errors(steps=["a", "a", "b"])["workflows.flow.steps[1]"]
    assert "already a step" in contract_errors(exits=["a"])["workflows.flow.exits[0]"]
    assert "at least two" in contract_errors(steps=["only"])["workflows.flow.steps"]
    assert "unknown key" in contract_errors(stepz=[])["workflows.flow.stepz"]


def test_named_functions_must_exist(tmp_path, deployed, database):
    src = tmp_path / "probe"
    src.mkdir()
    (src / "app.json").write_text(json.dumps(PROBE_APP))
    (src / "handlers.py").write_text(PROBE_CODE)
    (src / "workflows.json").write_text(json.dumps({"workflows": {"flow": {
        "collection": "tasks", "field": "step", "steps": [{"name": "a", "on_enter": "hello"}, "b"]}}}))
    assert deploy(database, src)["error"]["code"] == "WORKFLOW_FUNCTION_MISSING"
    (src / "workflows.py").write_text("def hello(ctx):\n    pass\n")
    assert deploy(database, src)["error"]["code"] == "WORKFLOW_FUNCTION_SIGNATURE"
    (src / "workflows.py").write_text("def hello(ctx, task):\n    pass\n\ndef unused(ctx, task):\n    pass\n")
    result = deploy(database, src)
    assert result["status"] == "activated" and any("unused" in w for w in result["warnings"])


def test_deploy_refuses_to_strand_records_in_a_removed_step(tmp_path, deployed, database):
    rt = probe(tmp_path, deployed, ["todo", "doing", "done"])
    task = ok(rt.call("add", "a", {"title": "x"}))
    ok(rt.call("move", "a", {"id": task["id"]}))                 # now in "doing"
    flows = tmp_path / "probe" / "workflows.json"
    flows.write_text(flows.read_text().replace('"doing"', '"in_progress"'))
    result = deploy(database, tmp_path / "probe")
    assert result["error"]["code"] == "WORKFLOW_INCOMPATIBLE"
    assert result["error"]["details"]["examples"] == [{"id": task["id"], "step": "doing"}]
    flows.write_text(flows.read_text().replace('"todo"', '"backlog"'))  # nobody is in "todo": renaming is fine...
    flows.write_text(flows.read_text().replace('"in_progress"', '"doing"'))
    assert deploy(database, tmp_path / "probe")["status"] == "activated"


def test_adding_a_workflow_puts_existing_records_in_the_first_step(tmp_path, deployed, database):
    src = tmp_path / "probe"
    src.mkdir()
    (src / "app.json").write_text(json.dumps(PROBE_APP))
    (src / "handlers.py").write_text(PROBE_CODE.replace('return ctx.workflow.state("flow", args["id"])',
                                                       'return ctx.db.get("tasks", args["id"])'))
    rt = deployed(src)
    old = ok(rt.call("add", "a", {"title": "before workflows"}))
    (src / "handlers.py").write_text(PROBE_CODE)
    (src / "workflows.json").write_text(json.dumps(
        {"workflows": {"flow": {"collection": "tasks", "field": "step", "steps": ["todo", "done"]}}}))
    result = deploy(database, src)
    assert result["status"] == "activated" and result["filled_defaults"] == {"tasks.step": 1}
    assert ok(rt.call("peek", "a", {"id": old["id"]}))["step"] == "todo"
    assert ok(rt.call("move", "a", {"id": old["id"]}))["to"] == "done"


def test_the_hiring_app_ships_valid(database):
    assert deploy(database, APPS / "hiring", dry_run=True)["status"] == "valid"


def test_user_is_not_a_role_and_the_error_says_what_to_do():
    errors = contract_errors(steps=[{"name": "a", "by": "user"}, "b"])
    assert "leave \"by\" out" in errors["workflows.flow.steps[0].by"]


def test_state_says_who_may_move_the_record_on(hiring):
    rt, sde1, _ = hiring
    app = apply(rt, "sam", sde1, 1)
    from neai.handle import Context, DataHandle
    from neai.workflow import WorkflowHandle

    loaded = rt.refresh()
    for user, can in ((RITA, True), (IVAN, False)):
        db = DataHandle(rt.store, loaded.manifest, readonly=True, handler="t")
        ctx = Context(user, db, "t", "hiring", loaded.version)
        state = WorkflowHandle(ctx, db, loaded.manifest, loaded.flows).state("hiring", app["id"])
        assert (state["step"], state["by"], state["can_advance"]) == ("applied", ["recruiter"], can)


def test_deploy_warns_about_workflow_mistakes_it_can_see_in_the_code(database, tmp_path):
    code = PROBE_CODE + '''
def bad_set(ctx, args):
    return ctx.db.update("tasks", args["id"], {"step": "done"})

def bad_name(ctx, args):
    return ctx.workflow.advance("flwo", args["id"])

def bad_exit(ctx, args):
    return ctx.workflow.exit("flow", args["id"], "cancelled")
'''
    manifest = {**PROBE_APP, "handlers": {**PROBE_APP["handlers"], "bad_set": "write", "bad_name": "write", "bad_exit": "write"}}
    src = tmp_path / "probe"
    src.mkdir()
    (src / "app.json").write_text(json.dumps(manifest))
    (src / "handlers.py").write_text(code)
    (src / "workflows.json").write_text(json.dumps({"workflows": {"flow": {
        "collection": "tasks", "field": "step", "steps": ["todo", "done"], "exits": ["dropped"]}}}))
    warnings = deploy(database, src)["warnings"]
    assert any("sets 'tasks.step'" in w and "WORKFLOW_FIELD_MANAGED" in w for w in warnings)
    assert any("names no workflow" in w and "'flwo'" in w for w in warnings)
    assert any("'cancelled' isn't an exit" in w for w in warnings)
