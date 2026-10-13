from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import NeaiError


def call_server(url: str, handler: str, caller: str, args: Any) -> dict[str, Any]:
    body = json.dumps({"caller": caller, "args": args}).encode()
    req = urllib.request.Request(f"{url.rstrip('/')}/call/{handler}", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
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
                        hint="start the server with `neai run <dir>`") from None
