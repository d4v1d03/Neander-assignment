from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import NeaiError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
MAX_REQUEST_CHARS = 4000
REQUEST_TIMEOUT_SECONDS = 120
DEFAULT_MAX_TOKENS = 8000
FENCE_RE = re.compile(r"^```[a-z]*\s*\n(.*)\n```$", re.S)

AGENTS_MD = Path(__file__).resolve().parents[3] / "AGENTS.md"

STYLE_RULES = """\
Extra rules for code you write:
- handlers.py and workflows.py may import only `neai` and these standard modules: json, re, math,
  datetime, time, uuid, random, string, itertools, functools, collections, statistics, decimal, typing.
- No file, network, subprocess or OS access; no eval/exec/compile/open/__import__; no dunder attributes.
- Declare every handler's arguments in app.json with the long form
  {"kind": "write", "args": {"title": {"type": "string"}}} so the platform type-checks them; in code,
  only check what types can't express (blank strings, ranges) and raise AppError(CODE, message).
- Scope private data to ctx.caller and check ownership on every lookup by id.
- Declare every handler that writes as "write"; everything else as "read".
- Users log in; the platform sets ctx.caller from their login. Never store or check passwords yourself.
  Give each handler the narrowest "access": "public" only for reads that people who aren't logged in
  may see (ctx.caller is then None, so handle that), "user" (the default) for normal actions, and a
  role from "auth": {"roles": [...]} for privileged actions (moderation, admin, staff).
- For one-per-user rules (votes, RSVPs), declare the field pair in "unique" and still check first so
  the handler can return a friendly result.

- Add a workflow (workflows.json, plus workflows.py only if it names functions) when records move
  through steps with rules: approvals, interview rounds, order states. Otherwise leave both out.
- Write tests.json: 4 to 10 tests covering the main path, one refusal (someone who may not do
  something) and one rule; every deploy runs them in an empty sandbox, in order.

Answer with ONE json object and nothing else, exactly in this shape:
{"app_json": {<the complete app.json object>}, "handlers_py": "<the complete handlers.py source>",
 "workflows_json": {<the complete workflows.json object>} or null,
 "workflows_py": "<the complete workflows.py source>" or null,
 "tests_json": {<the complete tests.json object>},
 "summary": "<one or two sentences: what you built or changed>",
 "examples": [{"handler": "<name>", "caller": "<identity>", "args": {...}}, ...]}
"examples" is 3 to 6 calls that exercise the app, in an order that works on an empty app (create
things before reading or changing them; ids start at 1 and are shared across collections), using at
least two different callers. A caller is a username (lowercase letters and digits); add
"roles": ["..."] to an example whose handler needs a role, and use "caller": null for an anonymous
call to a public handler."""


UI_RULES = """\
You now write the app's own UI: one file, ui/index.html, served by the app's server at /.
- Plain HTML, CSS and JavaScript in that one file. No frameworks, no build step, and nothing from
  other sites (no CDN scripts, fonts or images): the page's Content-Security-Policy blocks them.
- Load the helper with <script src="/_neai/sdk.js"></script> and use only window.neai:
  await neai.contract(), await neai.me() ({username, roles} or null), await neai.signup(u, p),
  await neai.login(u, p), await neai.logout(), await neai.call("handler", {...args}) (returns the
  result; failures throw an error with .code, .message and .hint), neai.onAuthChange(render).
- Build the page with document.createElement and textContent. Never put data into innerHTML:
  records come from other users.
- Show a login / sign-up form when nobody is logged in (sign-up only if auth.signup is "open"),
  and only offer actions the user may call (their access and the user's roles); show every error's
  message to the user.
- Call only handlers that app.json declares, with their declared args. If the app has a workflow,
  show each record's step and the steps around it.
- Make it pleasant and responsive: readable on a phone, light and dark (prefers-color-scheme).

Answer with ONE json object and nothing else: {"ui_html": "<the complete file>", "summary": "<one sentence>"}"""


