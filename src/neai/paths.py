from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from .errors import NeaiError
from .schema import APP_NAME_RE


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    raw = home or os.environ.get("NEAI_HOME") or ".neai"
    return Path(raw).expanduser().resolve()


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def check_app_name(name: str) -> str:
    # The name becomes a directory name, so this also prevents path traversal ("../x").
    if not isinstance(name, str) or not APP_NAME_RE.match(name):
        raise NeaiError(
            "INVALID_APP_NAME",
            f"invalid app name {name!r}",
            hint="use lowercase letters, digits, '_' or '-', starting with a letter",
        )
    return name


def app_dir(home: Path, name: str) -> Path:
    return home / "apps" / check_app_name(name)


def list_apps(home: Path) -> list[str]:
    apps = home / "apps"
    if not apps.is_dir():
        return []
    return sorted(p.name for p in apps.iterdir() if (p / "app.db").exists())


def require_app(home: Path, name: str) -> Path:
    d = app_dir(home, name)
    if not (d / "app.db").exists():
        known = list_apps(home)
        raise NeaiError(
            "APP_NOT_FOUND",
            f"no app named {name!r} in {home}",
            hint="deploy it first with `neai deploy <dir>`" + (f"; known apps: {', '.join(known)}" if known else ""),
            details={"home": str(home), "known_apps": known},
        )
    return d


def server_file(home: Path, name: str) -> Path:
    return app_dir(home, name) / "server.json"


def read_server_info(home: Path, name: str) -> dict[str, Any] | None:
    path = server_file(home, name)
    try:
        info = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    alive = _port_open(info.get("port")) if PROBE_PORT_FOR_LIVENESS else _pid_alive(info.get("pid"))
    return info if alive else None


# On Windows, os.kill(pid, 0) doesn't check a process: signal 0 is CTRL_C_EVENT, so it would
# interrupt the server. There we check that the server's port answers instead.
PROBE_PORT_FOR_LIVENESS = os.name == "nt"


def _port_open(port: Any) -> bool:
    if not isinstance(port, int):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
