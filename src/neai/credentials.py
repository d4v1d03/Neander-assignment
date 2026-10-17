"""Login tokens the CLI saved, so `neai call --as alice` can send alice's token.

Client-side only: it lives in <home>/credentials.json (mode 0600). A token works with every
server copy of its app, because the sessions behind it are in the shared database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import NeaiError
from .paths import ensure_private_dir, resolve_home

FILE_NAME = "credentials.json"


def credentials_file(home: str | os.PathLike[str] | None) -> Path:
    return resolve_home(home) / FILE_NAME


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"apps": {}}
    except ValueError:
        raise NeaiError("CREDENTIALS_INVALID", f"{path} is not valid JSON",
                        hint="delete it and log in again") from None
    return data if isinstance(data, dict) and isinstance(data.get("apps"), dict) else {"apps": {}}


def _save(path: Path, data: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    # Created with 0600 from the start, so the token is never readable by others, even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_login(home: Any, app: str, username: str, token: str, expires_at: str | None) -> Path:
    path = credentials_file(home)
    data = _load(path)
    data["apps"].setdefault(app, {})[username] = {"token": token, "expires_at": expires_at}
    _save(path, data)
    return path


def token_for(home: Any, app: str, username: str) -> str | None:
    entry = _load(credentials_file(home))["apps"].get(app, {}).get(username)
    return entry.get("token") if isinstance(entry, dict) else None


def forget_login(home: Any, app: str, username: str) -> bool:
    path = credentials_file(home)
    data = _load(path)
    if username not in data["apps"].get(app, {}):
        return False
    del data["apps"][app][username]
    if not data["apps"][app]:
        del data["apps"][app]
    _save(path, data)
    return True


def saved_logins(home: Any) -> dict[str, list[str]]:
    return {app: sorted(users) for app, users in _load(credentials_file(home))["apps"].items()}
