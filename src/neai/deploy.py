from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .errors import HandlerTimeout, NeaiError
from .loader import (
    HANDLERS_FILE,
    MANIFEST_FILE,
    check_handlers,
    content_hash,
    import_handlers,
    read_definition,
    read_handler_writes,
)
from .paths import app_dir, ensure_private_dir, list_apps, resolve_home
from .runtime import time_limit
from .schema import APP_NAME_RE, Manifest, parse_manifest, record_problems, schema_diff
from .store import Store

IMPORT_TIMEOUT_SECONDS = 10
MAX_FAILING_SAMPLES = 5


def deploy(home: str | os.PathLike[str] | None, source_dir: str | os.PathLike[str], *, dry_run: bool = False) -> dict[str, Any]:
    home = resolve_home(home)
    if not dry_run:
        ensure_private_dir(home)
    source = Path(source_dir).resolve()

    try:
        manifest_bytes, code_bytes, manifest = read_definition(source)
    except NeaiError as err:
        name = _peek_name(source) or _app_deployed_from(home, source)
        store = Store(app_dir(home, name)) if (name and not dry_run) else None
        try:
            return _rejected(store, err, app=name, digest=None, source=source, record=not dry_run)
        finally:
            if store:
                store.close()

    target = app_dir(home, manifest.name)
    if dry_run and not (target / "app.db").exists():
        return _validate_new(manifest, manifest_bytes, code_bytes)
    store = Store(target)
    try:
        return _deploy(store, manifest, manifest_bytes, code_bytes, source, dry_run)
    finally:
        store.close()


def _validate_new(manifest: Manifest, manifest_bytes: bytes, code_bytes: bytes) -> dict[str, Any]:
    digest = content_hash(manifest_bytes, code_bytes)
    with tempfile.TemporaryDirectory(prefix="neai-validate-") as tmp:
        path = Path(tmp) / HANDLERS_FILE
        path.write_bytes(code_bytes)
        try:
            warnings = check_handlers(_import(path, manifest.name, digest), manifest)
            warnings += read_handler_writes(code_bytes, manifest)
        except NeaiError as err:
            return {"ok": False, "status": "rejected", "app": manifest.name, "active_version": None,
                    "error": err.to_dict(), "message": f"definition rejected ({err.code}); nothing was recorded (dry run)"}
    return {"ok": True, "status": "valid", "app": manifest.name, "hash": digest, "active_version": None,
            "schema_changed": False, "warnings": warnings,
            "message": "definition is valid; nothing was activated (dry run)"}


def _import(path: Path, name: str, digest: str) -> Any:
    try:
        with time_limit(IMPORT_TIMEOUT_SECONDS, what=f"importing {HANDLERS_FILE}"):
            return import_handlers(path, f"neai_deploy_{name}_{digest[7:19]}")
    except HandlerTimeout:
        raise NeaiError("HANDLER_IMPORT_ERROR", f"importing {HANDLERS_FILE} took longer than {IMPORT_TIMEOUT_SECONDS}s",
                        hint="don't do slow work (or loop forever) at import time") from None


