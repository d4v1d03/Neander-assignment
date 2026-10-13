from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import NeaiError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
MAX_REQUEST_CHARS = 4000
REQUEST_TIMEOUT_SECONDS = 120

AGENTS_MD = Path(__file__).resolve().parents[3] / "AGENTS.md"

STYLE_RULES = """\
Extra rules for code you write:
- handlers.py may import only `neai` and these standard modules: json, re, math, datetime, time,
  uuid, random, string, itertools, functools, collections, statistics, decimal, typing.
- No file, network, subprocess or OS access; no eval/exec/compile/open/__import__; no dunder attributes.
- Declare every handler's arguments in app.json with the long form
  {"kind": "write", "args": {"title": {"type": "string"}}} so the platform type-checks them; in code,
  only check what types can't express (blank strings, ranges) and raise AppError(CODE, message).
- Scope private data to ctx.caller and check ownership on every lookup by id.
- Declare every handler that writes as "write"; everything else as "read".

Answer with ONE json object and nothing else, exactly in this shape:
{"app_json": {<the complete app.json object>}, "handlers_py": "<the complete handlers.py source>",
 "summary": "<one or two sentences: what you built or changed>",
 "examples": [{"handler": "<name>", "caller": "<identity>", "args": {...}}, ...]}
"examples" is 3 to 6 calls that exercise the app, in an order that works on an empty app (create
things before reading or changing them; ids start at 1 and are shared across collections), using at
least two different callers."""


def settings() -> dict[str, Any]:
    return {
        "enabled": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "provider": "DeepSeek",
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    }


def generate(request: str, *, current: dict[str, str] | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise NeaiError("AI_NOT_CONFIGURED", "no DeepSeek API key is configured",
                        hint="put DEEPSEEK_API_KEY=... in the project's .env file (or export it), then restart `neai ui`")
    if not isinstance(request, str) or not request.strip():
        raise NeaiError("AI_REQUEST_INVALID", "describe the app or the change you want")
    if len(request) > MAX_REQUEST_CHARS:
        raise NeaiError("AI_REQUEST_INVALID", f"the description is longer than {MAX_REQUEST_CHARS} characters")

    messages = build_messages(request, current=current, error=error)
    content, usage = chat(messages, api_key=key)
    draft = parse_answer(content)
    draft["usage"] = usage
    return draft


def build_messages(request: str, *, current: dict[str, str] | None, error: dict[str, Any] | None) -> list[dict[str, str]]:
    try:
        docs = AGENTS_MD.read_text()
    except OSError:
        raise NeaiError("AI_DOCS_MISSING", f"can't read the platform docs at {AGENTS_MD}",
                        hint="install with `pip install -e .` from the repo root") from None
    system = (
        "You are a careful coding agent that builds apps for the neai platform. "
        "This is the platform's complete documentation:\n\n" + docs + "\n\n" + STYLE_RULES
    )
    if current and error:
        user = (
            f"The user asked for: {request}\n\n"
            f"The platform rejected these files with this error (JSON):\n{json.dumps(error, indent=2)}\n\n"
            f"{_files(current)}\n\nFix the problem and return the complete corrected files."
        )
    elif current:
        user = (
            f"{_files(current)}\n\nChange this app as follows: {request}\n\n"
            "Keep the app name. Don't make schema changes that stored records wouldn't satisfy "
            "(new fields must be optional). Return the complete updated files."
        )
    else:
        user = f"Build this app: {request}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def chat(messages: list[dict[str, str]], *, api_key: str) -> tuple[str, dict[str, Any] | None]:
    base = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    body = {
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
        "messages": messages,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 8000,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise NeaiError("AI_UNAVAILABLE", f"can't reach DeepSeek at {base}: {getattr(e, 'reason', e)}",
                        hint="check the network connection and DEEPSEEK_BASE_URL") from None
    except ValueError:
        raise NeaiError("AI_BAD_RESPONSE", "DeepSeek returned a response that isn't JSON") from None

    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise NeaiError("AI_BAD_RESPONSE", "DeepSeek's response has no message content") from None
    if choice.get("finish_reason") == "length":
        raise NeaiError("AI_BAD_RESPONSE", "DeepSeek's answer was cut off (too long)",
                        hint="ask for a smaller app, or split the request into steps")
    usage = payload.get("usage")
    return content, ({k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                     if isinstance(usage, dict) else None)


def parse_answer(content: str) -> dict[str, Any]:
    try:
        answer = json.loads(content)
    except ValueError:
        raise NeaiError("AI_BAD_RESPONSE", "the model's answer isn't valid JSON",
                        hint="try again; the answer is in details.preview", details={"preview": content[:500]}) from None
    app_json, handlers_py = answer.get("app_json") if isinstance(answer, dict) else None, None
    if isinstance(answer, dict):
        handlers_py = answer.get("handlers_py")
    if isinstance(app_json, str):
        try:
            app_json = json.loads(app_json)
        except ValueError:
            pass
    if not isinstance(app_json, dict) or not isinstance(handlers_py, str):
        raise NeaiError("AI_BAD_RESPONSE", "the model's answer is missing app_json or handlers_py",
                        hint="try again", details={"preview": content[:500]})
    examples = [e for e in answer.get("examples") or [] if isinstance(e, dict) and isinstance(e.get("handler"), str)]
    return {
        "app_json": json.dumps(app_json, indent=2) + "\n",
        "handlers_py": handlers_py.rstrip() + "\n",
        "summary": str(answer.get("summary", "")),
        "examples": examples[:8],
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    }


def _files(current: dict[str, str]) -> str:
    return (f"Current app.json:\n```json\n{current.get('app_json', '')}\n```\n\n"
            f"Current handlers.py:\n```python\n{current.get('handlers_py', '')}\n```")


def _http_error(e: urllib.error.HTTPError) -> NeaiError:
    try:
        detail = json.loads(e.read()).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        detail = ""
    known = {
        400: ("AI_REQUEST_INVALID", "DeepSeek rejected the request format", None),
        401: ("AI_AUTH_FAILED", "DeepSeek rejected the API key", "check DEEPSEEK_API_KEY, then restart `neai ui`"),
        402: ("AI_NO_BALANCE", "the DeepSeek account has insufficient balance", "top up at platform.deepseek.com"),
        422: ("AI_REQUEST_INVALID", "DeepSeek rejected a request parameter", "check DEEPSEEK_MODEL"),
        429: ("AI_RATE_LIMITED", "DeepSeek rate limit reached", "wait a moment and try again"),
    }
    code, message, hint = known.get(e.code, ("AI_UNAVAILABLE", f"DeepSeek returned HTTP {e.code}", "try again shortly"))
    return NeaiError(code, message + (f": {detail}" if detail else ""), hint=hint, details={"status": e.code})
