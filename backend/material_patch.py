"""Guardrail for propose_material_patch: adapt a code material's edges, never rewrite its core.

Edges are the input channel (argparse), stdout (JSON / [NEEDS_INFO]), stderr and exit
codes. The core is every call into an external system; a patch must keep all of them.
"""

from __future__ import annotations

import ast
import difflib
import sys

# Share of the original's non-edge lines a patch may change before it counts as a rewrite.
MAX_CHANGED_RATIO = 0.5

# Stdlib modules that talk to the outside world; the rest of the stdlib is local computation.
IO_STDLIB_MODULES = frozenset({"urllib", "http", "socket", "subprocess", "sqlite3", "smtplib", "ssl", "ftplib"})


def _is_external_module(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in IO_STDLIB_MODULES or top not in sys.stdlib_module_names


def _imported_roots(tree: ast.AST) -> dict[str, str]:
    roots: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_external_module(alias.name):
                    if alias.asname:
                        roots[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".", 1)[0]
                        roots[top] = top
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            if node.level or _is_external_module(module):
                for alias in node.names:
                    roots[alias.asname or alias.name] = f"{module}.{alias.name}"
    return roots


def _origin(expr: ast.AST | None, tracked: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return tracked.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _origin(expr.value, tracked)
        return f"{base}.{expr.attr}" if base else None
    if isinstance(expr, ast.Call):
        base = _origin(expr.func, tracked)
        return f"{base}()" if base else None
    if isinstance(expr, ast.Await):
        return _origin(expr.value, tracked)
    if isinstance(expr, ast.Subscript):
        base = _origin(expr.value, tracked)
        return f"{base}[]" if base else None
    return None


def _bindings(tree: ast.AST):
    """Yield (target, value, iterated) for every simple name binding."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value, False
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            yield node.target, node.value, False
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield node.target, node.iter, True
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    yield item.optional_vars, item.context_expr, False


def _tracked_origins(tree: ast.AST) -> dict[str, str]:
    """Name -> external origin, propagated through assignments, loops and ``with``."""
    tracked = _imported_roots(tree)
    # Bindings can appear before the binding they depend on in walk order.
    for _ in range(10):
        changed = False
        for target, value, iterated in _bindings(tree):
            if not isinstance(target, ast.Name):
                continue
            origin = _origin(value, tracked)
            if origin and iterated:
                origin = f"{origin}[]"
            if origin and tracked.get(target.id) != origin:
                tracked[target.id] = origin
                changed = True
        if not changed:
            break
    return tracked


def core_call_signatures(code: str) -> set[str]:
    """Every external call in ``code``, keyed by import origin + attribute chain, not variable names.

    Raises SyntaxError when ``code`` does not parse.
    """
    tree = ast.parse(code)
    tracked = _tracked_origins(tree)
    return {
        origin
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (origin := _origin(node.func, tracked))
    }


# Edge statements (the I/O channel) are left out of the changed-line ratio. A statement is
# an edge only when EVERY call in it is one of these, so business calls cannot hide in one.
_EDGE_NAME_CALLS = frozenset({"print", "SystemExit", "ArgumentParser", "dumps"})
_EDGE_MODULE_CALLS = frozenset({("json", "dumps"), ("sys", "exit"), ("argparse", "ArgumentParser")})
_PARSE_ARGS_ATTRS = frozenset(
    {"parse_args", "parse_known_args", "parse_intermixed_args", "parse_known_intermixed_args"}
)
_ARGPARSE_ATTRS = _PARSE_ARGS_ATTRS | {
    "add_argument", "add_argument_group", "add_mutually_exclusive_group", "set_defaults",
}
_EXIT_EXCEPTIONS = frozenset({"SystemExit", "ArgumentError"})
# Only allowed inside an input-binding assignment, never on their own.
_PURE_BUILTINS = frozenset(
    {"str", "int", "float", "bool", "list", "tuple", "set", "dict", "len", "isinstance", "sorted", "min", "max"}
)
_PURE_METHODS = frozenset({
    "lower", "upper", "casefold", "title", "strip", "lstrip", "rstrip", "split", "rsplit",
    "splitlines", "replace", "startswith", "endswith", "join", "get",
})


def _is_module_attr(node: ast.AST, module: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == module
    )


def _is_reader_call(call: ast.Call) -> bool:
    """``globals()``, ``os.getenv(...)``, ``os.environ.get(...)``, ``globals().get(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "globals"
    if _is_module_attr(func, "os", "getenv"):
        return True
    return isinstance(func, ast.Attribute) and func.attr == "get" and (
        _is_module_attr(func.value, "os", "environ")
        or (isinstance(func.value, ast.Call) and isinstance(func.value.func, ast.Name) and func.value.func.id == "globals")
    )


def _calls(node: ast.AST) -> list[ast.Call]:
    return [sub for sub in ast.walk(node) if isinstance(sub, ast.Call)]


def _functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class _EdgeContext:
    def __init__(self, tree: ast.AST, pure_helpers: frozenset[str]) -> None:
        self.tracked = _tracked_origins(tree)
        self.pure_helpers = pure_helpers
        self.reader_helpers = frozenset(
            name for name, func in _functions(tree).items()
            if any(_is_reader_call(c) for c in _calls(func))
            and all(_is_reader_call(c) or self.is_pure_call(c) for c in _calls(func))
        )
        self.namespaces = frozenset(self._namespace_names(tree))

    @staticmethod
    def _namespace_names(tree: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and _is_parse_call(node.value)):
                continue
            for target in node.targets:
                if isinstance(target, (ast.Tuple, ast.List)) and target.elts:
                    target = target.elts[0]
                if isinstance(target, ast.Name):
                    names.add(target.id)
        return names

    def _external(self, call: ast.Call) -> bool:
        return _origin(call.func, self.tracked) is not None

    def is_edge_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in _EDGE_NAME_CALLS
        if any(_is_module_attr(func, m, a) for m, a in _EDGE_MODULE_CALLS):
            return True
        return isinstance(func, ast.Attribute) and func.attr in _ARGPARSE_ATTRS and not self._external(call)

    def is_pure_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in _PURE_BUILTINS
        return isinstance(func, ast.Attribute) and func.attr in _PURE_METHODS and not self._external(call)

    def _reads_input(self, node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and (
                _is_reader_call(sub) or (isinstance(sub.func, ast.Name) and sub.func.id in self.reader_helpers)
            ):
                return True
            if isinstance(sub, ast.Attribute) and (
                (isinstance(sub.value, ast.Name) and sub.value.id in self.namespaces) or _is_parse_call(sub.value)
            ):
                return True
            if isinstance(sub, ast.Subscript) and _is_module_attr(sub.value, "os", "environ"):
                return True
        return False

    def is_input_binding(self, value: ast.AST) -> bool:
        """An assignment whose value only reads an input (argparse, env, globals) and converts it."""
        helpers = self.reader_helpers | self.pure_helpers
        return self._reads_input(value) and all(
            self.is_edge_call(c)
            or _is_reader_call(c)
            or self.is_pure_call(c)
            or (isinstance(c.func, ast.Name) and c.func.id in helpers)
            for c in _calls(value)
        )

    def is_edge(self, node: ast.AST) -> bool:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(node, ast.ExceptHandler):
            return _catches_exit(node)
        if isinstance(node, ast.Try):
            return bool(node.handlers) and all(_catches_exit(h) for h in node.handlers)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name in self.reader_helpers
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Name) and node.exc.id == "SystemExit":
            return True
        calls = [c for expr in _header_exprs(node) for c in _calls(expr)]
        if calls and all(self.is_edge_call(c) for c in calls):
            return True
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        return value is not None and self.is_input_binding(value)


def _is_parse_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _PARSE_ARGS_ATTRS


def _catches_exit(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return all(
        (isinstance(k, ast.Name) and k.id in _EXIT_EXCEPTIONS)
        or (isinstance(k, ast.Attribute) and k.attr in _EXIT_EXCEPTIONS)
        for k in kinds
    )


def _header_exprs(node: ast.AST) -> list[ast.AST]:
    """What a statement evaluates itself; a compound statement's body is not part of it."""
    if isinstance(node, (ast.If, ast.While)):
        return [node.test]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target, node.iter]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return list(node.items)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return list(node.decorator_list)
    if isinstance(node, (ast.Try, ast.ExceptHandler)):
        return []
    match_node = getattr(ast, "Match", None)
    if match_node is not None and isinstance(node, match_node):
        return [node.subject]
    return [node]


def _pure_helpers(before: ast.AST, after: ast.AST) -> frozenset[str]:
    """Functions both versions define identically and that only call pure builtins / methods."""
    ctx = _EdgeContext(before, frozenset())
    old, new = _functions(before), _functions(after)
    return frozenset(
        name for name, func in old.items()
        if name in new
        and ast.dump(func) == ast.dump(new[name])
        and all(ctx.is_pure_call(c) for c in _calls(func))
    )


def _logic_lines(code: str, tree: ast.AST | None, pure_helpers: frozenset[str]) -> list[str]:
    """Non-blank lines outside edge statements, indentation ignored."""
    edge: set[int] = set()
    if tree is not None:
        ctx = _EdgeContext(tree, pure_helpers)
        owner: dict[int, ast.AST] = {}
        # Breadth-first: an inner statement overrides the lines of the one around it.
        for node in ast.walk(tree):
            if isinstance(node, (ast.stmt, ast.ExceptHandler)):
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    owner[line] = node
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ctx.reader_helpers:
                edge.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        edge.update(line for line, node in owner.items() if ctx.is_edge(node))
    return [
        stripped
        for number, line in enumerate(code.splitlines(), start=1)
        if (stripped := line.strip()) and not stripped.startswith("#") and number not in edge
    ]


def _meaningful_lines(code: str) -> list[str]:
    # Indentation is ignored so moving code into main() is not counted as changing it.
    return [line.strip() for line in code.splitlines() if line.strip()]


def changed_ratio(before: str, after: str) -> float:
    """Share of the original's non-edge lines the patch does not keep."""
    if not _meaningful_lines(before):
        return 1.0
    try:
        before_tree, after_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        before_tree = after_tree = None
    pure = _pure_helpers(before_tree, after_tree) if before_tree and after_tree else frozenset()
    original = _logic_lines(before, before_tree, pure)
    if not original:
        return 0.0
    matcher = difflib.SequenceMatcher(None, original, _logic_lines(after, after_tree, pure), autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    return 1 - kept / len(original)


def patch_defects(before: str, after: str) -> list[str]:
    """Mistakes in the patch itself that a corrected patch can fix."""
    if before == after:
        return ["The patch changes nothing."]
    try:
        ast.parse(after)
    except SyntaxError as exc:
        return [f"The patched code is not valid Python ({exc.msg}, line {exc.lineno})."]
    return []


def rewrite_reasons(before: str, after: str) -> list[str]:
    """Why ``after`` (already free of patch_defects) is a rewrite of ``before``; empty when it is not."""
    try:
        core_before = core_call_signatures(before)
    except SyntaxError as exc:
        return [f"The original code material is not valid Python ({exc.msg}, line {exc.lineno}), so it cannot be adapted safely."]
    core_after = core_call_signatures(after)
    problems: list[str] = []
    missing = sorted(core_before - core_after)
    if missing:
        problems.append(
            "The patch removes or changes external call(s) the original makes: "
            + ", ".join(f"`{name}`" for name in missing)
            + "."
        )
    ratio = changed_ratio(before, after)
    if ratio > MAX_CHANGED_RATIO:
        problems.append(
            f"The patch changes {ratio:.0%} of the original lines outside the input/output edges "
            f"(limit {MAX_CHANGED_RATIO:.0%})."
        )
    return problems
