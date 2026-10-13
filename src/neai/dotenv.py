from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def env_files() -> list[Path]:
    candidates = [Path.cwd() / ".env"]
    if (PROJECT_ROOT / "pyproject.toml").is_file():
        candidates.append(PROJECT_ROOT / ".env")
    seen, out = set(), []
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            out.append(resolved)
    return out


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not KEY_RE.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_dotenv() -> list[dict[str, Any]]:
    report = []
    for path in env_files():
        try:
            values = parse(path.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        loaded = []
        for key, value in values.items():
            if value and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
        entry: dict[str, Any] = {"file": str(path), "variables": loaded}
        if os.name != "nt" and path.stat().st_mode & 0o077:
            entry["warning"] = f"{path} is readable by other users; run: chmod 600 {path}"
        report.append(entry)
    return report
