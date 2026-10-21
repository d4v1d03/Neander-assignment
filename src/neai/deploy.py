from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .database import Database
from .errors import HandlerTimeout, NeaiError
from .loader import (
    HANDLERS_FILE,
    check_handlers,
    code_filename,
    content_hash,
    import_source,
    parse_definition,
    read_definition,
    read_handler_writes,
    ui_warnings,
)
from .apptests import run_tests
from .runtime import LoadedVersion, time_limit
from .schema import APP_NAME_RE, Manifest, parse_manifest, record_problems, schema_diff, with_defaults
from .store import Store, require_app
from .workflow import WORKFLOWS_CODE, WORKFLOWS_FILE, check_functions, static_warnings

IMPORT_TIMEOUT_SECONDS = 10
MAX_FAILING_SAMPLES = 5


def deploy(database: str | None, source_dir: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    source = Path(source_dir).resolve()
    db = Database(database)
    try:
        try:
            files, manifest = read_definition(source)
        except NeaiError as err:
            name = _peek_name(source) or _app_deployed_from(db, source)
            store = Store(db, name) if name else None
            if store and not dry_run:
                store.ensure()
            return _rejected(store, err, app=name, digest=None, source=str(source), record=not dry_run)
        return deploy_files(db, files, manifest, source=str(source), dry_run=dry_run)
    finally:
        db.close()


def rollback(database: str | None, name: str, version: int) -> dict[str, Any]:
    """Deploy an earlier version's files again, as a new version, through the usual checks."""
    db = Database(database)
    try:
        store = require_app(db, name)
        info = store.get_version(version, files=True) if isinstance(version, int) else None
        if info is None:
            known = [v["version"] for v in store.versions()]
            raise NeaiError("VERSION_NOT_FOUND", f"'{name}' has no version {version!r}",
                            hint="versions: " + (", ".join(map(str, known)) or "none"), details={"versions": known})
        files = info["files"]
        result = deploy_files(db, files, parse_definition(files), source=f"rollback to version {version}",
                              keep_source_dir=True)
        result["rolled_back_to"] = version
        return result
    finally:
        db.close()


def deploy_files(db: Database, files: dict[str, str], manifest: Manifest, *, source: str,
                 dry_run: bool = False, keep_source_dir: bool = False) -> dict[str, Any]:
    store = Store(db, manifest.name)
    if dry_run and not store.exists():
        return _validate_new(db, manifest, files)
    if not dry_run:
        store.ensure()
    digest = content_hash(files)
    active = store.active_version()
    active_info = store.get_version(active) if active else None

    # A dry run (`neai validate`) always runs every check and test, even on files already active.
    if active_info and active_info["hash"] == digest and not dry_run:
        store.add_deploy("unchanged", digest=digest, version=active, source=source)
        return {"ok": True, "status": "unchanged", "app": manifest.name, "version": active, "hash": digest,
                "warnings": [], "message": f"identical to active version {active}; nothing to do"}

    try:
        warnings, loaded = _check_code(manifest, files, digest)
        tests = run_tests(store.db, manifest, loaded, files)

        with store.transaction(write=True):
            # Compared with the active version as it is now, under the app's write lock: another
            # deploy may have gone live while this one ran its tests, and no write can slip in
            # between the compatibility check and the activation.
            active = store.active_version()
            active_info = store.get_version(active) if active else None
            if active_info and active_info["hash"] == digest and not dry_run:
                store.add_deploy("unchanged", digest=digest, version=active, source=source)
                return {"ok": True, "status": "unchanged", "app": manifest.name, "version": active, "hash": digest,
                        "warnings": warnings, "message": f"identical to active version {active}; nothing to do"}
            old_manifest = _active_manifest(store, active) if active_info else None
            schema_changed = old_manifest is not None and old_manifest.data_contract() != manifest.data_contract()
            if schema_changed:
                _check_compatible(store, old_manifest, manifest)
                _check_workflow_steps(store, manifest)
            if dry_run:
                return {"ok": True, "status": "valid", "app": manifest.name, "hash": digest,
                        "active_version": active, "schema_changed": schema_changed, "warnings": warnings,
                        "tests": tests, "message": "definition is valid; nothing was activated (dry run)"}
            filled = _fill_defaults(store, manifest) if schema_changed else {}
            version = store.next_version()
            store.add_version(version, digest, manifest.to_dict(), files)
            store.activate(version, None if keep_source_dir else source)
            store.add_deploy("activated", digest=digest, version=version, source=source)
    except NeaiError as err:
        return _rejected(store, err, app=manifest.name, digest=digest, source=source, record=not dry_run)

    result: dict[str, Any] = {
        "ok": True, "status": "activated", "app": manifest.name, "version": version, "hash": digest,
        "previous_version": active, "schema_changed": schema_changed, "warnings": warnings, "tests": tests,
    }
    if schema_changed:
        result["schema_diff"] = schema_diff(old_manifest, manifest)
    if filled:
        result["filled_defaults"] = filled
    store.add_logs([{"event": "deploy", "version": version, "message": f"activated version {version}",
                     "data": {"hash": digest, "source": source}}])
    return result


def _validate_new(db: Database, manifest: Manifest, files: dict[str, str]) -> dict[str, Any]:
    digest = content_hash(files)
    try:
        warnings, loaded = _check_code(manifest, files, digest)
        tests = run_tests(db, manifest, loaded, files)
    except NeaiError as err:
        return {"ok": False, "status": "rejected", "app": manifest.name, "active_version": None,
                "error": err.to_dict(), "message": f"definition rejected ({err.code}); nothing was recorded (dry run)"}
    return {"ok": True, "status": "valid", "app": manifest.name, "hash": digest, "active_version": None,
            "schema_changed": False, "warnings": warnings, "tests": tests,
            "message": "definition is valid; nothing was activated (dry run)"}


def _check_code(manifest: Manifest, files: dict[str, str], digest: str) -> tuple[list[str], LoadedVersion]:
    """Import the new code, check it against the contract, and return it ready to run (for tests.json)."""
    code = files[HANDLERS_FILE]
    path = code_filename(manifest.name, "deploy")
    module = _import(code, path, f"neai_deploy_{manifest.name}_{digest[7:19]}")
    warnings = check_handlers(module, manifest) + read_handler_writes(code, manifest)
    warnings += static_warnings(code, HANDLERS_FILE, manifest)
    flows, sources = None, (path,)
    if WORKFLOWS_CODE in files:
        flows_path = code_filename(manifest.name, "deploy", WORKFLOWS_CODE)
        flows = _import(files[WORKFLOWS_CODE], flows_path, f"neai_deploy_flows_{manifest.name}_{digest[7:19]}")
        sources = (path, flows_path)
        warnings += static_warnings(files[WORKFLOWS_CODE], WORKFLOWS_CODE, manifest)
        if WORKFLOWS_FILE not in files:
            warnings.append(f"{WORKFLOWS_CODE} is never used: there is no {WORKFLOWS_FILE}")
    warnings += check_functions(flows, manifest.workflows) + ui_warnings(files, manifest)
    return warnings, LoadedVersion(0, digest, manifest, module, flows, sources, files)


def _active_manifest(store: Store, active: int) -> Manifest:
    # Rebuilt from the active version's files, so it includes that version's workflows.
    info = store.get_version(active, files=True)
    try:
        return parse_definition(info["files"])
    except NeaiError:
        return parse_manifest(info["manifest"])


def _import(code: str, filename: str, module_name: str) -> Any:
    label = filename.rsplit("/", 1)[-1]
    try:
        with time_limit(IMPORT_TIMEOUT_SECONDS, what=f"importing {label}"):
            return import_source(code, filename, module_name)
    except HandlerTimeout:
        raise NeaiError("HANDLER_IMPORT_ERROR", f"importing {label} took longer than {IMPORT_TIMEOUT_SECONDS}s",
                        hint="don't do slow work (or loop forever) at import time") from None


def _check_compatible(store: Store, old: Manifest, new: Manifest) -> None:
    failing, total = [], 0
    for collection, record in store.iter_records():
        fields = new.collections.get(collection)
        if fields is None:
            problems = [{"field": "$", "problem": f"collection '{collection}' was removed but still holds records"}]
        else:
            # Fields with a default are filled in during activation, so they never count as missing.
            problems = record_problems(fields, with_defaults(fields, {k: v for k, v in record.items() if k != "id"}))
        if problems:
            total += 1
            if len(failing) < MAX_FAILING_SAMPLES:
                failing.append({"collection": collection, "id": record["id"], "problems": problems})
    duplicates = []
    for collection, combos in new.unique.items():
        for combo in combos:
            for dup in store.duplicates(collection, combo):
                duplicates.append({"collection": collection, "fields": list(combo), **dup})
    if total or duplicates:
        what = []
        if total:
            what.append(f"doesn't accept {total} stored record(s)")
        if duplicates:
            what.append(f"has {len(duplicates)} unique constraint(s) the stored data already breaks")
        raise NeaiError(
            "SCHEMA_INCOMPATIBLE",
            "the new schema " + " and ".join(what),
            hint='keep fields that hold data; make new fields optional ("required": false) or give them a '
                 '"default"; remove duplicate records before adding a unique constraint; or deploy under a '
                 "new app name to start with empty data",
            details={"schema_diff": schema_diff(old, new), "failing_records": total, "examples": failing,
                     "duplicates": duplicates[:MAX_FAILING_SAMPLES]},
        )


def _check_workflow_steps(store: Store, manifest: Manifest) -> None:
    for wf in manifest.workflows.values():
        total, examples = store.records_outside(wf.collection, wf.field, [*wf.step_names, *wf.exits])
        if total:
            raise NeaiError(
                "WORKFLOW_INCOMPATIBLE",
                f"{total} record(s) in '{wf.collection}' are in a step that workflow '{wf.name}' no longer has",
                hint="keep every step that records are in (rename a step only while no record is in it), "
                     "or move those records first with the old version",
                details={"workflow": wf.name, "field": wf.field, "stranded_records": total, "examples": examples,
                         "steps": list(wf.step_names), "exits": list(wf.exits)},
            )


def _fill_defaults(store: Store, manifest: Manifest) -> dict[str, int]:
    filled = {}
    for collection, fields in manifest.collections.items():
        for fname, spec in fields.items():
            if spec.default is not None and (n := store.fill_default(collection, fname, spec.default)):
                filled[f"{collection}.{fname}"] = n
    return filled


def _rejected(store: Store | None, err: NeaiError, *, app: str | None, digest: str | None,
              source: str, record: bool) -> dict[str, Any]:
    active = store.active_version() if store else None
    if store and record:
        store.add_deploy("rejected", digest=digest, source=source, error=err.to_dict())
        store.add_logs([{"event": "deploy", "level": "error", "message": f"deploy rejected: {err.code}",
                         "data": {"error": err.to_dict(), "source": source}}])
    if active:
        note = f"version {active} is still active and serving"
    else:
        note = "no version of this app has been activated yet"
    return {"ok": False, "status": "rejected", "app": app, "active_version": active, "error": err.to_dict(),
            "message": f"definition rejected ({err.code}); {note}"}


def _peek_name(source: Path) -> str | None:
    try:
        text = (source / "app.json").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        name = json.loads(text).get("name")
    except (ValueError, AttributeError):
        match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
        name = match.group(1) if match else None
    return name if isinstance(name, str) and APP_NAME_RE.match(name) else None


def _app_deployed_from(db: Database, source: Path) -> str | None:
    row = db.execute("SELECT name FROM neai_apps WHERE source_dir = %s ORDER BY name LIMIT 1", (str(source),)).fetchone()
    return row[0] if row else None
