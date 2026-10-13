from __future__ import annotations

import os
from typing import Any

from .paths import read_server_info, require_app, resolve_home
from .schema import parse_manifest
from .store import Store


def inspect_app(home: str | os.PathLike[str] | None, name: str, *, records: int = 20, logs: int = 20) -> dict[str, Any]:
    home = resolve_home(home)
    store = Store(require_app(home, name))
    try:
        active = store.active_version()
        info = store.get_version(active) if active else None
        manifest = info["manifest"] if info else None
        counts = store.count_by_collection()

        data: dict[str, Any] = {}
        collections = list(manifest["collections"]) if manifest else []
        for coll in collections + sorted(set(counts) - set(collections)):
            rows = [r for _, r in store.iter_records(coll)]
            data[coll] = {"count": len(rows), "records": rows[-records:] if records else []}
            if len(rows) > records:
                data[coll]["showing"] = f"last {records} of {len(rows)} (use --records N)"

        deploys = store.deploys(limit=10)
        server = read_server_info(home, name)
        return {
            "ok": True,
            "app": {
                "name": name,
                "id": store.get_meta("app_id"),
                "created_at": store.get_meta("created_at"),
                "description": (manifest or {}).get("description"),
                "source_dir": store.get_meta("source_dir"),
                "store": str(store.dir),
            },
            "active_version": (
                {"version": info["version"], "hash": info["hash"], "activated_at": info["activated_at"]} if info else None
            ),
            "handlers": parse_manifest(manifest).handler_list() if manifest else [],
            "schema": (manifest or {}).get("collections", {}),
            "data": data,
            "latest_rejected": latest_rejected(deploys),
            "recent_deploys": deploys[:5],
            "recent_errors": store.logs(limit=logs, errors_only=True),
            "recent_logs": store.logs(limit=logs),
            "server": {"running": True, **server} if server else {"running": False},
        }
    finally:
        store.close()


def latest_rejected(deploys: list[dict[str, Any]]) -> dict[str, Any] | None:
    for d in deploys:
        if d["status"] in ("activated", "unchanged"):
            return None
        if d["status"] == "rejected":
            return d
    return None


def app_logs(home: str | os.PathLike[str] | None, name: str, *, limit: int = 50, errors_only: bool = False,
             handler: str | None = None) -> dict[str, Any]:
    home = resolve_home(home)
    store = Store(require_app(home, name))
    try:
        return {"ok": True, "app": name, "logs": store.logs(limit=limit, errors_only=errors_only, handler=handler)}
    finally:
        store.close()
