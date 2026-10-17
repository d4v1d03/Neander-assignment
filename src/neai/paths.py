from __future__ import annotations

import os
from pathlib import Path

from .errors import NeaiError
from .schema import APP_NAME_RE


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    # Client-side only in v2 (e.g. saved logins); everything a server needs is in the database.
    raw = home or os.environ.get("NEAI_HOME") or ".neai"
    return Path(raw).expanduser().resolve()


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def check_app_name(name: str) -> str:
    if not isinstance(name, str) or not APP_NAME_RE.match(name):
        raise NeaiError(
            "INVALID_APP_NAME",
            f"invalid app name {name!r}",
            hint="use lowercase letters, digits, '_' or '-', starting with a letter",
        )
    return name