def _deploy(store: Store, manifest: Manifest, manifest_bytes: bytes, code_bytes: bytes, source: Path, dry_run: bool) -> dict[str, Any]:
    digest = content_hash(manifest_bytes, code_bytes)
    active = store.active_version()
    active_info = store.get_version(active) if active else None

    if active_info and active_info["hash"] == digest:
        if not dry_run:
            store.add_deploy("unchanged", digest=digest, version=active, source=str(source))
        return {"ok": True, "status": "unchanged", "app": manifest.name, "version": active, "hash": digest,
                "warnings": [], "message": f"identical to active version {active}; nothing to do"}

    versions_dir = ensure_private_dir(store.dir / "versions")
    staging = versions_dir / f".staging-{uuid.uuid4().hex[:12]}"
    staging.mkdir(mode=0o700)
    (staging / MANIFEST_FILE).write_bytes(manifest_bytes)
    (staging / HANDLERS_FILE).write_bytes(code_bytes)

    try:
        warnings = check_handlers(_import(staging / HANDLERS_FILE, manifest.name, digest), manifest)
        warnings += read_handler_writes(code_bytes, manifest)

        old_manifest = parse_manifest(active_info["manifest"]) if active_info else None
        schema_changed = old_manifest is not None and old_manifest.schema_dict() != manifest.schema_dict()

        with store.transaction(write=True):
            # Checked inside the write transaction, so no write can slip in between the
            # compatibility check and the activation.
            if schema_changed:
                _check_compatible(store, old_manifest, manifest)
            if dry_run:
                return {"ok": True, "status": "valid", "app": manifest.name, "hash": digest,
                        "active_version": active, "schema_changed": schema_changed, "warnings": warnings,
                        "message": "definition is valid; nothing was activated (dry run)"}
            version = store.next_version()
            target = versions_dir / str(version)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            store.add_version(version, digest, manifest.to_dict())
            store.set_meta("active_version", str(version))
            store.set_meta("source_dir", str(source))
            store.add_deploy("activated", digest=digest, version=version, source=str(source))
    except NeaiError as err:
        return _rejected(store, err, app=manifest.name, digest=digest, source=source, record=not dry_run)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    result: dict[str, Any] = {
        "ok": True, "status": "activated", "app": manifest.name, "version": version, "hash": digest,
        "previous_version": active, "schema_changed": schema_changed, "warnings": warnings,
    }
    if schema_changed:
        result["schema_diff"] = schema_diff(old_manifest, manifest)
    store.add_logs([{"event": "deploy", "version": version, "message": f"activated version {version}",
                     "data": {"hash": digest, "source": str(source)}}])
    return result


def _check_compatible(store: Store, old: Manifest, new: Manifest) -> None:
    failing, total = [], 0
    for collection, record in store.iter_records():
        fields = new.collections.get(collection)
        if fields is None:
            problems = [{"field": "$", "problem": f"collection '{collection}' was removed but still holds records"}]
        else:
            problems = record_problems(fields, {k: v for k, v in record.items() if k != "id"})
        if problems:
            total += 1
            if len(failing) < MAX_FAILING_SAMPLES:
                failing.append({"collection": collection, "id": record["id"], "problems": problems})
    if total:
        raise NeaiError(
            "SCHEMA_INCOMPATIBLE",
            f"the new schema doesn't accept {total} stored record(s); migrations aren't supported",
            hint='keep fields that hold data, make new fields optional ("required": false), '
                 "or deploy under a new app name to start with empty data",
            details={"schema_diff": schema_diff(old, new), "failing_records": total, "examples": failing},
        )


def _rejected(store: Store | None, err: NeaiError, *, app: str | None, digest: str | None,
              source: Path, record: bool) -> dict[str, Any]:
    active = store.active_version() if store else None
    if store and record:
        store.add_deploy("rejected", digest=digest, source=str(source), error=err.to_dict())
        store.add_logs([{"event": "deploy", "level": "error", "message": f"deploy rejected: {err.code}",
                         "data": {"error": err.to_dict(), "source": str(source)}}])
    if active:
        note = f"version {active} is still active and serving"
    else:
        note = "no version of this app has been activated yet"
    return {"ok": False, "status": "rejected", "app": app, "active_version": active, "error": err.to_dict(),
            "message": f"definition rejected ({err.code}); {note}"}


def _peek_name(source: Path) -> str | None:
    try:
        text = (source / MANIFEST_FILE).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        name = json.loads(text).get("name")
    except (ValueError, AttributeError):
        match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
        name = match.group(1) if match else None
    return name if isinstance(name, str) and APP_NAME_RE.match(name) else None


def _app_deployed_from(home: Path, source: Path) -> str | None:
    for name in list_apps(home):
        try:
            with closing(sqlite3.connect(app_dir(home, name) / "app.db")) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = 'source_dir'").fetchone()
        except sqlite3.Error:
            continue
        if row and row[0] == str(source):
            return name
    return None
