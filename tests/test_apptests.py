import json

import psycopg
import pytest

from neai.apptests import _match, _resolve, parse_tests
from neai.deploy import deploy
from neai.errors import NeaiError
from neai.loader import parse_definition
from neai.runtime import Runtime

from conftest import APPS, ok


def test_deploy_runs_the_apps_tests_and_reports_them(database):
    for app, total in (("notes", 8), ("feedback", 8), ("hiring", 14)):
        result = deploy(database, APPS / app)
        assert result["status"] == "activated" and result["tests"] == {"total": total, "passed": total}, result
    assert deploy(database, APPS / "notes")["status"] == "unchanged"
    assert deploy(database, APPS / "notes", dry_run=True)["tests"]["passed"] == 8  # validate always runs them


def test_tests_never_touch_real_data(database, app_copy):
    src = app_copy("notes")
    deploy(database, src)
    rt = Runtime(database, "notes")
    try:
        ok(rt.call("create_note", "alice", {"title": "real note"}))
        (src / "handlers.py").write_text((src / "handlers.py").read_text() + "\n# a change\n")
        # "alice lists her notes" expects exactly one note: it passes because tests start from an empty copy.
        assert deploy(database, src)["tests"]["passed"] == 8
        assert [n["title"] for n in ok(rt.call("list_notes", "alice"))] == ["real note"]
    finally:
        rt.close()
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM neai_apps WHERE name LIKE '%~%'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM neai_records WHERE app = 'notes'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM neai_logs WHERE app LIKE '%~%'").fetchone()[0] == 0


def test_a_change_that_breaks_behaviour_is_rejected_with_expected_and_actual(database, app_copy):
    src = app_copy("hiring")
    assert deploy(database, src)["version"] == 1
    flows = src / "workflows.json"
    flows.write_text(flows.read_text().replace('"experience_years": {">=": 3}', '"experience_years": {">=": 30}'))
    result = deploy(database, src)
    assert result["status"] == "rejected" and result["active_version"] == 1
    e = result["error"]
    assert e["code"] == "TESTS_FAILED"
    assert 'experienced SDE1 candidates skip the technical round' in e["message"]
    assert e["details"]["problems"] == ['result.to: expected "manager", got "technical"',
                                        'result.skipped: expected a list of 1, got []']
    assert e["details"]["passed_before"] == 6 and e["details"]["total"] == 14
    assert e["message"].startswith('tests[6] "experienced SDE1')  # the same tests[i] path TESTS_INVALID uses
    assert e["details"]["test"] == "tests[6]"
    assert deploy(database, src, dry_run=True)["error"]["code"] == "TESTS_FAILED"  # validate runs them too


def test_a_test_that_expects_a_refusal_fails_when_the_call_succeeds(database, app_copy):
    src = app_copy("notes")
    code = src / "handlers.py"
    code.write_text(code.read_text().replace('if note is None or note["owner"] != ctx.caller:', "if note is None:"))
    e = deploy(database, src)["error"]
    assert e["code"] == "TESTS_FAILED" and "bob can't read alice's note" in e["message"]
    assert e["details"]["problems"] == ["expected the error NOTE_NOT_FOUND, but the call succeeded"]


def test_tests_are_checked_against_the_contract():
    manifest = parse_definition({"app.json": (APPS / "hiring" / "app.json").read_text()})

    def errors(*tests):
        with pytest.raises(NeaiError) as e:
            parse_tests(json.dumps({"tests": list(tests)}), manifest)
        assert e.value.code == "TESTS_INVALID"
        return {x["path"]: x["problem"] for x in e.value.details["errors"]}

    assert "no handler" in errors({"call": "hire_everyone"})["tests[0].call"]
    assert "unknown role" in errors({"as": {"user": "x1", "roles": ["ceo"]}, "call": "list_jobs"})["tests[0].as.roles"]
    assert "no earlier test" in errors({"call": "apply", "args": {"job_id": "$job.id"}})["tests[0]"]
    assert "unknown key" in errors({"call": "list_jobs", "expect": {"results": []}})["tests[0].expect.results"]
    assert "both" in errors({"call": "list_jobs", "expect": {"result": [], "error": "X"}})["tests[0].expect"]
    assert "username" in errors({"as": "Not Valid", "call": "list_jobs"})["tests[0].as"]
    assert "no argument 'jobid'" in errors({"call": "apply", "args": {"jobid": 1}})["tests[0].args.jobid"]
    assert "doesn't fit" in errors({"call": "apply", "args": {"job_id": "one"}})["tests[0].args.job_id"]
    # ...unless the test is about bad arguments:
    assert parse_tests(json.dumps({"tests": [{"call": "apply", "args": {"job_id": "one"},
                                               "expect": {"error": "ARGS_INVALID"}}]}), manifest)


def test_matching_rules():
    def problems(expected, actual):
        out = []
        _match(expected, actual, "result", out)
        return out

    assert problems({"a": 1}, {"a": 1, "b": 2}) == []                      # extra keys are fine
    assert problems({"a": 1.0}, {"a": 1}) == []                            # 1 and 1.0 are the same number
    assert problems({"a": {"b": 1}}, {"a": {}}) == ["result.a.b: missing"]
    assert problems([{"x": 1}], [{"x": 1}, {"x": 2}]) == ["result: expected a list of 1, got [{\"x\": 1}, {\"x\": 2}]"]
    assert problems(True, 1) == ["result: expected true, got 1"]            # booleans are strict
    assert problems("a", "b") == ['result: expected "a", got "b"']


def test_references_reach_into_saved_results():
    saved = {"job": {"id": 7, "tags": [{"name": "x"}]}}
    assert _resolve({"id": "$job.id", "first": "$job.tags.0.name", "all": "$job"}, saved) == {
        "id": 7, "first": "x", "all": {"id": 7, "tags": [{"name": "x"}]}}
    assert _resolve("$5 off", saved) == "$5 off"  # not a reference: references start with a name


def test_roles_given_once_stick_for_the_rest_of_the_run(database, app_copy):
    src = app_copy("hiring")
    (src / "tests.json").write_text(json.dumps({"tests": [
        {"as": {"user": "rita", "roles": ["recruiter"]}, "call": "post_job", "args": {"title": "a", "level": "SDE1"}},
        {"as": "rita", "call": "post_job", "args": {"title": "b", "level": "SDE2"}},
        {"call": "list_jobs", "expect": {"result": [{"title": "a"}, {"title": "b"}]}},
    ]}))
    assert deploy(database, src)["tests"] == {"total": 3, "passed": 3}
