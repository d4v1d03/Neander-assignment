from __future__ import annotations

import ast

ALLOWED_MODULES = {
    "neai", "json", "re", "math", "datetime", "time", "uuid", "random", "string", "itertools",
    "functools", "collections", "statistics", "decimal", "fractions", "typing", "dataclasses",
    "enum", "textwrap", "operator", "copy", "bisect", "heapq", "hashlib", "secrets",
}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals",
    "vars", "help", "exit", "quit", "memoryview", "__builtins__", "__loader__", "__spec__",
}


def check(source: str) -> list[dict[str, object]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    problems: list[dict[str, object]] = []

    def flag(node: ast.AST, problem: str) -> None:
        problems.append({"line": getattr(node, "lineno", None), "problem": problem})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_MODULES:
                    flag(node, f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level or top not in ALLOWED_MODULES:
                flag(node, f"import from '{'.' * node.level}{node.module or ''}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            flag(node, f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            flag(node, f"access to '{node.attr}' is not allowed")
    return sorted(problems, key=lambda p: (p["line"] or 0))
