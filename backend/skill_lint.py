"""Static lint of the generated SKILL.md artifact.

``topology.py`` validates the SHAPE of a skill -- frontmatter, section
presence, parent/child resolution. Nothing validated its CONTENT: the sample
code was never parsed, the declared variables were never reconciled with the
code that reads them, and a scenario's return-branch table was only ever
checked against the child by executing the child for real. Everything this
module finds used to ship silently.

Rules are namespaced by the layer they apply to: ``A*`` for capability skills
(the artifact that actually runs), ``B*`` for scenario skills (the artifact
that instructs the host) and ``I*`` for the verified-actor identity contract.
Only A1 is an error -- a python block that does not parse is never intentional
and the host executes it verbatim. Everything else is a warning, because a
content check that can reject a draft would be worse than the drift it catches.

The scenario layer no longer restates a child's field contract, so the rule that
required it to (B3) is gone; ``topology.py`` P6 now flags the opposite. That id
stays retired -- the operation-coverage rule added later is B4.

A2 compares two sets, which made it satisfiable by emptying both: drop the
``os.environ`` read, declare nothing, and the artifact becomes a script the host
runs to no effect. A9-A11 cover that gap by checking the entry point's shape
(A9), that a caller has a channel at all (A10) and that every runtime input can
report its own absence (A11).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Collection, Iterable, Mapping, Sequence

from .models import SkillKind
from .sections import h2_sections, normalize_section

ENV_SECTION = "Environment Variables"
OBO_SECTION = "OBO Token Scopes"
INPUTS_SECTION = "Required Inputs"
# The platform injects this after a successful OBO exchange; a skill never
# declares it as an ACA variable and never accepts it from the caller.
IDENTITY_SECTION = "身分使用規範"
DEPLOYMENT_SECTION = "部署設定使用規範"
VERIFIED_UPN_VAR = "EAA_VERIFIED_USER_UPN"
_IDENTITY_RULE_IDS = ("R1", "R2", "R3", "R4")
_DEPLOYMENT_RULE_IDS = ("D1", "D2", "D3")
_LOGGING_FUNCS = frozenset(
    {"print", "debug", "info", "warning", "error", "exception", "critical", "log"}
)
_RECOVERING_HANDLERS = frozenset({"KeyError", "Exception", "BaseException"})

_PY_FENCE_RE = re.compile(r"```(?:python|py)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Declared variables are written as a backticked ALL-CAPS token in a bullet.
_DECLARED_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_NEEDS_INFO_RE = re.compile(r"\[NEEDS_INFO\][ \t]*missing=([A-Za-z0-9_,\- ]+)")
# The f-string prefix a `needs_info(code, ...)` helper prints; the code itself is
# a placeholder, so the regex above finds nothing and only the AST can resolve it.
_NEEDS_INFO_TEMPLATE = "[NEEDS_INFO] missing="
_ERROR_KEY_RE = re.compile(r"[\"']error[\"'][ \t]*:[ \t]*[\"']([A-Za-z0-9_]+)[\"']")
_SQL_THROW_RE = re.compile(r"\bTHROW[ \t]+\d|\bRAISERROR\b", re.IGNORECASE)
_STEP_ROW_RE = re.compile(r"^\|[ \t]*(\d+)[ \t]*\|", re.MULTILINE)
_STEP_HEADING_RE = re.compile(r"^Step[ \t]+(\d+)", re.IGNORECASE)
# The conventional payload key a child capability skill dispatches on.
_OPERATION_KEY = "operation"

# --- The caller-facing value domain (A6-A8) --------------------------------
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]+)`")
_LOWER_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_VALUE_TOKEN_RE = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'")
_ALTERNATION_RE = re.compile(r"([\w-]+(?:[ \t]*\|[ \t]*[\w-]+)+)")
_BARE_LIST_RE = re.compile(r"[(\[（［]\s*([\w-]+(?:\s*[,，、]\s*[\w-]+)+)\s*[)\]）］]")
_LIST_SEPARATOR_RE = re.compile(r"[,，、|]")
_OPEN_BRACKETS = "([{（［【「"
_CLOSE_BRACKETS = ")]}）］】」"
_ANNOTATION_MAX_CHARS = 500
# A date is string-encoded on the wire, so it needs a stated format exactly the
# way a free-text field needs a stated value set.
_STRING_TYPE_WORDS = frozenset(
    {"字串", "文字", "字元", "日期", "時間", "string", "str", "text", "date", "datetime"}
)
_OTHER_TYPE_WORDS = frozenset(
    {
        "數字", "整數", "浮點", "小數", "布林", "陣列", "列表", "清單", "物件", "字典",
        "number", "int", "integer", "float", "decimal", "bool", "boolean",
        "list", "array", "object", "dict", "json",
    }
)
_TYPE_WORDS = _STRING_TYPE_WORDS | _OTHER_TYPE_WORDS
_OBLIGATION_WORDS = frozenset(
    {"必填", "選填", "可選", "條件式", "required", "optional", "conditional", "nullable"}
)
_NON_VALUE_WORDS = frozenset(w.lower() for w in _TYPE_WORDS | _OBLIGATION_WORDS)
_VALUE_DOMAIN_PHRASES = frozenset(
    {
        "允許值", "合法值", "可用值", "值域", "列舉", "其中之一", "只能", "限定",
        "格式", "單位", "範圍", "自由文字", "任意字串", "大小寫",
        "one of", "must be", "enum", "allowed values", "legal values", "free text",
        "free-form", "arbitrary", "case-sensitive", "case-insensitive",
        "format", "unit", "range", "pattern",
    }
)
_SQL_SINK_ATTRS = frozenset({"execute", "executemany", "executescript", "callproc"})
_NUMERIC_COERCIONS = frozenset({"int", "float", "Decimal", "round"})
_HTTP_SINK_ATTRS = frozenset({"get", "post", "put", "patch", "delete", "request", "send"})
_HTTP_RECEIVERS = frozenset(
    {"requests", "httpx", "session", "client", "http", "aiohttp", "urllib"}
)


def _word_pattern(words: Iterable[str]) -> re.Pattern[str]:
    """One pattern for a vocabulary: ASCII entries need word boundaries, CJK cannot have them."""
    ascii_words = sorted((w for w in words if w.isascii()), key=len, reverse=True)
    cjk_words = sorted((w for w in words if not w.isascii()), key=len, reverse=True)
    parts = []
    if ascii_words:
        parts.append(r"\b(?:" + "|".join(re.escape(w) for w in ascii_words) + r")\b")
    if cjk_words:
        parts.append("(?:" + "|".join(re.escape(w) for w in cjk_words) + ")")
    return re.compile("|".join(parts), re.IGNORECASE)


_TYPE_WORD_RE = _word_pattern(_TYPE_WORDS)
_DOMAIN_PHRASE_RE = _word_pattern(_VALUE_DOMAIN_PHRASES)


@dataclass(frozen=True)
class LintIssue:
    rule: str
    message: str
    detail: str = ""
    severity: str = "warning"

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


def has_lint_errors(issues: Sequence[LintIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def lint_warning_count(issues: Sequence[LintIssue]) -> int:
    return sum(1 for issue in issues if issue.severity in {"error", "warning"})


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _quoted_list(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _normalize(text: str) -> str:
    """Lowercase, strip backticks, collapse whitespace -- for prose matching."""
    return " ".join(text.replace("`", " ").replace("*", " ").lower().split())


def _sections(skill_md: str) -> list[tuple[int, str, str]]:
    """Return ``(level, title, body)`` for every ``##``/``###`` heading."""
    text = skill_md or ""
    matches = list(_HEADING_RE.finditer(text))
    out: list[tuple[int, str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((len(match.group(1)), match.group(2).strip(), text[match.end() : end]))
    return out


def _section_body(skill_md: str, title_fragment: str) -> str:
    wanted = title_fragment.lower()
    return "\n".join(
        body for _level, title, body in _sections(skill_md) if wanted in title.lower()
    )


def _python_code(skill_md: str) -> str:
    return "\n".join(_PY_FENCE_RE.findall(skill_md or ""))


def _declared_names(section_body: str) -> list[str]:
    """Variable names declared in a section: backticked, ALL-CAPS-ish tokens."""
    return _unique(
        name
        for name in _DECLARED_NAME_RE.findall(section_body or "")
        if name.isupper() and len(name) > 2
    )


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_os_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_env_read_call(node: ast.Call) -> bool:
    """``os.environ.get(...)``, ``os.getenv(...)`` or a bare imported ``getenv(...)``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return (func.attr == "get" and _is_os_environ(func.value)) or func.attr == "getenv"
    return isinstance(func, ast.Name) and func.id == "getenv"


def _env_keys(tree: ast.AST) -> list[str]:
    """Every literal key read out of the environment by subscript, ``.get`` or ``getenv``."""
    keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.append(node.slice.value)
        elif isinstance(node, ast.Call) and node.args and _is_env_read_call(node):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.append(first.value)
    return _unique(keys)


def _needs_info_emitters(tree: ast.AST) -> set[str]:
    """Helpers that print the ``[NEEDS_INFO] missing=`` line from an argument.

    The inline form is plain text the regex already finds. This is the other
    idiom -- ``needs_info(code, explanation)`` -- whose code reaches the line as
    an f-string placeholder, so a text scan resolves nothing and the rules that
    read the emitted codes were silently vacuous on every skill written that way.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.JoinedStr):
                continue
            literal = "".join(
                part.value
                for part in sub.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if _NEEDS_INFO_TEMPLATE in literal:
                names.add(node.name)
                break
    return names


def _needs_info_codes(tree: ast.AST, code: str) -> list[str]:
    """Every ``missing=`` code the sample can print, both idioms included."""
    codes = [
        part.strip()
        for group in _NEEDS_INFO_RE.findall(code)
        for part in group.split(",")
        if part.strip()
    ]
    emitters = _needs_info_emitters(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        first = node.args[0]
        if name in emitters and isinstance(first, ast.Constant):
            if isinstance(first.value, str):
                codes.extend(part.strip() for part in first.value.split(",") if part.strip())
    return _unique(codes)


def _raised_names(tree: ast.AST) -> list[str]:
    """Named exception types in explicit ``raise`` statements, ``SystemExit`` aside."""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = (
            exc.id
            if isinstance(exc, ast.Name)
            else exc.attr
            if isinstance(exc, ast.Attribute)
            else ""
        )
        if name and name != "SystemExit":
            names.append(name)
    return _unique(names)


def _has_ancestor(node: ast.AST, parents: Mapping[ast.AST, ast.AST], kind: type) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, kind):
            return True
        current = parents.get(current)
    return False


def _env_reads_for(tree: ast.AST, wanted: Collection[str]) -> tuple[list[ast.AST], list[ast.AST]]:
    """Reads of the named env keys, split into indexed (strict) and defaulted (lenient)."""
    names = {name.upper() for name in wanted}
    strict: list[ast.AST] = []
    lenient: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value.upper() in names:
                    strict.append(node)
        elif isinstance(node, ast.Call) and node.args and _is_env_read_call(node):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value.upper() in names:
                    lenient.append(node)
    return strict, lenient


def _verified_upn_reads(tree: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """Reads of the verified UPN, split into indexed (R1-compliant) and defaulted."""
    return _env_reads_for(tree, {VERIFIED_UPN_VAR})


def _in_recovering_try(tree: ast.AST, reads: Sequence[ast.AST]) -> bool:
    """Whether any of ``reads`` sits in a ``try`` whose handler swallows the absence."""
    read_ids = {id(node) for node in reads}
    if not read_ids:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        if not any(id(inner) in read_ids for stmt in node.body for inner in ast.walk(stmt)):
            continue
        if any(
            handler.type is None
            or (isinstance(handler.type, ast.Name) and handler.type.id in _RECOVERING_HANDLERS)
            for handler in node.handlers
        ):
            return True
    return False


def _aliases_of(tree: ast.AST, read_ids: set[int]) -> set[str]:
    """Local names bound directly to a verified-UPN read."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and id(node.value) in read_ids:
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if id(node.value) in read_ids and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


# ---------------------------------------------------------------------------
# Caller field helpers
# ---------------------------------------------------------------------------


def _payload_field_reads(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Field name -> the nodes reading it out of caller data rather than ``os.environ``."""
    reads: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        name = ""
        if isinstance(node, ast.Subscript) and not _is_os_environ(node.value):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                name = index.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and not _is_os_environ(node.func.value) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value
        # ALL-CAPS names are the runtime/env variables A2 already reconciles.
        if name and _FIELD_NAME_RE.match(name) and not name.isupper():
            reads.setdefault(name, []).append(node)
    return reads


def _field_aliases(tree: ast.AST, nodes: Sequence[ast.AST]) -> set[str]:
    """Local names bound to an expression that contains one of ``nodes``.

    Containment rather than identity, because the value reaching a query is
    usually normalized on the way (``payload["x"].strip().lower()``).
    """
    read_ids = {id(node) for node in nodes}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        if not any(id(sub) in read_ids for sub in ast.walk(value)):
            continue
        for target in targets:
            names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    return names


def _string_elements(node: ast.AST) -> list[str]:
    """The string constants of a literal collection, or a dict's string keys."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    if isinstance(node, ast.Dict):
        return [
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
    return []


def _string_collections(tree: ast.AST) -> dict[str, list[str]]:
    """Name -> string members, for the constant sets a membership test points at."""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        values = _string_elements(node.value)
        if values:
            out[target.id] = values
    return out


def _payload_literal_sets(
    tree: ast.AST, reads: Mapping[str, list[ast.AST]], aliases: Mapping[str, set[str]]
) -> dict[str, list[str]]:
    """Field -> the literal values the code compares it against, i.e. its real value domain."""
    collections = _string_collections(tree)
    owner: dict[str, str] = {}
    for field, names in aliases.items():
        for name in names:
            owner.setdefault(name, field)
    by_node = {id(node): field for field, nodes in reads.items() for node in nodes}

    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        field = by_node.get(id(node.left))
        if field is None and isinstance(node.left, ast.Name):
            field = owner.get(node.left.id)
        if field is None:
            continue
        values: list[str] = []
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    values.append(comparator.value)
            elif isinstance(op, (ast.In, ast.NotIn)):
                if isinstance(comparator, ast.Name):
                    values += collections.get(comparator.id, [])
                else:
                    values += _string_elements(comparator)
        if values:
            found[field] = _unique(found.get(field, []) + values)
    return found


def _query_bound_fields(
    tree: ast.AST, reads: Mapping[str, list[ast.AST]], aliases: Mapping[str, set[str]]
) -> set[str]:
    """Fields whose value reaches a SQL statement or an outbound HTTP call.

    The scope limiter for A7/A8: a field that only feeds a local branch or a
    printed line cannot silently match zero rows.
    """
    sinks: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        receiver = node.func.value.id.lower() if isinstance(node.func.value, ast.Name) else ""
        if attr in _SQL_SINK_ATTRS or (attr in _HTTP_SINK_ATTRS and receiver in _HTTP_RECEIVERS):
            sinks.extend(list(node.args) + [kw.value for kw in node.keywords])
    if not sinks:
        return set()

    node_ids: set[int] = set()
    names: set[str] = set()
    for argument in sinks:
        for sub in ast.walk(argument):
            node_ids.add(id(sub))
            if isinstance(sub, ast.Name):
                names.add(sub.id)
    return {
        field
        for field, nodes in reads.items()
        if any(id(node) in node_ids for node in nodes) or (aliases.get(field, set()) & names)
    }


def _numeric_fields(tree: ast.AST, reads: Mapping[str, list[ast.AST]]) -> set[str]:
    """Fields the code itself coerces to a number, so the type already bounds the value."""
    coerced: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _NUMERIC_COERCIONS:
            continue
        for argument in node.args:
            coerced.update(id(sub) for sub in ast.walk(argument))
    return {
        field for field, nodes in reads.items() if any(id(node) in coerced for node in nodes)
    }


def _field_annotations(inputs_body: str, field: str) -> list[str]:
    """Every span of contract prose that documents ``field``.

    A span runs from the field's backticked mention to the next field mention at
    bracket depth zero, so ``(`annual`, `sick`)`` stays with the field it
    constrains while ``、`start_date``` does not.
    """
    spans: list[str] = []
    needle = f"`{field}`"
    start = inputs_body.find(needle)
    while start != -1:
        cursor = start + len(needle)
        depth = 0
        end = cursor
        while end < len(inputs_body) and end - cursor < _ANNOTATION_MAX_CHARS:
            char = inputs_body[end]
            if char == "\n" and depth == 0:
                break
            if char in _OPEN_BRACKETS:
                depth += 1
            elif char in _CLOSE_BRACKETS:
                depth = max(depth - 1, 0)
            elif char == "`" and depth == 0:
                token = _BACKTICK_TOKEN_RE.match(inputs_body, end)
                if token and _LOWER_IDENT_RE.match(token.group(1)):
                    break
            end += 1
        spans.append(inputs_body[cursor:end])
        start = inputs_body.find(needle, cursor)
    return spans


def _documented_values(annotation: str) -> list[str]:
    """The literal values or format patterns an annotation states for its field."""
    raw: list[str] = []
    for match in _VALUE_TOKEN_RE.finditer(annotation):
        raw.append(next(group for group in match.groups() if group))
    for match in _ALTERNATION_RE.finditer(annotation):
        raw += match.group(1).split("|")
    for match in _BARE_LIST_RE.finditer(annotation):
        raw += _LIST_SEPARATOR_RE.split(match.group(1))
    return _unique(
        value for value in (v.strip() for v in raw) if value and value.lower() not in _NON_VALUE_WORDS
    )


def _states_a_value_domain(annotation: str) -> bool:
    return bool(_documented_values(annotation)) or bool(_DOMAIN_PHRASE_RE.search(annotation))


def _is_string_shaped(annotation: str) -> bool:
    """A field declared as a string or date -- or declared with no type at all."""
    declared = {word.lower() for word in _TYPE_WORD_RE.findall(annotation)}
    return not declared or bool(declared & {w.lower() for w in _STRING_TYPE_WORDS})


# ---------------------------------------------------------------------------
# Capability rules
# ---------------------------------------------------------------------------


def _lint_capability(skill_md: str, code_override: str | None = None) -> list[LintIssue]:
    issues: list[LintIssue] = []
    code = _python_code(skill_md) if code_override is None else code_override
    if not code.strip():
        return issues

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            LintIssue(
                rule="A1",
                severity="error",
                message=(
                    "The sample code block is not valid Python: "
                    f"{exc.msg} (line {exc.lineno}). The host executes this block verbatim."
                ),
                detail=f"line {exc.lineno}: {exc.msg}",
            )
        ]

    issues.extend(_check_variable_closure(skill_md, tree))
    issues.extend(_check_explicit_raises(tree))
    issues.extend(_check_needs_info_documented(skill_md, code, tree))
    issues.extend(_check_main_signature(tree))
    issues.extend(_check_runtime_input_channel(skill_md, tree))
    issues.extend(_check_runtime_needs_info_codes(skill_md, tree, code))
    issues.extend(_check_caller_field_contract(skill_md, tree))
    issues.extend(_check_uncaught_sql_throw(tree))
    issues.extend(_check_unchecked_call_result(tree))
    issues.extend(_check_deployment_config(skill_md, tree, code))
    issues.extend(_check_identity_contract(skill_md, tree))
    return issues


def _check_variable_closure(skill_md: str, tree: ast.AST) -> list[LintIssue]:
    """A2 -- declared variables and the keys the code reads must be the same set."""
    declared = _unique(
        _declared_names(_section_body(skill_md, ENV_SECTION))
        + _declared_names(_section_body(skill_md, OBO_SECTION))
        + _declared_names(_section_body(skill_md, INPUTS_SECTION))
        + _declared_names(_section_body(skill_md, IDENTITY_SECTION))
    )
    used = _env_keys(tree)
    used_folded = {key.upper() for key in used}
    declared_folded = {name.upper() for name in declared}

    issues: list[LintIssue] = []
    for name in declared:
        if name.upper() not in used_folded:
            issues.append(
                LintIssue(
                    rule="A2",
                    message=(
                        f"`{name}` is declared in the variable sections but the sample code "
                        "never reads it from `os.environ`."
                    ),
                    detail=name,
                )
            )
    for key in used:
        if key.upper() not in declared_folded:
            # I1 owns the reserved identity variable; A2 would only duplicate it.
            if key.upper() == VERIFIED_UPN_VAR:
                continue
            issues.append(
                LintIssue(
                    rule="A2",
                    message=(
                        f"The sample code reads `os.environ` key `{key}`, which is declared in "
                        "none of Required Inputs / Environment Variables / OBO Token Scopes. "
                        "Declare it -- deleting the read is not the fix, it removes the channel "
                        "the value arrives through."
                    ),
                    detail=key,
                )
            )
    return issues


def _check_explicit_raises(tree: ast.AST) -> list[LintIssue]:
    """A3 -- a non-``SystemExit`` raise exits non-zero, i.e. reads as a deployment error."""
    names = _raised_names(tree)
    if not names:
        return []
    return [
        LintIssue(
            rule="A3",
            message=(
                f"The sample code raises {', '.join('`' + n + '`' for n in names)}. A raise that "
                "is not `SystemExit` exits non-zero, which the host reads as a DEPLOYMENT "
                "failure. A missing caller input belongs in `[NEEDS_INFO]` + `SystemExit(0)`, "
                "and a business rejection belongs in the error taxonomy; only `aca_env` / "
                "`obo_token` absence may exit non-zero."
            ),
            detail=", ".join(names),
        )
    ]


def _needs_info_contract(skill_md: str) -> str:
    """Where a caller may learn the ``missing=`` codes: the field contract or its own section."""
    return "\n".join(
        body
        for title, body in h2_sections(skill_md)
        if "needsinfo" in normalize_section(title)
    )


def _check_needs_info_documented(skill_md: str, code: str, tree: ast.AST) -> list[LintIssue]:
    """A4 -- every ``[NEEDS_INFO] missing=X`` code must be documented in a named section."""
    inputs = _section_body(skill_md, INPUTS_SECTION)
    contract = _needs_info_contract(skill_md)
    documented = f"{inputs}\n{contract}"
    if not documented.strip():
        return []
    codes = _needs_info_codes(tree, code)
    return [
        LintIssue(
            rule="A4",
            message=(
                f"The sample code can print `[NEEDS_INFO] missing={item}`, but `{item}` appears in "
                "neither `## Required Inputs` nor a section dedicated to the `[NEEDS_INFO]` "
                "contract. The caller has no way to learn what to send."
            ),
            detail=item,
        )
        for item in codes
        if item not in documented
    ]


def _runtime_env_reads(skill_md: str, tree: ast.AST) -> list[str]:
    """Env keys that carry CALLER data: not deployment config, not the identity."""
    reserved = {VERIFIED_UPN_VAR}
    reserved.update(name.upper() for name in _declared_names(_section_body(skill_md, ENV_SECTION)))
    reserved.update(name.upper() for name in _declared_names(_section_body(skill_md, OBO_SECTION)))
    return [key for key in _env_keys(tree) if key.upper() not in reserved]


def _check_main_signature(tree: ast.AST) -> list[LintIssue]:
    """A9 -- ``main`` takes no parameters, because nothing ever calls it with any."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "main":
            continue
        args = node.args
        params = [arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        if args.vararg:
            params.append(f"*{args.vararg.arg}")
        if args.kwarg:
            params.append(f"**{args.kwarg.arg}")
        if not params:
            continue
        return [
            LintIssue(
                rule="A9",
                message=(
                    f"`main` takes {_quoted_list(params)}. The host executes this block verbatim "
                    "as a script, so nothing ever calls `main` with an argument: a parameter "
                    "writes an unconfirmed host mechanism into the artifact as a settled "
                    "contract, and the value it names can never arrive. Caller data reaches the "
                    "skill as a serialized JSON string in an environment variable -- read it "
                    "inside `main` via `os.environ`."
                ),
                detail=", ".join(params),
            )
        ]
    return []


def _check_runtime_input_channel(skill_md: str, tree: ast.AST) -> list[LintIssue]:
    """A10 -- code that consumes caller fields needs a channel the caller can use.

    A2 is a set comparison, so it is equally satisfied by shrinking BOTH sets to
    empty: drop the ``os.environ`` read, declare nothing, and the artifact is a
    script the host runs to no effect. That is worse than the drift A2 catches
    and nothing else looked at it.
    """
    if _runtime_env_reads(skill_md, tree):
        return []
    fields = list(_payload_field_reads(tree))
    if not fields:
        return []
    return [
        LintIssue(
            rule="A10",
            message=(
                f"The sample code consumes caller fields ({_quoted_list(fields)}) but never reads "
                "a runtime input from the environment, so nothing the caller sends can reach it. "
                "The host passes caller data as a serialized JSON string in a `credentials` "
                "entry, which arrives as an environment variable: declare that variable in "
                "`## Required Inputs` and read it with `os.environ`."
            ),
            detail=", ".join(fields),
        )
    ]


def _check_runtime_needs_info_codes(
    skill_md: str, tree: ast.AST, code: str
) -> list[LintIssue]:
    """A11 -- a runtime input the caller can omit needs a ``missing=`` code of its own.

    Keyed on the READ rather than on the declaration, so the envelope variable is
    covered without tracing the value into ``json.loads`` -- a trace that breaks
    at the first helper boundary. Case folding collapses the
    ``get("x") or get("X")`` pair into the one code that satisfies both.
    """
    emitted = {item.upper() for item in _needs_info_codes(tree, code)}
    return [
        LintIssue(
            rule="A11",
            message=(
                f"The sample code reads the runtime input `{key}` from the environment but can "
                f"never print `[NEEDS_INFO] missing={key}`. When the host omits it -- most often "
                "by putting the payload in the free-text `request` parameter instead of a "
                "`credentials` entry -- the caller gets no readable signal at all: the script "
                "exits silently or crashes on a value that was never there."
            ),
            detail=key,
        )
        for key in _unique(read.upper() for read in _runtime_env_reads(skill_md, tree))
        if key not in emitted
    ]


def _check_caller_field_contract(skill_md: str, tree: ast.AST) -> list[LintIssue]:
    """A6-A8 -- a caller field's value domain must be stated, not implied.

    A field documented as "a string" gives the caller nothing to convert the
    user's own wording into. The user's word goes on the wire, the query matches
    no row, and the skill reports a business condition -- so a broken contract
    surfaces as "you have no entitlement" and sends the reader after the data.
    The constraint used to be carried implicitly by a payload EXAMPLE in the
    calling scenario skill; once the scenario stopped restating the contract,
    nothing was left holding it.
    """
    inputs = _section_body(skill_md, INPUTS_SECTION)
    if not inputs.strip():
        return []
    reads = _payload_field_reads(tree)
    if not reads:
        return []

    aliases = {field: _field_aliases(tree, nodes) for field, nodes in reads.items()}
    literals = _payload_literal_sets(tree, reads, aliases)
    bound = _query_bound_fields(tree, reads, aliases) - _numeric_fields(tree, reads)
    documented = f"{inputs}\n{_needs_info_contract(skill_md)}"

    issues: list[LintIssue] = []
    for field, values in literals.items():
        undocumented = [value for value in values if value not in documented]
        if len(values) < 2 or not undocumented:
            continue
        issues.append(
            LintIssue(
                rule="A6",
                message=(
                    f"The sample code accepts only {_quoted_list(values)} for `{field}`, but "
                    f"{_quoted_list(undocumented)} appears nowhere in the field contract. The "
                    "caller cannot send a value it was never told exists."
                ),
                detail=f"{field}: {', '.join(undocumented)}",
            )
        )

    for field in reads:
        if field not in bound:
            continue
        spans = _field_annotations(inputs, field)
        if not spans:
            issues.append(
                LintIssue(
                    rule="A7",
                    message=(
                        f"The sample code puts `{field}` into a query, but `## Required Inputs` "
                        "never names it. The caller has no way to learn the field exists, let "
                        "alone which values it accepts."
                    ),
                    detail=field,
                )
            )
            continue
        annotation = " ".join(spans)
        if not _states_a_value_domain(annotation):
            if _is_string_shaped(annotation):
                issues.append(
                    LintIssue(
                        rule="A7",
                        message=(
                            f"`{field}` reaches a query with a contract that states its TYPE but "
                            "not its VALUE DOMAIN. Give the legal values verbatim as they go on "
                            "the wire, or the format/unit/casing, or say the field is free text. "
                            "A caller working from the user's own words will send those words."
                        ),
                        detail=field,
                    )
                )
            continue
        values = _documented_values(annotation)
        if len(values) >= 2 and field not in literals:
            issues.append(
                LintIssue(
                    rule="A8",
                    message=(
                        f"The contract limits `{field}` to {_quoted_list(values)}, but the code "
                        "never checks the value before querying with it. An illegal value has to "
                        "be a `[NEEDS_INFO]` line; letting it through returns an empty result the "
                        "skill then reports as a business condition."
                    ),
                    detail=field,
                )
            )
    return issues


def _check_uncaught_sql_throw(tree: ast.AST) -> list[LintIssue]:
    """A5 -- a SQL ``THROW``/``RAISERROR`` outside a ``try`` crashes on a business condition."""
    parents = _parents(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not _SQL_THROW_RE.search(node.value):
            continue
        if _has_ancestor(node, parents, ast.Try):
            continue
        return [
            LintIssue(
                rule="A5",
                message=(
                    "A SQL statement raises `THROW` / `RAISERROR` but the executing call is not "
                    "inside a `try`. The driver exception escapes and turns a business condition "
                    "into a non-zero exit the host reports as a broken skill."
                ),
            )
        ]
    return []


def _check_unchecked_call_result(tree: ast.AST) -> list[LintIssue]:
    """A12 -- an external call whose result is never inspected before the skill reports success.

    Deliberately narrow: it does not try to decide which ``print`` is the success
    message, only whether the result of the call was looked at AT ALL. When it
    was not, every path to the end of the run is a success path, and under the
    EAA output contract a ``status="completed"`` response is shown to the user
    verbatim -- so a fabricated "submitted" line reaches them unquestioned, and a
    non-idempotent operation gets retried into a duplicate.
    """
    issues: list[LintIssue] = []
    for label, node in _external_calls(tree):
        result = _call_result_name(tree, node)
        if result is None:
            issues.append(_unchecked_issue(label, "its result is never assigned"))
            continue
        if not _result_is_inspected(tree, result, label):
            issues.append(_unchecked_issue(label, f"`{result}` is never inspected"))
    return _dedupe_issues(issues)


def _external_calls(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    """Calls whose outcome the caller must check: subprocess launches and HTTP requests."""
    found: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        receiver = func.value.id if isinstance(func.value, ast.Name) else ""
        if receiver == "subprocess" and func.attr in {"run", "call"}:
            # check=True already raises on a non-zero exit.
            if any(kw.arg == "check" and _is_true(kw.value) for kw in node.keywords):
                continue
            found.append((f"subprocess.{func.attr}", node))
        elif receiver.lower() in _HTTP_RECEIVERS and func.attr in _HTTP_SINK_ATTRS:
            found.append((f"{receiver}.{func.attr}", node))
    return found


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _call_result_name(tree: ast.AST, call: ast.Call) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.value is call:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets:
                return targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is call:
            if isinstance(node.target, ast.Name):
                return node.target.id
    return None


def _result_is_inspected(tree: ast.AST, name: str, label: str) -> bool:
    wanted = (
        {"returncode", "check_returncode"}
        if label.startswith("subprocess")
        else {"status_code", "ok", "raise_for_status", "status"}
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in wanted:
            if isinstance(node.value, ast.Name) and node.value.id == name:
                return True
    return False


def _unchecked_issue(label: str, why: str) -> LintIssue:
    return LintIssue(
        rule="A12",
        message=(
            f"`{label}(...)` is called but {why}, so the code reaches its success output "
            "whether the call succeeded or not. The host shows a completed response to the "
            "user verbatim, so an unconditional success line is a claim nobody verifies -- and "
            "a create/submit operation the user believes failed gets sent twice. Check the "
            "outcome (`check=True`, `returncode`, `raise_for_status()`) before saying anything "
            "succeeded, or print the tool's own output instead of composing a verdict."
        ),
        detail=label,
    )


def _dedupe_issues(issues: Sequence[LintIssue]) -> list[LintIssue]:
    seen: dict[tuple[str, str], LintIssue] = {}
    for issue in issues:
        seen.setdefault((issue.rule, issue.detail), issue)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Deployment configuration rules
# ---------------------------------------------------------------------------


def _deployment_names(skill_md: str) -> list[str]:
    return _unique(
        _declared_names(_section_body(skill_md, ENV_SECTION))
        + _declared_names(_section_body(skill_md, OBO_SECTION))
    )


def _check_deployment_config(skill_md: str, tree: ast.AST, code: str) -> list[LintIssue]:
    """D1-D3 -- deployment config must fail loudly, never look like a missing caller input.

    ``[NEEDS_INFO]`` + exit 0 tells the host "ask the caller and retry". A broken
    deployment or a broken OBO chain is not something a caller can supply, so
    every shape that routes it there makes the host retry forever against an
    environment nobody was told is misconfigured.
    """
    names = _deployment_names(skill_md)
    if not names:
        return []
    strict, lenient = _env_reads_for(tree, names)
    issues: list[LintIssue] = []

    lenient_keys = _unique(
        arg.value
        for node in lenient
        if isinstance(node, ast.Call) and node.args
        for arg in [node.args[0]]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    )
    for key in lenient_keys:
        issues.append(
            LintIssue(
                rule="D1",
                message=(
                    f"`{key}` is deployment configuration but is read with `.get()` / "
                    "`os.getenv()`. D1 requires `os.environ[...]` indexing: a default turns a "
                    "misconfigured deployment into an empty string the code then works with."
                ),
                detail=key,
            )
        )

    emitted = {item.upper() for item in _needs_info_codes(tree, code)}
    for name in names:
        if name.upper() in emitted:
            issues.append(
                LintIssue(
                    rule="D2",
                    message=(
                        f"`{name}` is deployment configuration but appears in a "
                        f"`[NEEDS_INFO] missing={name}` line. D2 forbids asking the caller for "
                        "it: nobody on that side can set an ACA variable or an OBO scope, so "
                        "the host retries forever and the real fault is never reported."
                    ),
                    detail=name,
                )
            )

    if _in_recovering_try(tree, strict):
        issues.append(
            LintIssue(
                rule="D3",
                message=(
                    "A deployment configuration lookup sits in a `try` that recovers. D3 "
                    "requires a non-zero exit: recovering here substitutes a fallback "
                    "credential or an empty value for configuration that is simply absent."
                ),
                detail=", ".join(names),
            )
        )

    if issues and DEPLOYMENT_SECTION not in skill_md:
        issues.append(
            LintIssue(
                rule="D1",
                message=(
                    f"The file has no `## {DEPLOYMENT_SECTION}` section. The runtime writes its "
                    "own script from this prose, so correcting only the sample code leaves the "
                    "rule unstated and the next generated script repeats the mistake. Add the "
                    "section with the verbatim `D1`-`D3` rules."
                ),
                detail=DEPLOYMENT_SECTION,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Identity contract rules
# ---------------------------------------------------------------------------


def _check_identity_contract(skill_md: str, tree: ast.AST) -> list[LintIssue]:
    """I1-I4 -- the four rules that govern the platform-verified actor string."""
    strict, lenient = _verified_upn_reads(tree)
    if not strict and not lenient:
        return []
    issues = _check_identity_section(skill_md)
    issues.extend(_check_identity_read_shape(lenient))
    issues.extend(_check_identity_keyerror_recovery(tree, strict))
    issues.extend(_check_identity_leak(tree, strict + lenient))
    return issues


def _check_identity_section(skill_md: str) -> list[LintIssue]:
    """I1 -- reading the verified UPN obliges the file to carry all four rules."""
    body = _section_body(skill_md, IDENTITY_SECTION)
    if not body.strip():
        return [
            LintIssue(
                rule="I1",
                message=(
                    f"The sample code reads `{VERIFIED_UPN_VAR}` but the file has no "
                    "`## Skill 身分使用規範` section. Those rules are the only thing stopping a "
                    "later edit from falling back to an unverified identity source."
                ),
                detail=IDENTITY_SECTION,
            )
        ]
    missing = [rule for rule in _IDENTITY_RULE_IDS if rule not in body]
    if not missing:
        return []
    return [
        LintIssue(
            rule="I1",
            message=(
                "`## Skill 身分使用規範` is missing "
                + ", ".join(f"`{rule}`" for rule in missing)
                + ". All four rules must be reproduced; a partial contract is the one that "
                "gets argued with."
            ),
            detail=", ".join(missing),
        )
    ]


def _check_identity_read_shape(lenient: Sequence[ast.AST]) -> list[LintIssue]:
    """I2 -- R1: the verified UPN is read by index, never with a default."""
    if not lenient:
        return []
    return [
        LintIssue(
            rule="I2",
            message=(
                f"`{VERIFIED_UPN_VAR}` is read with `.get()` / `os.getenv()`. R1 requires "
                f"`os.environ[...]` indexing: a default disguises an absent identity as an "
                "empty string, and that value is about to be used as the authorization subject."
            ),
            detail=VERIFIED_UPN_VAR,
        )
    ]


def _check_identity_keyerror_recovery(
    tree: ast.AST, strict: Sequence[ast.AST]
) -> list[LintIssue]:
    """I3 -- R2: absence must abort, so the read may not sit in a recovering ``try``."""
    if not _in_recovering_try(tree, strict):
        return []
    return [
        LintIssue(
            rule="I3",
            message=(
                f"The `{VERIFIED_UPN_VAR}` lookup sits in a `try` that catches its "
                "`KeyError`. R2 requires the skill to abort and report instead: every "
                "way of recovering here substitutes an unverified identity."
            ),
            detail=VERIFIED_UPN_VAR,
        )
    ]


def _check_identity_leak(tree: ast.AST, reads: Sequence[ast.AST]) -> list[LintIssue]:
    """I4 -- R4: the verified UPN is never written to output or to a log."""
    read_ids = {id(node) for node in reads}
    aliases = _aliases_of(tree, read_ids)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if target not in _LOGGING_FUNCS:
            continue
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        leaked = any(
            id(sub) in read_ids or (isinstance(sub, ast.Name) and sub.id in aliases)
            for arg in arguments
            for sub in ast.walk(arg)
        )
        if leaked:
            return [
                LintIssue(
                    rule="I4",
                    message=(
                        f"`{VERIFIED_UPN_VAR}` is passed to `{target}(...)`. R4 forbids writing "
                        "the verified identity to a log or to the skill's output."
                    ),
                    detail=target,
                )
            ]
    return []


# ---------------------------------------------------------------------------
# Scenario rules
# ---------------------------------------------------------------------------


def _lint_scenario(
    skill_md: str,
    *,
    child_full_md: Mapping[str, str] | None,
    host_capabilities: Sequence[str],
) -> list[LintIssue]:
    issues = _check_step_host_capability(skill_md, host_capabilities)
    for name, child_md in (child_full_md or {}).items():
        issues.extend(_check_return_branches(skill_md, name, child_md))
        issues.extend(_check_operation_coverage(skill_md, name, child_md))
    return issues


def _step_capability_flags(skill_md: str) -> dict[int, bool]:
    """Step number -> whether its overview row claims a host capability is needed."""
    for _level, title, body in _sections(skill_md):
        if "step overview" not in title.lower():
            continue
        rows = [line for line in body.splitlines() if line.strip().startswith("|")]
        header = next((r for r in rows if "host capability" in r.lower()), "")
        if not header:
            continue
        columns = [c.strip() for c in header.strip().strip("|").split("|")]
        index = next(
            (i for i, c in enumerate(columns) if "host capability" in c.lower()), None
        )
        if index is None:
            continue
        flags: dict[int, bool] = {}
        for row in rows:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) <= index or not _STEP_ROW_RE.match(row):
                continue
            flags[int(cells[0])] = cells[index].lower().startswith("yes")
        return flags
    return {}


def _check_step_host_capability(
    skill_md: str, host_capabilities: Sequence[str]
) -> list[LintIssue]:
    """B1 -- a step whose detail names a host capability must be marked as needing one."""
    capabilities = [c for c in (str(c).strip() for c in host_capabilities or []) if c]
    flags = _step_capability_flags(skill_md)
    if not capabilities or not flags:
        return []

    issues: list[LintIssue] = []
    seen: set[int] = set()
    for _level, title, body in _sections(skill_md):
        match = _STEP_HEADING_RE.match(title.strip())
        if not match:
            continue
        step = int(match.group(1))
        if flags.get(step, False) or step in seen:
            continue
        normalized = _normalize(body)
        for capability in capabilities:
            if _normalize(capability) and _normalize(capability) in normalized:
                seen.add(step)
                issues.append(
                    LintIssue(
                        rule="B1",
                        message=(
                            f"Step {step} is marked as needing no host capability, but its "
                            f"detail section tells the host to use `{capability}`. The host "
                            "cannot plan a step the overview table says it is not involved in."
                        ),
                        detail=f"step {step}: {capability}",
                    )
                )
                break
    return issues


def _child_branches(child_md: str) -> list[str]:
    """Every ``[NEEDS_INFO]`` code and business error key the child can return."""
    code = _python_code(child_md)
    codes = [
        part.strip()
        for group in _NEEDS_INFO_RE.findall(code)
        for part in group.split(",")
        if part.strip()
    ]
    return _unique(codes + _ERROR_KEY_RE.findall(code))


def _check_return_branches(skill_md: str, child: str, child_md: str) -> list[LintIssue]:
    """B2 -- the return-branch table must be exhaustive over the child's real outputs."""
    table = _section_body(skill_md, "return-branch") or _section_body(skill_md, "return branch")
    if not table.strip():
        return []
    return [
        LintIssue(
            rule="B2",
            message=(
                f"Child `{child}` can return `{branch}`, but the return-branch table has no row "
                "for it. The host has no defined next action for that branch."
            ),
            detail=f"{child}: {branch}",
        )
        for branch in _child_branches(child_md)
        if branch not in table
    ]


def _is_operation_expr(node: ast.AST) -> bool:
    """Whether ``node`` reads the caller-selected operation out of the payload."""
    if isinstance(node, ast.Name):
        return node.id == _OPERATION_KEY
    if isinstance(node, ast.Subscript):
        index = node.slice
        return isinstance(index, ast.Constant) and index.value == _OPERATION_KEY
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr == "get"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == _OPERATION_KEY
        )
    return False


def _child_operations(child_md: str) -> list[str]:
    """Every operation the child's own sample code dispatches on.

    Derived from the code rather than the prose for the same reason B2 is: the
    code is what the host actually runs. A child that does not dispatch on an
    operation at all yields nothing and the coverage rule stays silent.
    """
    try:
        tree = ast.parse(_python_code(child_md))
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not _is_operation_expr(node.left):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(
                comparator, (ast.Set, ast.List, ast.Tuple)
            ):
                found += [
                    e.value
                    for e in comparator.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
            elif isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                if isinstance(comparator.value, str):
                    found.append(comparator.value)
    return _unique(found)


def _check_operation_coverage(skill_md: str, child: str, child_md: str) -> list[LintIssue]:
    """B4 -- every operation the child supports must be accounted for in the body.

    Matched against the whole body rather than the request-type section, because
    section titles are free prose in any language: anchoring on an English
    heading would make the rule silently vacuous on the files that need it most.
    The cost is that a passing mention counts as coverage, which keeps this to
    under-reporting rather than false alarms.
    """
    body = _normalize(skill_md)
    return [
        LintIssue(
            rule="B4",
            message=(
                f"Child `{child}` supports the `{operation}` operation, but this scenario "
                "never mentions it. The host cannot reach that capability through this "
                "skill; if it is out of scope, say so in the not-applicable section."
            ),
            detail=f"{child}: {operation}",
        )
        for operation in _child_operations(child_md)
        if _normalize(operation) not in body
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def lint_skill(
    skill_md: str,
    kind: SkillKind,
    *,
    child_full_md: Mapping[str, str] | None = None,
    host_capabilities: Sequence[str] = (),
    code_override: str | None = None,
) -> list[LintIssue]:
    """Return every content issue in the artifact. Only A1 is an error.

    ``code_override`` swaps the python block for another script -- the code the
    runtime PREPARED for a test sample. That script is regenerated from the body
    prose, so it is a different artifact from the sample code and nothing else
    ever looked at it; the declarations it is reconciled against still come from
    ``skill_md``.
    """
    if not (skill_md or "").strip():
        return []
    if kind is SkillKind.SCENARIO:
        return _lint_scenario(
            skill_md, child_full_md=child_full_md, host_capabilities=host_capabilities
        )
    return _lint_capability(skill_md, code_override)
