from __future__ import annotations

from typing import Any

from .database import Database, safe_url
from .errors import NeaiError
from .loader import parse_definition
from .schema import parse_manifest
from .store import Store, require_app


def inspect_app(database: str | None, name: str, *, records: int = 20, logs: int = 20) -> dict[str, Any]:
    db = Database(database)
    try:
        store = require_app(db, name)
        app = store.info()
        active = app["active_version"]
        info = store.get_version(active, files=True) if active else None
        manifest = info["manifest"] if info else None
        counts = store.count_by_collection()

        data: dict[str, Any] = {}
        collections = list(manifest["collections"]) if manifest else []
        for coll in collections + sorted(set(counts) - set(collections)):
            order = ["id", *(manifest["collections"].get(coll, {}) if manifest else {})]
            rows = [_ordered(r, order) for _, r in store.iter_records(coll)]
            data[coll] = {"count": len(rows), "records": rows[-records:] if records else []}
            if len(rows) > records:
                data[coll]["showing"] = f"last {records} of {len(rows)} (use --records N)"

        deploys = store.deploys(limit=10)
        instances = store.instances()
        return {
            "ok": True,
            "app": {
                "name": name,
                "id": app["id"],
                "created_at": app["created_at"],
                "description": (manifest or {}).get("description"),
                "source_dir": app["source_dir"],
                "database": safe_url(db.url),
            },
            "active_version": (
                {"version": info["version"], "hash": info["hash"], "activated_at": info["activated_at"],
                 "files": sorted(info["files"])} if info else None
            ),
            "versions": store.versions()[:10],
            "handlers": parse_manifest(manifest).handler_list() if manifest else [],
            "schema": _schema(manifest, info["files"] if info else {}),
            "unique": (manifest or {}).get("unique", {}),
            "auth": {**((manifest or {}).get("auth") or {"signup": "open", "roles": []}),
                     "users": len(store.list_users())},
            "workflows": _workflows(store, info["files"]) if info else {},
            "data": data,
            "latest_rejected": latest_rejected(deploys),
            "recent_deploys": deploys[:5],
            "recent_errors": store.logs(limit=logs, errors_only=True),
            "recent_logs": store.logs(limit=logs),
            "server": ({"running": True, "url": instances[0]["url"], "instances": instances} if instances
                       else {"running": False, "instances": []}),
        }
    finally:
        db.close()


def _ordered(record: dict[str, Any], order: list[str]) -> dict[str, Any]:
    return {**{k: record[k] for k in order if k in record}, **record}


def _schema(manifest: dict[str, Any] | None, files: dict[str, str]) -> dict[str, Any]:
    schema = {c: {f: dict(spec) for f, spec in fields.items()} for c, fields in (manifest or {}).get("collections", {}).items()}
    try:
        workflows = parse_definition(files).workflows if files else {}
    except NeaiError:
        workflows = {}
    for name, wf in workflows.items():
        spec = schema.get(wf.collection, {}).get(wf.field)
        if spec is not None:
            spec.pop("default", None)  # how the platform starts records there, not something to declare
            spec["managed_by_workflow"] = name
    return schema


def _workflows(store: Store, files: dict[str, str]) -> dict[str, Any]:
    try:
        workflows = parse_definition(files).workflows
    except NeaiError:
        return {}
    out = {}
    for name, wf in workflows.items():
        counts = store.count_by_value(wf.collection, wf.field)
        out[name] = {**wf.to_dict(), "records_per_step": {s: counts.get(s, 0) for s in (*wf.step_names, *wf.exits)}}
    return out


def latest_rejected(deploys: list[dict[str, Any]]) -> dict[str, Any] | None:
    for d in deploys:
        if d["status"] in ("activated", "unchanged"):
            return None
        if d["status"] == "rejected":
            return d
    return None


def app_logs(database: str | None, name: str, *, limit: int = 50, errors_only: bool = False,
             handler: str | None = None) -> dict[str, Any]:
    db = Database(database)
    try:
        store = require_app(db, name)
        return {"ok": True, "app": name, "logs": store.logs(limit=limit, errors_only=errors_only, handler=handler)}
    finally:
        db.close()
