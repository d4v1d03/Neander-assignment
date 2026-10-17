"""Accounts for app users: every app has its own users, but the platform runs sign-up and login.

Apps (and AI-written handlers) never see or store a password. A handler only gets ctx.caller and
ctx.user, which the platform fills in from a verified login token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any

from .errors import NeaiError
from .schema import Manifest
from .store import Store

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")
USERNAME_RULE = "2-32 characters: lowercase letters, digits, '.', '_' or '-', starting with a letter or digit"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
SESSION_DAYS = 30
MAX_FAILED_LOGINS = 5
LOCK_SECONDS = 15 * 60
TOKEN_PREFIX = "neai_"
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32


@dataclass(frozen=True)
class User:
    username: str
    roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"username": self.username, "roles": list(self.roles)}


def _require_scrypt() -> None:
    if not hasattr(hashlib, "scrypt"):  # e.g. macOS's built-in Python, built on LibreSSL
        raise NeaiError("PYTHON_UNSUPPORTED", "this Python has no hashlib.scrypt, which neai uses for passwords",
                        hint="use Python 3.11 or newer from python.org, Homebrew or pyenv, and reinstall neai")


def hash_password(password: str) -> str:
    _require_scrypt()
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    _require_scrypt()
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_dummy_hash: str | None = None


def _waste_time_like_a_real_check(password: str) -> None:
    # A login for an unknown username takes as long as a wrong password, so timing
    # doesn't reveal which usernames exist.
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not a real password")
    verify_password(password, _dummy_hash)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_username(username: Any) -> str:
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise NeaiError("USERNAME_INVALID", f"invalid username {username!r}", hint=f"usernames are {USERNAME_RULE}")
    return username


def check_password(password: Any) -> str:
    if not isinstance(password, str) or "\x00" in password:
        raise NeaiError("PASSWORD_INVALID", "the password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise NeaiError("PASSWORD_TOO_SHORT", f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise NeaiError("PASSWORD_INVALID", f"the password must be at most {MAX_PASSWORD_LENGTH} characters")
    return password


class Accounts:
    """The users of one app, in the shared database (so every server copy sees the same logins)."""

    def __init__(self, store: Store):
        self.store = store

    def signup(self, manifest: Manifest, username: Any, password: Any) -> dict[str, Any]:
        if manifest.auth.signup != "open":
            raise NeaiError("SIGNUP_CLOSED", f"'{manifest.name}' doesn't allow sign-up",
                            hint=f"the operator creates accounts: neai users {manifest.name} add <username>")
        with self.store.db.transaction():
            user = self.create_user(manifest, username, password)
            return self._session(user)

    def login(self, username: Any, password: Any) -> dict[str, Any]:
        failed = NeaiError("LOGIN_FAILED", "wrong username or password")
        if not isinstance(username, str) or not isinstance(password, str) or not USERNAME_RE.match(username):
            raise failed
        with self.store.db.transaction():
            row = self.store.get_user(username)
            if row is None:
                _waste_time_like_a_real_check(password)
                raise failed
            if row["locked"]:
                raise NeaiError("LOGIN_LOCKED", f"too many wrong passwords for '{username}'; try again later",
                                hint="the lock lifts by itself; the operator can lift it now with "
                                     f"`neai users {self.store.app} unlock {username}`",
                                details={"locked_until": row["locked_until"]})
            good = verify_password(password, row["password_hash"])
            if not good:
                self.store.record_login_failure(username, max_failures=MAX_FAILED_LOGINS, lock_seconds=LOCK_SECONDS)
            elif row["failed_logins"]:
                self.store.clear_login_failures(username)
        # Raised outside the transaction, so the failure count above is kept.
        if not good:
            raise failed
        with self.store.db.transaction():
            return self._session(User(row["username"], tuple(row["roles"])))

    def user_for_token(self, token: Any) -> User:
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            raise NeaiError("AUTH_INVALID", "that isn't a neai login token",
                            hint="log in again: POST /auth/login (CLI: neai login <app> <username>)")
        row = self.store.session_user(token_hash(token))
        if row is None:
            raise NeaiError("AUTH_INVALID", f"the login token is unknown, expired or logged out for '{self.store.app}'",
                            hint=f"log in again: POST /auth/login (CLI: neai login {self.store.app} <username>); "
                                 "a token only works for the app it was issued by")
        return User(row["username"], tuple(row["roles"]))

    def logout(self, token: Any) -> User:
        user = self.user_for_token(token)
        self.store.delete_session(token_hash(token))
        return user

    def _session(self, user: User) -> dict[str, Any]:
        token = new_token()
        expires_at = self.store.add_session(token_hash(token), user.username, SESSION_DAYS)
        return {"user": user.to_dict(), "token": token, "expires_at": expires_at}


    def create_user(self, manifest: Manifest, username: Any, password: Any, roles: list[str] | None = None) -> User:
        username = check_username(username)
        check_password(password)
        roles = list(dict.fromkeys(roles or []))
        for role in roles:
            self._check_role(manifest, role)
        if not self.store.add_user(username, hash_password(password), roles):
            raise NeaiError("USERNAME_TAKEN", f"'{username}' already has an account in '{manifest.name}'",
                            hint="pick another username, or log in")
        return User(username, tuple(roles))

    def list_users(self) -> list[dict[str, Any]]:
        return self.store.list_users()

    def grant(self, manifest: Manifest, username: str, role: str) -> User:
        self._check_role(manifest, role)
        user = self._require(username)
        roles = list(dict.fromkeys([*user["roles"], role]))
        self.store.set_roles(username, roles)
        return User(username, tuple(roles))

    def revoke(self, username: str, role: str) -> User:
        user = self._require(username)
        roles = [r for r in user["roles"] if r != role]
        self.store.set_roles(username, roles)
        return User(username, tuple(roles))

    def set_password(self, username: str, password: Any) -> int:
        self._require(username)
        self.store.set_password_hash(username, hash_password(check_password(password)))
        return self.store.delete_sessions(username)  # a new password logs out every old session

    def unlock(self, username: str) -> None:
        self._require(username)
        self.store.clear_login_failures(username)

    def remove(self, username: str) -> None:
        self._require(username)
        self.store.delete_user(username)

    def _require(self, username: str) -> dict[str, Any]:
        user = self.store.get_user(username) if isinstance(username, str) else None
        if user is None:
            raise NeaiError("USER_NOT_FOUND", f"'{self.store.app}' has no user {username!r}",
                            hint=f"see who exists with `neai users {self.store.app} list`")
        return user

    @staticmethod
    def _check_role(manifest: Manifest, role: str) -> None:
        if role not in manifest.auth.roles:
            raise NeaiError("ROLE_UNKNOWN", f"'{manifest.name}' has no role {role!r}",
                            hint="roles are declared in app.json under auth.roles; declared: "
                                 + (", ".join(manifest.auth.roles) or "none"),
                            details={"declared": list(manifest.auth.roles)})


USER_ACTIONS = ("list", "add", "remove", "grant", "revoke", "passwd", "unlock")


def user_admin(store: Store, manifest: Manifest | None, action: str, *, username: str | None = None,
               role: str | None = None, roles: list[str] | None = None, password: str | None = None) -> dict[str, Any]:
    """The operator's account commands, shared by `neai users` and the dashboard."""
    accounts = Accounts(store)
    app = store.app
    if action not in USER_ACTIONS:
        raise NeaiError("USAGE_ERROR", f"unknown users action {action!r}", hint="one of: " + ", ".join(USER_ACTIONS))
    if action == "list":
        return {"ok": True, "app": app, "signup": manifest.auth.signup if manifest else None,
                "roles": list(manifest.auth.roles) if manifest else [], "users": accounts.list_users()}
    if not username:
        raise NeaiError("USAGE_ERROR", f"`{action}` needs a username")
    if action in ("grant", "revoke") and not role:
        raise NeaiError("USAGE_ERROR", f"`{action} {username}` needs a role")
    if action in ("add", "grant") and manifest is None:
        raise NeaiError("NO_ACTIVE_VERSION", f"app '{app}' has no active version, so it has no roles yet",
                        hint="deploy it first with `neai deploy <dir>`")
    if action == "add":
        user = accounts.create_user(manifest, username, password, roles)
        return {"ok": True, "status": "added", "app": app, "user": user.to_dict()}
    if action == "grant":
        return {"ok": True, "status": "granted", "app": app, "user": accounts.grant(manifest, username, role).to_dict()}
    if action == "revoke":
        return {"ok": True, "status": "revoked", "app": app, "user": accounts.revoke(username, role).to_dict()}
    if action == "passwd":
        ended = accounts.set_password(username, password)
        return {"ok": True, "status": "password_changed", "app": app, "username": username,
                "logged_out_sessions": ended}
    if action == "unlock":
        accounts.unlock(username)
        return {"ok": True, "status": "unlocked", "app": app, "username": username}
    accounts.remove(username)
    return {"ok": True, "status": "removed", "app": app, "username": username,
            "note": "their sessions ended; records they created stay"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
