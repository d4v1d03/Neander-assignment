from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, NoReturn, Sequence

from . import __version__
from .auth import USER_ACTIONS, user_admin
from .client import auth_request, call_server
from .credentials import forget_login, saved_logins, save_login, token_for
from .database import Database
from .deploy import deploy, rollback
from .dotenv import load_dotenv
from .errors import NeaiError, error_envelope
from .inspection import app_logs, inspect_app
from .paths import check_app_name
from .proxy import DEFAULT_PROXY_PORT, run_proxy
from .scaffold import new_app
from .schema import parse_manifest
from .server import DEFAULT_PORT, run, serve
from .store import list_instances, require_app


SHELL_QUOTING_HINT = ("the shell split or rewrote your --args JSON: wrap it in single quotes, "
                      """e.g. --args '{"title": "hi", "body": "text"}'""")


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        hint = f"run `{self.prog} --help`"
        if "unrecognized arguments" in message and "--args" in sys.argv:
            hint = SHELL_QUOTING_HINT
        _print(error_envelope(NeaiError("USAGE_ERROR", message, hint=hint)))
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="Postgres URL (default: $NEAI_DATABASE_URL, else the docker compose database)")
    common.add_argument("--home", help="where the CLI keeps saved logins (default: $NEAI_HOME or ./.neai)")

    p = JsonArgumentParser(prog="neai", description="A small local app platform for coding agents.")
    p.add_argument("--version", action="version", version=f"neai {__version__}")
    sub = p.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    s = sub.add_parser("new", parents=[common], help="create a skeleton app folder")
    s.add_argument("name")
    s.add_argument("--dir", help="folder to create (default: ./<name>)")

    s = sub.add_parser("validate", parents=[common], help="check a definition without activating it")
    s.add_argument("dir")

    s = sub.add_parser("deploy", parents=[common], help="validate and activate a definition")
    s.add_argument("dir")

    s = sub.add_parser("run", parents=[common], help="deploy a folder if it changed, then serve it on 127.0.0.1")
    s.add_argument("dir")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}; 0 picks a free port")
    s.add_argument("--quiet", action="store_true", help="don't log each request to stderr")

    s = sub.add_parser("serve", parents=[common], help="serve an app's active version straight from the database")
    s.add_argument("app")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}; 0 picks a free port")
    s.add_argument("--quiet", action="store_true", help="don't log each request to stderr")

    s = sub.add_parser("ps", parents=[common], help="list running app servers (instances)")
    s.add_argument("app", nargs="?")

    s = sub.add_parser("proxy", parents=[common], help="a round-robin load balancer over an app's instances")
    s.add_argument("app")
    s.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT, help=f"default {DEFAULT_PROXY_PORT}")
    s.add_argument("--quiet", action="store_true", help="don't log each request to stderr")

    s = sub.add_parser("call", parents=[common], help="call a handler on the running server")
    s.add_argument("app")
    s.add_argument("handler")
    s.add_argument("--as", dest="caller", help="call as this user, with the login `neai login` saved "
                                                 "(leave it out to call a public handler anonymously)")
    s.add_argument("--token", help="send this login token instead of a saved one")
    s.add_argument("--args", default="{}", help="handler arguments as a JSON object")
    s.add_argument("--url", help="server URL (default: a running instance, found in the database)")

    for name, what in (("signup", "create an account in an app and save its login"),
                       ("login", "log in to an app and save the login")):
        s = sub.add_parser(name, parents=[common], help=what)
        s.add_argument("app")
        s.add_argument("username")
        s.add_argument("--password-stdin", action="store_true",
                       help="read the password from stdin instead of asking (for scripts and agents)")
        s.add_argument("--show-token", action="store_true", help="also print the token, e.g. to use with curl")
        s.add_argument("--url", help="server URL (default: a running instance, found in the database)")

    s = sub.add_parser("logout", parents=[common], help="end a saved login")
    s.add_argument("app")
    s.add_argument("username")
    s.add_argument("--url", help="server URL (default: a running instance, found in the database)")

    sub.add_parser("logins", parents=[common], help="list the logins this CLI has saved")

    s = sub.add_parser("users", parents=[common], help="manage an app's accounts and roles (operator; "
                                                       "talks to the database directly)")
    s.add_argument("app")
    s.add_argument("action", choices=USER_ACTIONS)
    s.add_argument("username", nargs="?")
    s.add_argument("role", nargs="?", help="with grant / revoke: the role")
    s.add_argument("--role", dest="roles", action="append", default=[], help="with add: give a role (repeatable)")
    s.add_argument("--password-stdin", action="store_true", help="with add / passwd: read the password from stdin")

    s = sub.add_parser("inspect", parents=[common], help="identity, version, handlers, data, logs, rejections")
    s.add_argument("app")
    s.add_argument("--records", type=int, default=20, help="records to show per collection (default 20)")
    s.add_argument("--logs", type=int, default=20, help="log entries to show (default 20)")

    s = sub.add_parser("logs", parents=[common], help="recent calls, errors and deploys")
    s.add_argument("app")
    s.add_argument("--errors", action="store_true", help="only errors")
    s.add_argument("--handler", help="only this handler")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("versions", parents=[common], help="list an app's deployed versions")
    s.add_argument("app")

    s = sub.add_parser("rollback", parents=[common], help="deploy an earlier version again (as a new version)")
    s.add_argument("app")
    s.add_argument("--to", dest="version", type=int, required=True, help="the version to go back to")

    s = sub.add_parser("ui", parents=[common], help="open a minimal local web UI (optional extra)")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--apps-dir", default="apps", help="where new apps created in the UI are written (default ./apps)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    loaded_env = load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run(args.db, args.dir, port=args.port, quiet=args.quiet)
        if args.command == "serve":
            return serve(args.db, check_app_name(args.app), port=args.port, quiet=args.quiet)
        if args.command == "proxy":
            return run_proxy(args.db, check_app_name(args.app), port=args.port, quiet=args.quiet)
        if args.command == "ui":
            from .ui.server import serve_ui

            return serve_ui(args.db, args.apps_dir, port=args.port, loaded_env=loaded_env)
        result = dispatch(args)
    except NeaiError as err:
        result = error_envelope(err)
    _print(result)
    return 0 if result.get("ok") else 1


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "new":
        return new_app(args.name, args.dir)
    if args.command == "validate":
        return deploy(args.db, args.dir, dry_run=True)
    if args.command == "deploy":
        return deploy(args.db, args.dir)
    if args.command == "call":
        return call(args)
    if args.command in ("signup", "login"):
        return login(args)
    if args.command == "logout":
        return logout(args)
    if args.command == "logins":
        return {"ok": True, "logins": saved_logins(args.home)}
    if args.command == "users":
        return users(args)
    if args.command == "inspect":
        return inspect_app(args.db, check_app_name(args.app), records=args.records, logs=args.logs)
    if args.command == "logs":
        return app_logs(args.db, check_app_name(args.app), limit=args.limit, errors_only=args.errors,
                        handler=args.handler)
    if args.command == "ps":
        return with_db(args, lambda db: {"ok": True, "instances": list_instances(
            db, check_app_name(args.app) if args.app else None)})
    if args.command == "versions":
        return with_db(args, lambda db: {"ok": True, "app": args.app,
                                         "versions": require_app(db, check_app_name(args.app)).versions()})
    if args.command == "rollback":
        return rollback(args.db, check_app_name(args.app), args.version)
    raise NeaiError("USAGE_ERROR", f"unknown command {args.command}")


