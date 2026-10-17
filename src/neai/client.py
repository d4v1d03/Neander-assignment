from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import NeaiError


def call_server(url: str, handler: str, args: Any, *, token: str | None = None) -> dict[str, Any]:
    return _request(url, "POST", f"/call/{handler}", {"args": args}, token)


def auth_request(url: str, action: str, *, body: dict[str, Any] | None = None,
                 token: str | None = None) -> dict[str, Any]:
    if action == "me":
        return _request(url, "GET", "/auth/me", None, token)
    return _request(url, "POST", f"/auth/{action}", body or {}, token)


def _request(url: str, method: str, path: str, body: Any, token: str | None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            raise NeaiError("SERVER_ERROR", f"server answered HTTP {e.code} without a JSON body") from None
    except urllib.error.URLError as e:
        raise NeaiError("SERVER_NOT_RUNNING", f"can't reach {url}: {e.reason}",
                        hint="start the server with `neai serve <app>` or `neai run <dir>`") from None
