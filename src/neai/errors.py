from __future__ import annotations

import re
from typing import Any

HTTP_STATUS = {
    "UNKNOWN_HANDLER": 404,
    "APP_NOT_FOUND": 404,
    "ROUTE_NOT_FOUND": 404,
    "NO_ACTIVE_VERSION": 503,
    "HANDLER_EXCEPTION": 500,
    "HANDLER_TIMEOUT": 504,
    "HANDLER_BAD_RESULT": 500,
    "READ_ONLY_VIOLATION": 500,
    "UNKNOWN_COLLECTION": 500,
    "INVALID_QUERY": 500,
    "RECORD_NOT_FOUND": 500,
    "BODY_TOO_LARGE": 413,
    "FORBIDDEN_HOST": 403,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "INTERNAL_ERROR": 500,
    "APP_EXISTS": 409,
    "SOURCE_CHANGED": 409,
    "AI_UNAVAILABLE": 502,
    "AI_BAD_RESPONSE": 502,
    "DATABASE_UNAVAILABLE": 503,
    "DATABASE_BUSY": 503,
    "AI_BUSY": 503,
    "DATABASE_ERROR": 500,
    "QUERY_TIMEOUT": 504,
    "UNIQUE_VIOLATION": 409,
    "VERSION_NOT_FOUND": 404,
    "NO_INSTANCE": 502,
    "INSTANCE_FAILED": 502,
    "AUTH_REQUIRED": 401,
    "AUTH_INVALID": 401,
    "LOGIN_FAILED": 401,
    "NOT_LOGGED_IN": 401,
    "FORBIDDEN": 403,
    "SIGNUP_CLOSED": 403,
    "USERNAME_TAKEN": 409,
    "USER_NOT_FOUND": 404,
    "LOGIN_LOCKED": 429,
    "WORKFLOW_FORBIDDEN": 403,
    "WORKFLOW_FINISHED": 409,
    "WORKFLOW_UNKNOWN": 500,
    "WORKFLOW_EXIT_UNKNOWN": 500,
    "WORKFLOW_FIELD_MANAGED": 500,
    "WORKFLOW_LOOP": 500,
    "WORKFLOW_BAD_CONDITION": 500,
    "WORKFLOW_BAD_NOTE": 500,
    "WORKFLOW_STATE_INVALID": 500,
    "TESTS_FAILED": 422,
}


class NeaiError(Exception):
    kind = "platform"

    def __init__(self, code: str, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "kind": self.kind, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)


APP_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class AppError(NeaiError):
    kind = "app"

    def __init__(self, code: str, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None):
        if not isinstance(code, str) or not APP_CODE_RE.match(code):
            raise ValueError(f"AppError code must be UPPER_SNAKE_CASE, e.g. NOT_FOUND (got {code!r})")
        super().__init__(code, str(message), hint=hint, details=details)


# A BaseException, so a handler's `except Exception:` can't swallow its own timeout.
class HandlerTimeout(BaseException):
    pass


def error_envelope(err: NeaiError) -> dict[str, Any]:
    return {"ok": False, "error": err.to_dict()}
