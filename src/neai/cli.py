from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn, Sequence

from . import __version__
from .client import call_server
from .deploy import deploy
from .dotenv import load_dotenv
from .errors import NeaiError, error_envelope
from .inspection import app_logs, inspect_app
from .paths import check_app_name, read_server_info, require_app, resolve_home
from .scaffold import new_app
from .server import DEFAULT_PORT, run


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
    common.add_argument("--home", help="platform state directory (default: $NEAI_HOME or ./.neai)")

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

    s = sub.add_parser("run", parents=[common], help="deploy if changed, then serve on 127.0.0.1")
    s.add_argument("dir")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}; 0 picks a free port")
    s.add_argument("--quiet", action="store_true", help="don't log each request to stderr")

    s = sub.add_parser("call", parents=[common], help="call a handler on the running server")
    s.add_argument("app")
    s.add_argument("handler")
    s.add_argument("--as", dest="caller", required=True, help="the caller identity")
    s.add_argument("--args", default="{}", help="handler arguments as a JSON object")
    s.add_argument("--url", help="server URL (default: discovered from the running server)")

    s = sub.add_parser("inspect", parents=[common], help="identity, version, handlers, data, logs, rejections")
    s.add_argument("app")
    s.add_argument("--records", type=int, default=20, help="records to show per collection (default 20)")
    s.add_argument("--logs", type=int, default=20, help="log entries to show (default 20)")

    s = sub.add_parser("logs", parents=[common], help="recent calls, errors and deploys")
    s.add_argument("app")
    s.add_argument("--errors", action="store_true", help="only errors")
    s.add_argument("--handler", help="only this handler")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("ui", parents=[common], help="open a minimal local web UI (optional extra)")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--apps-dir", default="apps", help="where new apps created in the UI are written (default ./apps)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    loaded_env = load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run(args.home, args.dir, port=args.port, quiet=args.quiet)
        if args.command == "ui":
            from .ui.server import serve_ui

            return serve_ui(args.home, args.apps_dir, port=args.port, loaded_env=loaded_env)
        result = dispatch(args)
    except NeaiError as err:
        result = error_envelope(err)
    _print(result)
    return 0 if result.get("ok") else 1


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "new":
        return new_app(args.name, args.dir)
    if args.command == "validate":
        return deploy(args.home, args.dir, dry_run=True)
    if args.command == "deploy":
        return deploy(args.home, args.dir)
    if args.command == "call":
        return call(args)
    if args.command == "inspect":
        return inspect_app(args.home, check_app_name(args.app), records=args.records, logs=args.logs)
    if args.command == "logs":
        return app_logs(args.home, check_app_name(args.app), limit=args.limit, errors_only=args.errors,
                        handler=args.handler)
    raise NeaiError("USAGE_ERROR", f"unknown command {args.command}")


def call(args: argparse.Namespace) -> dict[str, Any]:
    try:
        handler_args = json.loads(args.args)
    except ValueError as e:
        raise NeaiError("ARGS_INVALID", f"--args is not valid JSON: {e}", hint=SHELL_QUOTING_HINT,
                        details={"received": args.args}) from None
    url = args.url
    if not url:
        home = resolve_home(args.home)
        require_app(home, check_app_name(args.app))
        info = read_server_info(home, args.app)
        if not info:
            raise NeaiError("SERVER_NOT_RUNNING", f"no running server for app '{args.app}' in {home}",
                            hint="start it in another shell with `neai run <dir>` (same --home / NEAI_HOME, "
                                 "or run both from the same folder)",
                            details={"home": str(home)})
        url = info["url"]
    return call_server(url, args.handler, args.caller, handler_args)


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