def settings() -> dict[str, Any]:
    return {
        "enabled": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "provider": "DeepSeek",
        "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    }


def _key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise NeaiError("AI_NOT_CONFIGURED", "no DeepSeek API key is configured",
                        hint="put DEEPSEEK_API_KEY=... in the project's .env file (or export it), then restart `neai ui`")
    return key


def _check_request(request: Any, *, required: bool = True) -> str:
    if not isinstance(request, str) or (required and not request.strip()):
        raise NeaiError("AI_REQUEST_INVALID", "describe the app or the change you want")
    if len(request) > MAX_REQUEST_CHARS:
        raise NeaiError("AI_REQUEST_INVALID", f"the description is longer than {MAX_REQUEST_CHARS} characters")
    return request


def generate(request: str, *, current: dict[str, str] | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """Draft (or change, or fix) an app's backend files: app.json, handlers.py and its workflow files."""
    key = _key()
    messages = build_messages(_check_request(request), current=current, error=error)
    draft, usage = _ask(messages, key, parse_answer)
    draft["usage"] = usage
    return draft


def generate_ui(request: str, *, files: dict[str, str]) -> dict[str, Any]:
    """Draft ui/index.html for an app, from its contract (and an optional description of the UI)."""
    key = _key()
    request = _check_request(request, required=False)
    system = _system_prompt(UI_RULES)
    user = (f"{_files({k: v for k, v in files.items() if not k.startswith('ui/')})}\n\n"
            + (f"Current ui/index.html:\n```html\n{files['ui/index.html']}\n```\n\n" if "ui/index.html" in files else "")
            + (f"What the UI should be like: {request}\n\n" if request.strip() else "")
            + "Write the complete ui/index.html for this app.")
    answer, usage = _ask([{"role": "system", "content": system}, {"role": "user", "content": user}], key, _json_answer)
    html = answer.get("ui_html") if isinstance(answer, dict) else None
    if not isinstance(html, str) or "<" not in html:
        raise NeaiError("AI_BAD_RESPONSE", "the model's answer has no ui_html", hint="try again",
                        details={"keys": sorted(answer) if isinstance(answer, dict) else None})
    return {"files": {"ui/index.html": html.rstrip() + "\n"}, "summary": str(answer.get("summary", "")),
            "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL), "usage": usage}


def _system_prompt(rules: str) -> str:
    try:
        docs = AGENTS_MD.read_text()
    except OSError:
        raise NeaiError("AI_DOCS_MISSING", f"can't read the platform docs at {AGENTS_MD}",
                        hint="install with `pip install -e .` from the repo root") from None
    return ("You are a careful coding agent that builds apps for the neai platform. "
            "This is the platform's complete documentation:\n\n" + docs + "\n\n" + rules)


def build_messages(request: str, *, current: dict[str, str] | None, error: dict[str, Any] | None) -> list[dict[str, str]]:
    system = _system_prompt(STYLE_RULES)
    if current:
        current = {k: v for k, v in current.items() if not k.startswith("ui/")}  # the UI has its own request
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
            "(new fields must be optional or have a \"default\"). Return the complete updated files."
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
        "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
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
    reason = choice.get("finish_reason")
    if reason == "length":
        raise NeaiError("AI_BAD_RESPONSE", "DeepSeek's answer was cut off (too long)",
                        hint="ask for a smaller app, split the request into steps, or raise DEEPSEEK_MAX_TOKENS")
    if reason not in (None, "stop"):
        raise NeaiError("AI_BAD_RESPONSE", f"DeepSeek stopped before finishing its answer ({reason})",
                        hint="try again in a moment", details={"finish_reason": reason})
    usage = payload.get("usage")
    return content, ({k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
                     if isinstance(usage, dict) else None)


def _ask(messages: list[dict[str, str]], key: str, parse: Any) -> tuple[Any, dict[str, Any] | None]:
    """Ask, and if the answer isn't valid JSON, ask once more, saying exactly where it broke."""
    content, usage = chat(messages, api_key=key)
    try:
        return parse(content), usage
    except NeaiError as err:
        if err.details.get("kind") != "invalid_json":
            raise
        retry = [*messages, {"role": "assistant", "content": content}, {"role": "user", "content": (
            f"That answer isn't valid JSON ({err.details['error']} at character {err.details['position']}). "
            "Send the complete answer again as ONE valid JSON object: inside strings, escape every double quote "
            "as \\\" and every backslash as \\\\, and write newlines as \\n.")}]
        content, again = chat(retry, api_key=key)
        return parse(content), _total(usage, again)


def _total(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    if not a or not b:
        return a or b
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _repair_escapes(text: str) -> str:
    # Models writing code inside JSON often emit escapes JSON doesn't have, like \' or \d: keep the
    # backslash as a character, which is what the code meant. Valid escapes are left alone.
    return re.sub(r"\\(.)", lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else "\\\\" + m.group(1),
                  text, flags=re.S)


def _json_answer(content: str) -> Any:
    """Parse the model's JSON, forgiving the usual slips: a ```json fence, raw newlines inside strings,
    invalid escapes, and text after the object."""
    text = content.strip()
    if fenced := FENCE_RE.match(text):
        text = fenced.group(1).strip()
    decoder = json.JSONDecoder(strict=False)
    first: ValueError | None = None
    for candidate in (text, _repair_escapes(text)):
        try:
            return decoder.raw_decode(candidate)[0]
        except ValueError as e:
            first = first or e
    pos = getattr(first, "pos", None)
    raise NeaiError(
        "AI_BAD_RESPONSE", f"the model's answer isn't valid JSON: {getattr(first, 'msg', first)}"
                           + (f" (character {pos} of {len(text)})" if pos is not None else ""),
        hint="click Generate (or Fix with AI) again; if it keeps happening, ask for a smaller app or raise "
             "DEEPSEEK_MAX_TOKENS",
        details={"kind": "invalid_json", "error": getattr(first, "msg", str(first)), "position": pos,
                 "length": len(text), "near": text[max(0, (pos or 0) - 150):(pos or 0) + 150],
                 "start": text[:300], "end": text[-300:]},
    )


def _as_json_text(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return json.dumps(value, indent=2) + "\n" if isinstance(value, dict) else None


def parse_answer(content: str) -> dict[str, Any]:
    answer = _json_answer(content)
    answer = answer if isinstance(answer, dict) else {}
    app_json, handlers_py = _as_json_text(answer.get("app_json")), answer.get("handlers_py")
    if app_json is None or not isinstance(handlers_py, str):
        raise NeaiError("AI_BAD_RESPONSE", "the model's answer is missing app_json or handlers_py",
                        hint="try again", details={"preview": content[:500]})
    files = {"app.json": app_json, "handlers.py": handlers_py.rstrip() + "\n"}
    if (workflows := _as_json_text(answer.get("workflows_json"))) is not None:
        files["workflows.json"] = workflows
    if isinstance(answer.get("workflows_py"), str) and answer["workflows_py"].strip():
        files["workflows.py"] = answer["workflows_py"].rstrip() + "\n"
    if (tests := _as_json_text(answer.get("tests_json"))) is not None:
        files["tests.json"] = tests
    examples = [e for e in answer.get("examples") or [] if isinstance(e, dict) and isinstance(e.get("handler"), str)]
    return {"files": files, "summary": str(answer.get("summary", "")), "examples": examples[:8],
            "model": os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)}


LANGUAGES = {".json": "json", ".py": "python", ".html": "html", ".js": "javascript", ".css": "css"}


def _files(current: dict[str, str]) -> str:
    return "\n\n".join(f"Current {name}:\n```{LANGUAGES.get(Path(name).suffix, '')}\n{text}\n```"
                        for name, text in current.items())


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
