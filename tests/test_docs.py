import re

from neai.auth import Accounts
from neai.inspection import inspect_app
from neai.schema import parse_manifest

from conftest import REPO, err, ok, write_app


def test_the_example_app_in_agents_md_works_as_documented(deployed, tmp_path, database):
    text = (REPO / "AGENTS.md").read_text()
    manifest = re.search(r"## 3\. app\.json.*?```json\n(.*?)```", text, re.S).group(1)
    code = re.search(r"## 4\. handlers\.py.*?```python\n(.*?)```", text, re.S).group(1)
    rt = deployed(write_app(tmp_path / "todo", manifest, code))

    alice = rt.auth("signup", {"username": "alice", "password": "alice-password-1"})["token"]
    item = ok(rt.call("add_item", args={"text": "buy milk"}, token=alice))
    assert item == {"id": 1, "owner": "alice", "text": "buy milk", "done": False}
    assert err(rt.call("add_item", args={"text": "buy milk"}, token=alice))["code"] == "UNIQUE_VIOLATION"
    assert ok(rt.call("stats")) == {"items": 1}  # public: no login
    assert err(rt.call("list_items"))["code"] == "AUTH_REQUIRED"
    assert err(rt.call("purge_done", token=alice))["code"] == "FORBIDDEN"
    Accounts(rt.store).grant(parse_manifest(rt.store.get_version(1)["manifest"]), "alice", "admin")
    assert ok(rt.call("purge_done", token=alice)) == {"deleted": 0}
    assert inspect_app(database, "todo")["auth"] == {"signup": "open", "roles": ["admin"], "users": 1}


def test_the_workflow_example_in_agents_md_works_as_documented(deployed, tmp_path):
    from neai.auth import User

    section = re.search(r"## 5\. Workflows.*?## 6\.", (REPO / "AGENTS.md").read_text(), re.S).group(0)
    workflows = re.search(r"```json\n(.*?)```", section, re.S).group(1)
    hook, approve = re.findall(r"```python\n(.*?)```", section, re.S)[:2]
    manifest = {
        "name": "claims", "auth": {"roles": ["manager", "finance"]},
        "collections": {"claims": {"employee": {"type": "string"}, "amount": {"type": "number"},
                                   "step": {"type": "string", "required": False}},
                        "payouts": {"claim_id": {"type": "integer"}, "employee": {"type": "string"},
                                    "amount": {"type": "number"}}},
        "handlers": {"submit": {"kind": "write", "args": {"amount": {"type": "number"}}},
                     "approve": {"kind": "write", "access": ["manager", "finance"],
                                 "args": {"id": {"type": "integer"}, "note": {"type": "string", "required": False}}},
                     "payouts": "read"},
    }
    code = ("from neai import AppError\n\n" + approve + "\n\ndef submit(ctx, args):\n"
            "    return ctx.db.insert('claims', {'employee': ctx.caller, 'amount': args['amount']})\n\n"
            "def payouts(ctx, args):\n    return ctx.db.find('payouts')\n")
    src = write_app(tmp_path / "claims", manifest, code)
    (src / "workflows.json").write_text(workflows)
    (src / "workflows.py").write_text(hook)
    rt = deployed(src)
    manager, finance = User("mia", ("manager",)), User("fin", ("finance",))

    small = ok(rt.call("submit", "emp", {"amount": 50}))
    big = ok(rt.call("submit", "emp", {"amount": 500}))
    assert small["step"] == big["step"] == "manager_review"
    move = ok(rt.call("approve", manager, {"id": small["id"]}))
    assert (move["to"], move["skipped"]) == ("payout", ["finance_review"])
    assert ok(rt.call("payouts", "emp")) == [{"id": 3, "claim_id": small["id"], "employee": "emp", "amount": 50}]
    assert ok(rt.call("approve", manager, {"id": big["id"]}))["to"] == "finance_review"
    assert err(rt.call("approve", manager, {"id": big["id"]}))["code"] == "WORKFLOW_FORBIDDEN"
    assert ok(rt.call("approve", finance, {"id": big["id"]}))["to"] == "payout"
    assert ok(rt.call("approve", finance, {"id": big["id"]}))["finished"] is True


def test_the_ui_example_in_agents_md_only_calls_real_handlers(database, app_copy):
    from neai.deploy import deploy

    text = (REPO / "AGENTS.md").read_text()
    page = re.search(r"## 6\. A UI for the app.*?```html\n(.*?)```", text, re.S).group(1)
    src = app_copy("notes")
    (src / "ui").mkdir()
    (src / "ui" / "index.html").write_text(page)
    result = deploy(database, src)
    assert result["status"] == "activated" and result["warnings"] == []


def test_the_tests_json_example_in_agents_md_passes_against_notes(database, app_copy):
    from neai.deploy import deploy

    text = (REPO / "AGENTS.md").read_text()
    example = re.search(r"## 7\. Example tests.*?```json\n(.*?)```", text, re.S).group(1)
    src = app_copy("notes")
    (src / "tests.json").write_text(example)
    assert deploy(database, src)["tests"] == {"total": 4, "passed": 4}