def call(args: argparse.Namespace) -> dict[str, Any]:
    try:
        handler_args = json.loads(args.args)
    except ValueError as e:
        raise NeaiError("ARGS_INVALID", f"--args is not valid JSON: {e}", hint=SHELL_QUOTING_HINT,
                        details={"received": args.args}) from None
    # The app is looked up first, so a typo in its name says "no such app", not "not logged in".
    url = args.url or with_db(args, lambda db: instance_url(db, check_app_name(args.app)))
    token = args.token
    if token is None and args.caller:
        token = token_for(args.home, args.app, args.caller)
        if token is None:
            raise NeaiError("NOT_LOGGED_IN", f"there's no saved login for {args.caller} in '{args.app}'",
                            hint=f"log in first: neai login {args.app} {args.caller} "
                                 f"(or neai signup {args.app} {args.caller} for a new account)")
    return call_server(url, args.handler, handler_args, token=token)


def login(args: argparse.Namespace) -> dict[str, Any]:
    app = check_app_name(args.app)
    password = read_password(args, confirm=args.command == "signup")
    url = args.url or with_db(args, lambda db: instance_url(db, app))
    result = auth_request(url, args.command, body={"username": args.username, "password": password})
    if not result.get("ok"):
        return result
    user = result["user"]
    path = save_login(args.home, app, user["username"], result["token"], result["expires_at"])
    out = {"ok": True, "status": "signed_up" if args.command == "signup" else "logged_in", "app": app,
           "user": user, "expires_at": result["expires_at"], "saved_to": str(path),
           "next": f"neai call {app} <handler> --as {user['username']}"}
    if args.show_token:
        out["token"] = result["token"]
    return out


def logout(args: argparse.Namespace) -> dict[str, Any]:
    app = check_app_name(args.app)
    token = token_for(args.home, app, args.username)
    if token is None:
        return {"ok": True, "status": "not_logged_in", "app": app, "username": args.username}
    url = args.url or with_db(args, lambda db: instance_url(db, app))
    result = auth_request(url, "logout", token=token)
    forget_login(args.home, app, args.username)  # forgotten here even if the server had already expired it
    if not result.get("ok") and result["error"]["code"] != "AUTH_INVALID":
        return result
    return {"ok": True, "status": "logged_out", "app": app, "username": args.username}


def users(args: argparse.Namespace) -> dict[str, Any]:
    app = check_app_name(args.app)
    db = Database(args.db)
    try:
        store = require_app(db, app)
        active = store.active_version()
        manifest = parse_manifest(store.get_version(active)["manifest"]) if active else None
        password = None
        if args.action in ("add", "passwd") and args.username:
            password = read_password(args, confirm=True)
        return user_admin(store, manifest, args.action, username=args.username, role=args.role,
                          roles=args.roles, password=password)
    finally:
        db.close()


def read_password(args: argparse.Namespace, *, confirm: bool) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    if not sys.stdin.isatty():
        action = f" {args.action}" if args.command == "users" else ""
        command = f"neai {args.command} {args.app}{action} {args.username}"
        raise NeaiError("PASSWORD_REQUIRED", "there's no terminal to type the password in",
                        hint=f"pipe it in with --password-stdin, e.g. printf '%s\\n' \"$PASSWORD\" | {command} --password-stdin")
    password = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Password again: ") != password:
        raise NeaiError("PASSWORD_MISMATCH", "the two passwords don't match")
    return password


def with_db(args: argparse.Namespace, fn: Any) -> Any:
    db = Database(args.db)
    try:
        return fn(db)
    finally:
        db.close()


def instance_url(db: Database, app: str) -> str:
    require_app(db, app)
    instances = list_instances(db, app)
    if not instances:
        raise NeaiError("SERVER_NOT_RUNNING", f"no running server for app '{app}'",
                        hint=f"start one in another shell with `neai serve {app}` (or `neai run <dir>`)")
    return instances[0]["url"]


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
