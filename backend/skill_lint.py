"""Static lint of the generated SKILL.md artifact.

``topology.py`` validates the SHAPE of a skill -- frontmatter, section
presence, parent/child resolution. Nothing validated its CONTENT: the sample
code was never parsed, the declared variables were never reconciled with the
code that reads them, and a scenario's return-branch table was only ever
checked against the child by executing the child for real. Everything this
module finds used to ship silently.

Rules are namespaced by the layer they apply to: ``A*`` for capability skills
(the artifact that actually runs), ``B*`` for scenario skills (the artifact
that instructs the host), ``I*`` for the verified-actor identity contract and
``S*`` for script-form capability skills, whose code is a bundled script run
with argv rather than a sample block the runtime rewrites.
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
import json
import re
from dataclasses import dataclass
from typing import Collection, Iterable, Mapping, Sequence

from .models import SkillKind
from .eaa_platform import (
    CONTENT_ERROR_HARD_PATTERNS,
    MI_ENV_KEYS,
    MI_HARD_DENIED_RESOURCES,
    MI_IDENTITY_SELECTORS,
    PLATFORM_SECRET_DENYLIST,
    STATIC_OBO_REGISTRY_KEYS,
    declared_mi_scopes,
    mi_allowlist_note,
    mi_scope_allowlist,
    normalize_mi_resource,
    reserved_credentials_key_reason,
)
from .input_contract import parse_input_bindings
from .sections import h2_sections, normalize_section

ENV_SECTION = "Environment Variables"
OBO_SECTION = "OBO Token Scopes"
INPUTS_SECTION = "Required Inputs"
# Script form: ACA variables and OBO tokens are declared here instead of ENV/OBO.
PREREQUISITES_SECTION = "Prerequisites"
# Script form: the exit-code table and the output field tables.
RESULT_SECTION = "Reading the Result"
# Written by EAA's gatekeeper, not by the generator.
GATEKEEPER_ADDENDUM_SECTION = "Gatekeeper Addendum"
SCRIPT_EXIT_CODES = frozenset({0, 1, 3})
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
_PREREQ_BULLET_RE = re.compile(r"^[-*+][ \t]+`([A-Z_][A-Z0-9_]*)`")
_ANY_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]")
_OPTIONAL_RE = re.compile(r"\boptional\b", re.IGNORECASE)
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

# --- EAA platform rules (D4-D6, A15) ----------------------------------------
# EAA's tools/skill_lint.py regex: it scans prose too, because the runtime
# model follows prose. Do not narrow this to code.
_ENV_READ_TEXT_RE = re.compile(
    r"""os\.(?:environ\s*\[\s*|environ\.get\(\s*|getenv\(\s*)["']([A-Za-z_][A-Za-z0-9_]*)["']"""
)
_ANY_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_CREDENTIAL_PLACEHOLDER_RE = re.compile(r"\bcredential\s*=\s*(?:\.\.\.|None\b|<)")
_MI_CREDENTIAL_RE = re.compile(r"\b(?:DefaultAzureCredential|ManagedIdentityCredential)\b")
_DEFAULT_CREDENTIAL = "DefaultAzureCredential"
# EAA's own "uses Managed Identity" test.
_MI_CALL_RE = re.compile(r"\b(?:DefaultAzureCredential|ManagedIdentityCredential)\s*\(")
_MI_CREDENTIAL_CLASSES = frozenset({"DefaultAzureCredential", "ManagedIdentityCredential"})
# Any of these next to the MI is a fallback identity or a key replacing Entra.
_FALLBACK_CREDENTIALS = frozenset({
    "ChainedTokenCredential", "ClientSecretCredential", "CertificateCredential",
    "EnvironmentCredential", "AzureCliCredential", "AzurePowerShellCredential",
    "AzureDeveloperCliCredential", "UsernamePasswordCredential", "InteractiveBrowserCredential",
    "DeviceCodeCredential", "WorkloadIdentityCredential", "AzureKeyCredential", "AzureNamedKeyCredential",
    "AzureSasCredential",
})
_MI_ENDPOINT_FRAGMENTS = ("169.254.169.254", "/msi/token", "/metadata/identity/oauth2/token")

# --- EAA execution environment (E1-E4) --------------------------------------
# Under SUBPROCESS_UID_SANDBOX the script runs as a throwaway uid with no passwd
# entry, confined to its work_dir, and every process it started is killed.
_EXECUTION_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "E1",
        re.compile(r"""["'](/(?:app|tmp|home|root))(?=[/"'])"""),
        "The code uses an absolute path under `{match}`. The script may only read and write "
        "in its working directory: use a relative path, or `tempfile` (which lands in "
        "`TMPDIR`) for scratch files.",
    ),
    (
        "E2",
        re.compile(
            r"""\bpip3?\s+install\b|-m\s+pip\b|["']-m["']\s*,\s*["']pip3?["']|["']pip3?["']\s*,\s*["']install["']"""
        ),
        "The code installs a package at run time (`{match}`). Only packages already in the "
        "container are available; when one is missing, print `[NEEDS_INFO]` explaining it "
        "instead of installing it.",
    ),
    (
        "E3",
        re.compile(r"\b(?:getpwuid|getlogin)\b"),
        "The code depends on user-account information (`{match}`). The execution uid has no "
        "passwd entry; use `os.environ[\"HOME\"]` or `Path.home()` for the home directory.",
    ),
    (
        "E4",
        re.compile(
            r"\bnohup\b|\bsetsid\b|\bos\.fork\b|\bstart_new_session\s*=\s*True\b|\bdaemon\s*=\s*True\b"
        ),
        "The code starts a background or detached process (`{match}`). Every process is "
        "killed when the run ends, so all work must finish before the script exits.",
    ),
)

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


def _h2_body(skill_md: str, name: str) -> str:
    wanted = normalize_section(name)
    return "\n".join(
        body for title, body in h2_sections(skill_md or "") if wanted in normalize_section(title)
    )


def _without_gatekeeper_addendum(skill_md: str) -> str:
    """The file minus ``## Gatekeeper Addendum``, which the generator does not author."""
    wanted = normalize_section(GATEKEEPER_ADDENDUM_SECTION)
    head = (skill_md or "").partition("\n## ")[0]
    kept = [
        f"## {title}\n{body}"
        for title, body in h2_sections(skill_md or "")
        if wanted not in normalize_section(title)
    ]
    return "\n".join([head, *kept])


def _prerequisites(skill_md: str) -> dict[str, bool]:
    """Script form: name -> optional, from each top-level bullet's leading backticked name.

    Nested bullets are the details of their parent (permissions, notes), so an
    "optional" there does not make the parent variable optional.
    """
    found: dict[str, bool] = {}
    current: str | None = None
    for line in _h2_body(skill_md, PREREQUISITES_SECTION).splitlines():
        match = _PREREQ_BULLET_RE.match(line)
        if match:
            current = match.group(1)
            found.setdefault(current, False)
        elif not line.strip() or _ANY_BULLET_RE.match(line):
            current = None
        if current is not None and _OPTIONAL_RE.search(line):
            found[current] = True
    return found


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


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    """Exception class names an ``except`` clause names; empty for a bare ``except``."""
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [
        kind.id if isinstance(kind, ast.Name) else kind.attr
        for kind in kinds
        if isinstance(kind, (ast.Name, ast.Attribute))
    ]


def _caught_names(tree: ast.AST) -> set[str]:
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        for name in _handler_names(node)
    }


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


def _lint_capability(
    skill_md: str,
    code_override: str | None = None,
    registry_keys: Collection[str] = STATIC_OBO_REGISTRY_KEYS,
    script: str | None = None,
) -> list[LintIssue]:
    issues: list[LintIssue] = []
    try:
        bindings = parse_input_bindings(skill_md)
    except ValueError as exc:
        return [LintIssue(rule="A13", severity="error", message=str(exc))]
    script_form = script is not None
    if script_form:
        code = script
    else:
        code = _python_code(skill_md) if code_override is None else code_override
    if not code.strip():
        if bindings is not None and not script_form:
            return [LintIssue(rule="A14", severity="error", message="An explicit input-bindings contract requires a Python sample that binds and validates its inputs.")]
        return issues

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        where = (
            "The bundled script is not valid Python"
            if script_form
            else "The sample code block is not valid Python"
        )
        return [
            LintIssue(
                rule="A1",
                severity="error",
                message=(
                    f"{where}: "
                    f"{exc.msg} (line {exc.lineno}). The host executes this block verbatim."
                ),
                detail=f"line {exc.lineno}: {exc.msg}",
            )
        ]

    issues.extend(_check_variable_closure(skill_md, tree, script=script_form))
    if bindings is not None and not script_form:
        issues.extend(_check_input_bindings(skill_md, tree, code, template=code_override is None))
    issues.extend(_check_explicit_raises(tree, script=script_form))
    if script_form:
        issues.extend(_check_needs_info_exit_row(skill_md, code))
    else:
        issues.extend(_check_needs_info_documented(skill_md, code, tree))
        issues.extend(_check_main_signature(tree))
        issues.extend(_check_runtime_input_channel(skill_md, tree))
        issues.extend(_check_runtime_needs_info_codes(skill_md, tree, code))
    issues.extend(_check_caller_field_contract(skill_md, tree))
    issues.extend(_check_uncaught_sql_throw(tree))
    issues.extend(_check_unchecked_call_result(tree))
    issues.extend(_check_deployment_config(skill_md, tree, code, script=script_form))
    issues.extend(_check_identity_contract(skill_md, tree))
    issues.extend(_check_managed_identity(skill_md, tree, code, script=script_form))
    issues.extend(_check_mi_scopes(skill_md, tree, code, script=script_form))
    issues.extend(_check_mi_acquisition(tree, code))
    issues.extend(_check_reserved_credentials_keys(skill_md, tree, registry_keys, script=script_form))
    if script_form:
        issues.extend(_lint_script(skill_md, tree, code))
    return issues


def _check_variable_closure(skill_md: str, tree: ast.AST, *, script: bool = False) -> list[LintIssue]:
    """A2 -- declared variables and the keys the code reads must be the same set."""
    if script:
        declared = list(_prerequisites(skill_md))
        declared_in = f"`## {PREREQUISITES_SECTION}`"
        declared_where = f"no bullet of `## {PREREQUISITES_SECTION}`"
        reader = "the script"
    else:
        bindings = parse_input_bindings(skill_md)
        runtime_names = (
            [binding.credentials_key or binding.name for binding in bindings if binding.source == "credentials"]
            if bindings is not None else _declared_names(_section_body(skill_md, INPUTS_SECTION))
        )
        declared = _unique(
            _declared_names(_section_body(skill_md, ENV_SECTION))
            + _declared_names(_section_body(skill_md, OBO_SECTION))
            + runtime_names
            + _declared_names(_section_body(skill_md, IDENTITY_SECTION))
        )
        declared_where = "none of Required Inputs / Environment Variables / OBO Token Scopes"
        declared_in = "the variable sections"
        reader = "the sample code"
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
                        f"`{name}` is declared in {declared_in} but {reader} "
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
                        f"{reader[0].upper()}{reader[1:]} reads `os.environ` key `{key}`, which is "
                        f"declared in {declared_where}. "
                        "Declare it -- deleting the read is not the fix, it removes the channel "
                        "the value arrives through."
                    ),
                    detail=key,
                )
            )
    return issues


def _check_explicit_raises(tree: ast.AST, *, script: bool = False) -> list[LintIssue]:
    """A3 -- a non-``SystemExit`` raise exits non-zero, i.e. reads as a deployment error.

    A script owns its whole control flow, so a raise its own ``except`` catches
    never reaches the interpreter.
    """
    names = _raised_names(tree)
    if script:
        caught = _caught_names(tree)
        names = [name for name in names if name not in caught]
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


def needs_info_exit_row(skill_md: str) -> bool:
    """Script form: the exit-code table in ``## Reading the Result`` covers ``needs_info``.

    Either a row of its own (first cell names ``needs_info``) or the exit-0 row
    naming it among its meanings. Only the row is required, not a list of
    ``missing=`` codes: a script reports whichever argument it could not use, and
    the host reads the field from the JSON that follows.
    """
    for line in _h2_body(skill_md, RESULT_SECTION).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.replace("`", "").strip().lower() for cell in line.strip().strip("|").split("|")]
        if "needs_info" in cells[0]:
            return True
        if re.match(r"0\b", cells[0]) and any("needs_info" in cell for cell in cells[1:]):
            return True
    return False


def _check_needs_info_exit_row(skill_md: str, code: str) -> list[LintIssue]:
    """A4 (script form) -- a script that prints ``[NEEDS_INFO]`` documents that exit."""
    if "[NEEDS_INFO]" not in code or needs_info_exit_row(skill_md):
        return []
    return [
        LintIssue(
            rule="A4",
            # Topology C2 blocks save on the same check.
            severity="error",
            message=(
                "The script can print `[NEEDS_INFO]`, but the exit-code table in "
                f"`## {RESULT_SECTION}` has no `status` = `needs_info` row. The caller has no way "
                "to tell a missing argument from a finished run. Add a row whose first cell is "
                "`` 0, `status` = `needs_info` ``."
            ),
            detail=RESULT_SECTION,
        )
    ]


def _runtime_env_reads(skill_md: str, tree: ast.AST, *, script: bool = False) -> list[str]:
    """Env keys that carry CALLER data: not deployment config, not the identity."""
    reserved = {VERIFIED_UPN_VAR}
    reserved.update(name.upper() for name in _deployment_names(skill_md, script=script))
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
    if parse_input_bindings(skill_md) is not None or _runtime_env_reads(skill_md, tree):
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


def _check_input_bindings(
    skill_md: str, tree: ast.AST, code: str, *, template: bool,
) -> list[LintIssue]:
    bindings = parse_input_bindings(skill_md) or []
    request_fields = {binding.name for binding in bindings if binding.source == "request"}
    mappings = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "request_inputs" for target in node.targets)
        and isinstance(node.value, ast.Dict)
    ]
    supplied = {
        key.value: value for mapping in mappings for key, value in zip(mapping.keys, mapping.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    reads = {
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "request_inputs"
        and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)
    }
    codes = {item.upper() for item in _needs_info_codes(tree, code)}
    issues: list[LintIssue] = []
    for binding in bindings:
        if binding.source != "request":
            if binding.payload_field and binding.payload_field not in _payload_field_reads(tree):
                issues.append(LintIssue(rule="A14", severity="error", message=f"The code never reads the declared payload field `{binding.payload_field}`.", detail=binding.name))
            continue
        value = supplied.get(binding.name)
        if binding.name not in supplied or binding.name not in reads:
            issues.append(LintIssue(rule="A14", severity="error", message=f"Bind `{binding.name}` in `request_inputs` and read it with `.get()` before validation. Request is not an automatic Python variable.", detail=binding.name))
        elif template and not (isinstance(value, ast.Constant) and value.value is None):
            issues.append(LintIssue(rule="A14", severity="error", message=f"The sample template must leave `{binding.name}` as None, not an executable example value. The Coding Agent binds it from the current request.", detail=binding.name))
        if binding.required and binding.name.upper() not in codes:
            issues.append(LintIssue(rule="A14", severity="error", message=f"Validate `{binding.name}` before external calls and emit `[NEEDS_INFO] missing={binding.name.upper()}` when required but absent. A request source does not remove missing-data handling.", detail=binding.name))
    for name in supplied.keys() - request_fields:
        issues.append(LintIssue(rule="A14", severity="error", message=f"`request_inputs` contains `{name}` without a request-source binding. Do not duplicate credential inputs or invent a fallback.", detail=name))
    return issues


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


def _deployment_names(skill_md: str, *, script: bool = False) -> list[str]:
    if script:
        return list(_prerequisites(skill_md))
    return _unique(
        _declared_names(_section_body(skill_md, ENV_SECTION))
        + _declared_names(_section_body(skill_md, OBO_SECTION))
    )


def _has_literal_default(node: ast.AST) -> bool:
    """``os.environ.get(NAME, <literal>)`` / ``os.getenv(NAME, <literal>)``."""
    if not isinstance(node, ast.Call):
        return False
    default = node.args[1] if len(node.args) > 1 else next(
        (kw.value for kw in node.keywords if kw.arg == "default"), None
    )
    return isinstance(default, ast.Constant) and default.value is not None


def _check_deployment_config(
    skill_md: str, tree: ast.AST, code: str, *, script: bool = False
) -> list[LintIssue]:
    """D1-D3 -- deployment config must fail loudly, never look like a missing caller input.

    ``[NEEDS_INFO]`` + exit 0 tells the host "ask the caller and retry". A broken
    deployment or a broken OBO chain is not something a caller can supply, so
    every shape that routes it there makes the host retry forever against an
    environment nobody was told is misconfigured.

    Script form only: a variable ``## Prerequisites`` marks optional may be read
    with a literal default. An OBO token never may -- its absence is always a
    broken deployment.
    """
    names = _deployment_names(skill_md, script=script)
    if not names:
        return []
    strict, lenient = _env_reads_for(tree, names)
    issues: list[LintIssue] = []

    if script:
        optional = {name.upper() for name, is_optional in _prerequisites(skill_md).items() if is_optional}
        lenient = [
            node
            for node in lenient
            if not (
                _has_literal_default(node)
                and node.args[0].value.upper() in optional
                and not node.args[0].value.upper().endswith("_ACCESS_TOKEN")
            )
        ]

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

    if issues and not script and DEPLOYMENT_SECTION not in skill_md:
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
# EAA platform rules
# ---------------------------------------------------------------------------


def _check_platform_secrets(
    skill_md: str, code_override: str | None, script: str | None = None
) -> list[LintIssue]:
    """D4 -- EAA strips these secrets from the skill's environment."""
    if script is not None:
        text = f"{skill_md}\n{script}"
        sections: tuple[str, ...] = (PREREQUISITES_SECTION, INPUTS_SECTION)
    elif code_override is None:
        text = skill_md
        sections = (ENV_SECTION, OBO_SECTION, INPUTS_SECTION)
    else:
        text = code_override
        sections = ()
    read = [key for key in _unique(_ENV_READ_TEXT_RE.findall(text or "")) if key in PLATFORM_SECRET_DENYLIST]
    declared = [
        name
        for section in sections
        for name in _declared_names(_section_body(skill_md, section))
        if name in PLATFORM_SECRET_DENYLIST
    ]
    return [
        LintIssue(
            rule="D4",
            severity="error",
            message=(
                f"`{name}` is a platform secret that EAA removes from the skill's execution "
                "environment. The skill must neither read it nor declare it as a variable -- "
                "not even in prose, because the runtime model follows the prose and EAA's lint "
                "scans the full text."
            ),
            detail=name,
        )
        for name in _unique(read + declared)
    ]


def _check_credential_placeholder(skill_md: str, code_override: str | None) -> list[LintIssue]:
    """D5 -- a credential left for someone else to fill in is not a credential."""
    code = code_override if code_override is not None else "\n".join(_ANY_FENCE_RE.findall(skill_md or ""))
    match = _CREDENTIAL_PLACEHOLDER_RE.search(code or "")
    if not match:
        return []
    return [
        LintIssue(
            rule="D5",
            severity="error",
            message=(
                f"The code passes a placeholder credential (`{match.group(0).strip()}`). EAA no "
                "longer supplies an identity by default, so the skill fails or comes back as "
                "`[NEEDS_INFO]`. Name the credential explicitly: "
                "`credential=DefaultAzureCredential()` for the platform Managed Identity."
            ),
            detail=match.group(0).strip(),
        )
    ]


def _check_execution_environment(skill_md: str, code_override: str | None) -> list[LintIssue]:
    """E1-E4 -- code shapes the EAA uid sandbox breaks. Prose is never scanned.

    E1 stays a warning (EAA only reports absolute paths); E2-E4 block saving.
    """
    code = code_override if code_override is not None else "\n".join(_ANY_FENCE_RE.findall(skill_md or ""))
    return [
        LintIssue(
            rule=rule,
            message=message.format(match=match),
            detail=match,
            severity="warning" if rule == "E1" else "error",
        )
        for rule, pattern, message in _EXECUTION_RULES
        for match in _unique(m.group(0) if not m.groups() else m.group(1) for m in pattern.finditer(code or ""))
    ]


def _passes_default_credential(tree: ast.AST) -> bool:
    def is_dac_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _DEFAULT_CREDENTIAL

    aliases = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and is_dac_call(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return any(
        kw.arg == "credential" and (is_dac_call(kw.value) or (isinstance(kw.value, ast.Name) and kw.value.id in aliases))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
    )


def _check_managed_identity(
    skill_md: str, tree: ast.AST, code: str, *, script: bool = False
) -> list[LintIssue]:
    """D6 -- a Managed Identity skill must say so in code AND in `## OBO Token Scopes`.

    Script form states it in `## Prerequisites` instead.
    """
    auth_section = PREREQUISITES_SECTION if script else OBO_SECTION
    obo_body = _section_body(skill_md, auth_section)
    if not (_MI_CREDENTIAL_RE.search(code) or _DEFAULT_CREDENTIAL.lower() in obo_body.lower()):
        return []
    issues: list[LintIssue] = []
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "azure.identity"
        and any(alias.name == _DEFAULT_CREDENTIAL and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )
    if not imported:
        issues.append(LintIssue(
            rule="D6",
            severity="error",
            message=(
                "A Managed Identity skill must import the credential explicitly: "
                "`from azure.identity import DefaultAzureCredential`."
            ),
            detail="import",
        ))
    if "ManagedIdentityCredential" in code:
        issues.append(LintIssue(
            rule="D6",
            severity="error",
            message="Use `DefaultAzureCredential()` for the platform Managed Identity, not `ManagedIdentityCredential`.",
            detail="ManagedIdentityCredential",
        ))
    if not _passes_default_credential(tree):
        issues.append(LintIssue(
            rule="D6",
            severity="error",
            message=(
                "The code never passes `credential=DefaultAzureCredential()` to a client. EAA no "
                "longer defaults to the Managed Identity; a client built without it fails or "
                "comes back as `[NEEDS_INFO]`."
            ),
            detail="credential",
        ))
    normalized = _normalize(obo_body)
    authenticates = "authenticate" in normalized or "驗證" in normalized
    if not (authenticates and all(phrase in normalized for phrase in ("defaultazurecredential()", "managed identity"))):
        issues.append(LintIssue(
            rule="D6",
            severity="error",
            message=(
                f"`## {auth_section}` must state how the skill authenticates, e.g. "
                "\"Authenticate with DefaultAzureCredential() (platform Managed Identity).\" "
                "The runtime writes its own script from the prose, so a sample that is right "
                "is not enough."
            ),
            detail=auth_section,
        ))
    return issues


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _string_constants(tree: ast.AST) -> list[str]:
    return [
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _code_token_scopes(tree: ast.AST) -> list[str]:
    """Resources named by a literal token scope: any ``.../.default`` string or a ``get_token`` argument."""
    literals = [value for value in _string_constants(tree) if value.strip().endswith("/.default")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "get_token":
            literals.extend(
                arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return _unique(literals)


def _check_mi_scopes(
    skill_md: str, tree: ast.AST, code: str, *, script: bool = False
) -> list[LintIssue]:
    """D7 -- EAA's MI gate hands out a token only for resources in `metadata.mi_scopes`."""
    auth_section = PREREQUISITES_SECTION if script else OBO_SECTION
    uses_mi = bool(_MI_CALL_RE.search(code))
    scopes = declared_mi_scopes(skill_md)
    if not uses_mi and not scopes:
        return []
    if not scopes:
        return [LintIssue(
            rule="D7",
            severity="error",
            message=(
                "The code uses Managed Identity but declares no metadata.mi_scopes. EAA's MI gate "
                "refuses the token (the script fails with `EAA MI proxy`). List every Entra resource "
                "the code reaches under `metadata.mi_scopes`, e.g. `- https://storage.azure.com`."
            ),
            detail="mi_scopes",
        )]
    issues: list[LintIssue] = []
    if not uses_mi:
        issues.append(LintIssue(
            rule="D7",
            severity="error",
            message=(
                "`metadata.mi_scopes` is declared but the code never calls `DefaultAzureCredential()`. "
                "A skill that does not use the platform Managed Identity must not declare the field."
            ),
            detail="unused",
        ))
    allowlist = mi_scope_allowlist()
    resources: list[str] = []
    for scope in scopes:
        resource = normalize_mi_resource(scope)
        if not resource:
            issues.append(LintIssue(
                rule="D7",
                severity="error",
                message=(
                    f"metadata.mi_scopes entry `{scope}` is not an Entra resource identifier. Write "
                    "`https://<host>` with no path, e.g. `https://storage.azure.com`."
                ),
                detail=scope,
            ))
            continue
        resources.append(resource)
        if resource in MI_HARD_DENIED_RESOURCES:
            issues.append(LintIssue(
                rule="D7",
                severity="error",
                message=(
                    f"metadata.mi_scopes: {resource} is always denied by the MI proxy. Key Vault secrets "
                    "belong to no single skill; read an ACA secret with `os.environ[\"<NAME>\"]` instead."
                ),
                detail=resource,
            ))
        elif resource not in allowlist:
            issues.append(LintIssue(
                rule="D7",
                severity="info",
                message=(
                    f"metadata.mi_scopes: {resource} is not in MI_SCOPE_ALLOWLIST. "
                    + mi_allowlist_note(resource)
                ),
                detail=resource,
            ))
    for literal in _code_token_scopes(tree):
        resource = normalize_mi_resource(literal)
        if resource and resources and resource not in resources:
            issues.append(LintIssue(
                rule="D7",
                severity="error",
                message=(
                    f"The code requests a token for `{literal}`, but `metadata.mi_scopes` does not "
                    f"declare {resource}. The MI gate refuses any resource that is not declared."
                ),
                detail=resource,
            ))
    normalized = _normalize(_section_body(skill_md, auth_section))
    missing = [r for r in resources if r not in normalized]
    if uses_mi and ("mi_scopes" not in normalized or missing):
        issues.append(LintIssue(
            rule="D7",
            severity="error",
            message=(
                f"`## {auth_section}` must name the declared resources, e.g. \"以 `DefaultAzureCredential()`"
                f"（平台 Managed Identity）驗證，已宣告 `metadata.mi_scopes: [{', '.join(resources)}]`\"."
            ),
            detail=auth_section,
        ))
    return issues


def _check_mi_acquisition(tree: ast.AST, code: str) -> list[LintIssue]:
    """D8 -- the MI token comes only from azure-identity, for the system-assigned identity."""
    issues: list[LintIssue] = []
    constants = _string_constants(tree)
    for key in sorted(MI_ENV_KEYS & set(constants)):
        issues.append(LintIssue(
            rule="D8",
            severity="error",
            message=(
                f"The code references `{key}`. Get the Managed Identity token only through "
                "azure-identity (`DefaultAzureCredential()`); the MI endpoint variables are not the "
                "skill's to read."
            ),
            detail=key,
        ))
    for fragment in _MI_ENDPOINT_FRAGMENTS:
        if any(fragment in value for value in constants):
            issues.append(LintIssue(
                rule="D8",
                severity="error",
                message=(
                    f"The code calls the identity endpoint directly (`{fragment}`). Use "
                    "`DefaultAzureCredential()`; a hand-rolled token request bypasses the MI gate."
                ),
                detail=fragment,
            ))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) in _MI_CREDENTIAL_CLASSES):
            continue
        for kw in node.keywords:
            if kw.arg in MI_IDENTITY_SELECTORS:
                issues.append(LintIssue(
                    rule="D8",
                    severity="error",
                    message=(
                        f"`{_call_name(node)}({kw.arg}=...)` selects a user-assigned identity. The "
                        "platform has only a system-assigned Managed Identity and rejects it (403); "
                        "call `DefaultAzureCredential()` with no identity arguments."
                    ),
                    detail=kw.arg,
                ))
    if _MI_CALL_RE.search(code):
        used = {_call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        used |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for name in sorted(used & _FALLBACK_CREDENTIALS):
            issues.append(LintIssue(
                rule="D8",
                severity="error",
                message=(
                    f"A Managed Identity skill also uses `{name}`. When the MI token is refused "
                    "(`EAA MI proxy`) the script must print the error and exit non-zero -- never "
                    "retry with another credential, a key or another identity."
                ),
                detail=name,
            ))
    return issues


def _check_reserved_credentials_keys(
    skill_md: str, tree: ast.AST, registry_keys: Collection[str], *, script: bool = False
) -> list[LintIssue]:
    """A15 -- EAA drops caller-supplied credentials keys with reserved names."""
    bindings = parse_input_bindings(skill_md)
    if bindings is not None:
        names = [b.credentials_key or b.name for b in bindings if b.source == "credentials"]
    elif script:
        # Script arguments arrive as argv; only an undeclared env read is left for credentials.
        names = _runtime_env_reads(skill_md, tree, script=True)
    else:
        platform = {VERIFIED_UPN_VAR}
        for section in (ENV_SECTION, OBO_SECTION, IDENTITY_SECTION):
            platform.update(_declared_names(_section_body(skill_md, section)))
        names = [
            name
            for name in _declared_names(_section_body(skill_md, INPUTS_SECTION)) + _runtime_env_reads(skill_md, tree)
            if name not in platform
        ]
    issues: list[LintIssue] = []
    for name in _unique(names):
        reason = reserved_credentials_key_reason(name, registry_keys)
        if reason:
            issues.append(LintIssue(
                rule="A15",
                severity="error",
                message=(
                    f"The caller is asked to send `{name}` in `credentials`, but EAA discards that "
                    f"key: {reason}. The value never reaches the script. Rename the input."
                ),
                detail=name,
            ))
    return issues


# ---------------------------------------------------------------------------
# Script-form rules (S2-S12)
# ---------------------------------------------------------------------------
# S1 (path, extra files, name, scenario) is EAA's lint_skill_package's to judge.

_FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z0-9][A-Za-z0-9_-]*)")
_TOKEN_LIKE_FLAG_RE = re.compile(
    r"token|secret|passw(?:or)?d|api[-_]?key|bearer|credential", re.IGNORECASE
)
_EAA_RUNS_RE = re.compile(r"eaa_runs", re.IGNORECASE)
_NEEDS_INFO_PREFIX = "[NEEDS_INFO]"


def is_needs_info_response(text: str) -> bool:
    """A route_only response where the runtime asked for missing input instead of preparing code."""
    return (text or "").lstrip().startswith(_NEEDS_INFO_PREFIX)

_PARSE_ARGS_ATTRS = frozenset(
    {"parse_args", "parse_known_args", "parse_intermixed_args", "parse_known_intermixed_args"}
)
_SYSTEM_EXIT_CATCHERS = frozenset({"SystemExit", "BaseException"})
_CONTENT_ERROR_RES = tuple(
    (pattern, re.compile(pattern, re.IGNORECASE)) for pattern in CONTENT_ERROR_HARD_PATTERNS
)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclass
class _ScriptModel:
    """What every S rule needs to know about the script's control flow."""

    tree: ast.AST
    parents: dict[ast.AST, ast.AST]
    consts: dict[str, int]
    # Functions whose return value is the exit code: ``sys.exit(main(...))``.
    exit_funcs: frozenset[str]
    # Functions (not exit functions) that print the ``[NEEDS_INFO]`` line themselves.
    needs_info_helpers: frozenset[str]
    source: str = ""

    def function_of(self, node: ast.AST) -> ast.AST | None:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = self.parents.get(current)
        return current


def _script_model(tree: ast.AST, source: str = "") -> _ScriptModel:
    parents = _parents(tree)
    exit_funcs = frozenset(
        expr.func.id
        for node in ast.walk(tree)
        for expr in [_exit_expr(node)]
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
    )
    helpers = frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name not in exit_funcs
        and any(_is_needs_info_print(sub) for sub in _own_nodes(node))
    )
    return _ScriptModel(tree, parents, _module_int_constants(tree), exit_funcs, helpers, source)


def _own_nodes(scope: ast.AST) -> Iterable[ast.AST]:
    """Every node of a function body, nested functions and classes excluded."""
    stack = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPE_NODES))


def _is_int(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and type(node.value) is int


def _module_int_constants(tree: ast.AST) -> dict[str, int]:
    """Module-level ``NAME = <int>``, including ``A, B = 0, 3``."""
    consts: dict[str, int] = {}
    for node in getattr(tree, "body", []):
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append((node.target, node.value))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, (ast.Tuple, ast.List))
                    and isinstance(node.value, (ast.Tuple, ast.List))
                    and len(target.elts) == len(node.value.elts)
                ):
                    pairs.extend(zip(target.elts, node.value.elts))
                else:
                    pairs.append((target, node.value))
        for target, value in pairs:
            if isinstance(target, ast.Name) and _is_int(value):
                consts[target.id] = value.value
    return consts


def _is_module_attr(node: ast.AST, module: str, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == module
    )


_NO_EXIT = object()


def _exit_expr(node: ast.AST):
    """The exit-code expression of ``sys.exit(x)`` / ``raise SystemExit(x)`` (None = no argument).

    Returns ``_NO_EXIT`` when ``node`` is not an exit at all.
    """
    if isinstance(node, ast.Raise) and node.exc is not None:
        exc = node.exc
        if isinstance(exc, ast.Name) and exc.id == "SystemExit":
            return None
        if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and exc.func.id == "SystemExit":
            return exc.args[0] if exc.args else None
        return _NO_EXIT
    if isinstance(node, ast.Call):
        func = node.func
        if (
            _is_module_attr(func, "sys", "exit")
            or _is_module_attr(func, "os", "_exit")
            or (isinstance(func, ast.Name) and func.id in {"exit", "quit"})
        ):
            return node.args[0] if node.args else None
    return _NO_EXIT


def _exit_code(expr: ast.AST | None, consts: Mapping[str, int]) -> int | None:
    """The literal exit status, or None when it cannot be resolved statically."""
    if expr is None:
        return 0
    if isinstance(expr, ast.Constant):
        if expr.value is None:
            return 0
        if type(expr.value) is int:
            return expr.value
        return 1 if isinstance(expr.value, str) else None
    if isinstance(expr, ast.Name):
        return consts.get(expr.id)
    return None


def _stmt_exit(stmt: ast.stmt, s: _ScriptModel, func: ast.AST | None) -> tuple[str, int | None] | None:
    """How ``stmt`` leaves the current path: ``("exit", code)``, ``("return"|"raise"|"jump", None)``."""
    if isinstance(stmt, ast.Return):
        if getattr(func, "name", None) in s.exit_funcs:
            return "exit", _exit_code(stmt.value, s.consts)
        return "return", None
    if isinstance(stmt, (ast.Continue, ast.Break)):
        return "jump", None
    target = stmt if isinstance(stmt, ast.Raise) else stmt.value if isinstance(stmt, ast.Expr) else None
    if target is None:
        return None
    expr = _exit_expr(target)
    if expr is _NO_EXIT:
        return ("raise", None) if isinstance(stmt, ast.Raise) else None
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in s.exit_funcs:
        return "exit", None
    return "exit", _exit_code(expr, s.consts)


def _block_terminates(block: Sequence[ast.stmt], s: _ScriptModel, func: ast.AST | None) -> bool:
    return bool(block) and _stmt_exit(block[-1], s, func) is not None


def _is_sys_stream(node: ast.AST | None, stream: str) -> bool:
    # A bare name covers `from sys import stdout` / `stderr`.
    return _is_module_attr(node, "sys", stream) or (isinstance(node, ast.Name) and node.id == stream)


def _is_stdout_print(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        target = next((kw.value for kw in node.keywords if kw.arg == "file"), None)
        return target is None or _is_sys_stream(target, "stdout")
    return isinstance(func, ast.Attribute) and func.attr == "write" and _is_sys_stream(func.value, "stdout")


def _is_stderr_write(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        target = next((kw.value for kw in node.keywords if kw.arg == "file"), None)
        return _is_sys_stream(target, "stderr") if target is not None else False
    return isinstance(func, ast.Attribute) and func.attr == "write" and _is_sys_stream(func.value, "stderr")


def _source_excerpt(s: _ScriptModel, node: ast.AST) -> str:
    """`` (`first source line`)`` for a message, or ``""`` when the source is unknown."""
    segment = ast.get_source_segment(s.source, node) if s.source else None
    if not segment:
        return ""
    line = " ".join(segment.split())
    if len(line) > 100:
        line = line[:97] + "..."
    return f" (`{line}`)"


def _literal_prefix(node: ast.AST) -> str | None:
    """The literal text a string expression starts with."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        prefix = []
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                break
            prefix.append(part.value)
        return "".join(prefix)
    return None


def _is_needs_info_print(node: ast.AST) -> bool:
    if not (_is_stdout_print(node) and node.args):
        return False
    prefix = _literal_prefix(node.args[0])
    return prefix is not None and prefix.lstrip().startswith(_NEEDS_INFO_PREFIX)


def _emits_needs_info(stmt: ast.AST, s: _ScriptModel) -> str:
    """``"print"`` / ``"helper"`` when ``stmt`` prints the marker directly / via a helper, else ``""``."""
    for node in ast.walk(stmt):
        if _is_needs_info_print(node):
            return "print"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in s.needs_info_helpers:
                return "helper"
    return ""


def _is_json_dumps(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _is_module_attr(node.func, "json", "dumps") or (
        isinstance(node.func, ast.Name) and node.func.id == "dumps"
    )


def _assigned_values(s: _ScriptModel, name: str, near: ast.AST) -> list[ast.AST]:
    """Values bound to ``name`` in the scope around ``near``."""
    scope = s.function_of(near) or s.tree
    values: list[ast.AST] = []
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id == name:
                values.append(node.value)
    return values


def _locate(child: ast.AST, parent: ast.AST) -> tuple[str, list] | None:
    for field, value in ast.iter_fields(parent):
        if isinstance(value, list) and any(item is child for item in value):
            return field, value
    return None


def _index_of(child: ast.AST, block: list) -> int:
    return next(i for i, item in enumerate(block) if item is child)


def _statement_of(node: ast.AST, s: _ScriptModel) -> ast.AST:
    while not isinstance(node, ast.stmt) and s.parents.get(node) is not None:
        node = s.parents[node]
    return node


def _stmt_prints(stmt: ast.AST) -> list[ast.Call]:
    stack = [stmt]
    found: list[ast.Call] = []
    while stack:
        node = stack.pop()
        if _is_stdout_print(node):
            found.append(node)
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPE_NODES))
    return found


def _prior_prints(stmt: ast.stmt, s: _ScriptModel, func: ast.AST | None) -> list[ast.Call]:
    """Stdout writes ``stmt`` can make on a path that continues past it.

    A branch that ends in ``return`` / exit / ``raise`` never reaches the next
    statement, so what it prints cannot precede anything after it.
    """
    def block(stmts: Sequence[ast.stmt]) -> list[ast.Call]:
        if _block_terminates(stmts, s, func):
            return []
        return [call for st in stmts for call in _prior_prints(st, s, func)]

    if isinstance(stmt, _SCOPE_NODES):
        return []
    if isinstance(stmt, ast.If):
        return block(stmt.body) + block(stmt.orelse)
    if isinstance(stmt, ast.Try):
        found = [call for st in stmt.body for call in _prior_prints(st, s, func)]
        for handler in stmt.handlers:
            found += block(handler.body)
        return found + block(stmt.orelse) + block(stmt.finalbody)
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
        found = [call for st in stmt.body for call in _prior_prints(st, s, func)]
        return found + [call for st in getattr(stmt, "orelse", []) for call in _prior_prints(st, s, func)]
    return _stmt_prints(stmt)


def _print_count(stmts: Sequence[ast.stmt], s: _ScriptModel, func: ast.AST | None) -> int:
    """Most stdout lines one pass through ``stmts`` can write (a loop counts as many)."""
    total = 0
    for stmt in stmts:
        if isinstance(stmt, _SCOPE_NODES):
            continue
        if isinstance(stmt, ast.If):
            total += max(_print_count(stmt.body, s, func), _print_count(stmt.orelse, s, func))
        elif isinstance(stmt, ast.Try):
            total += sum(_print_count(b, s, func) for b in (stmt.body, stmt.orelse, stmt.finalbody))
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            total += 2 * _print_count(stmt.body, s, func) + _print_count(stmt.orelse, s, func)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            total += _print_count(stmt.body, s, func)
        else:
            total += len(_stmt_prints(stmt))
        if _stmt_exit(stmt, s, func) is not None:
            break
    return total


def _needs_info_path(
    marker: ast.Call, s: _ScriptModel
) -> tuple[list[ast.Call], int, tuple[str, int | None] | None]:
    """(stdout writes that can precede the marker, lines after it, how the path ends)."""
    func = s.function_of(marker)
    child = _statement_of(marker, s)
    parent = s.parents.get(child)
    prior: list[ast.Call] = []
    after = 0
    ending: tuple[str, int | None] | None = None
    ended = False
    while parent is not None:
        located = _locate(child, parent)
        if located and isinstance(child, ast.stmt):
            field, block = located
            index = _index_of(child, block)
            for stmt in block[:index]:
                prior += _prior_prints(stmt, s, func)
            if not ended:
                for stmt in block[index + 1 :]:
                    after += _print_count([stmt], s, func)
                    ending = _stmt_exit(stmt, s, func)
                    if ending is not None:
                        ended = True
                        break
            if isinstance(parent, ast.Try) and field != "body":
                for stmt in parent.body:
                    prior += _prior_prints(stmt, s, func)
        elif located and isinstance(parent, ast.Try) and located[0] == "handlers":
            for stmt in parent.body:
                prior += _prior_prints(stmt, s, func)
        if parent is func or isinstance(parent, ast.Module):
            break
        child, parent = parent, s.parents.get(parent)
    return prior, after, ending


def _check_script_fence(skill_md: str) -> list[LintIssue]:
    """S2 -- a script-form SKILL.md carries no Python block for the runtime to rewrite."""
    if not _PY_FENCE_RE.search(_without_gatekeeper_addendum(skill_md)):
        return []
    return [
        LintIssue(
            rule="S2",
            severity="error",
            message=(
                "A script-form SKILL.md contains a Python code block. The host runs the bundled "
                "script with `run_skill_script`; a sample block invites it to write its own code "
                "instead. Describe the arguments in `## Required Inputs` and remove the block."
            ),
            detail="python fence",
        )
    ]


def _argument_flags(tree: ast.AST) -> list[str]:
    return _unique(
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--")
    )


def _documented_flags(skill_md: str) -> list[str]:
    """``--flag`` names in the first column of the ``## Required Inputs`` table."""
    flags: list[str] = []
    for line in _h2_body(skill_md, INPUTS_SECTION).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        first = line.strip().strip("|").split("|")[0]
        for span in _BACKTICK_TOKEN_RE.findall(first):
            flags += _FLAG_RE.findall(span)
    return _unique(flags)


def _flagless_input_rows(skill_md: str) -> list[str]:
    """First cells of ``## Required Inputs`` table rows that name no ``--flag``."""
    rows: list[str] = []
    header = True
    for line in _h2_body(skill_md, INPUTS_SECTION).splitlines():
        if not line.lstrip().startswith("|"):
            header = True
            continue
        cells = line.strip().strip("|").split("|")
        if header:
            header = False
            continue
        if all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells):
            continue
        first = cells[0]
        if not any(_FLAG_RE.findall(span) for span in _BACKTICK_TOKEN_RE.findall(first)):
            rows.append(first.strip())
    return rows


def _check_script_args(skill_md: str, s: _ScriptModel) -> list[LintIssue]:
    """S3 -- the argument table, every flag the prose names, and ``add_argument`` agree."""
    accepted = _argument_flags(s.tree)
    documented = _documented_flags(skill_md)
    issues: list[LintIssue] = []
    for cell in _flagless_input_rows(skill_md):
        issues.append(LintIssue(
            rule="S3",
            severity="error",
            message=(
                f"The `## {INPUTS_SECTION}` row `{cell}` names no backticked `--flag` in its first "
                "column. A bundled script receives business inputs only as arguments, so the host "
                "has no way to pass this one."
            ),
            detail=cell,
        ))
    for flag in documented:
        if flag not in accepted:
            issues.append(LintIssue(
                rule="S3",
                severity="error",
                message=(
                    f"`## {INPUTS_SECTION}` lists `{flag}`, but the script never declares it with "
                    "`add_argument`. Every call that passes it fails."
                ),
                detail=flag,
            ))
    for flag in accepted:
        if flag not in documented:
            issues.append(LintIssue(
                rule="S3",
                message=(
                    f"The script accepts `{flag}`, but the first column of the "
                    f"`## {INPUTS_SECTION}` table does not list it. The host cannot pass an "
                    "argument it was never told exists."
                ),
                detail=flag,
            ))
    mentioned = _unique(
        flag for span in _BACKTICK_TOKEN_RE.findall(skill_md or "") for flag in _FLAG_RE.findall(span)
    )
    for flag in mentioned:
        if flag not in accepted and flag not in documented:
            issues.append(LintIssue(
                rule="S3",
                severity="error",
                message=(
                    f"SKILL.md names the argument `{flag}`, which the script does not declare. "
                    "The host follows the prose, so the call fails."
                ),
                detail=flag,
            ))
    return issues


def _prints_after_marker(s: _ScriptModel) -> set[int]:
    """ids of the stdout writes that can follow a ``[NEEDS_INFO]`` print on its own path."""
    found: set[int] = set()
    for marker in ast.walk(s.tree):
        if not _is_needs_info_print(marker):
            continue
        func = s.function_of(marker)
        child = _statement_of(marker, s)
        parent = s.parents.get(child)
        ended = False
        while parent is not None and not ended:
            located = _locate(child, parent)
            if located and isinstance(child, ast.stmt):
                block = located[1]
                for stmt in block[_index_of(child, block) + 1 :]:
                    found.update(id(call) for call in _stmt_prints(stmt))
                    if _stmt_exit(stmt, s, func) is not None:
                        ended = True
                        break
            if parent is func or isinstance(parent, ast.Module):
                break
            child, parent = parent, s.parents.get(parent)
    return found


def _check_stdout_shape(s: _ScriptModel) -> list[LintIssue]:
    """S4 -- stdout carries one ``json.dumps`` document, optionally after ``[NEEDS_INFO]``."""
    issues: list[LintIssue] = []
    after_marker = _prints_after_marker(s)
    prints = sorted(
        (node for node in ast.walk(s.tree) if _is_stdout_print(node)), key=lambda node: node.lineno
    )
    for node in prints:
        if _is_needs_info_print(node):
            continue
        if len(node.args) == 1 and (
            _is_json_dumps(node.args[0])
            or (
                isinstance(node.args[0], ast.Name)
                and any(_is_json_dumps(v) for v in _assigned_values(s, node.args[0].id, node))
            )
        ):
            continue
        where = f"Line {node.lineno}{_source_excerpt(s, node)}"
        if id(node) in after_marker:
            message = (
                f"{where} prints plain text after the `[NEEDS_INFO]` line. EAA strips the marker "
                "line and parses the rest of stdout as one JSON document, so text there leaves the "
                "host an unparsed string instead of the result. Put the explanation into the JSON "
                "object printed after the marker (for example a `reason` field), or send it to "
                "`sys.stderr`."
            )
        else:
            message = (
                f"{where} writes to stdout something other than a `json.dumps(...)` "
                "document or the `[NEEDS_INFO]` line. stdout is the machine-readable result: the "
                "host parses it as one JSON document and EAA scans it for error words. Extra text "
                "stops stdout parsing as JSON, and words in it such as `failed` or `Error:` can make "
                "a successful run count as a failure. Send diagnostics to `sys.stderr`, which EAA "
                "keeps for debugging but does not scan on a successful run."
            )
        issues.append(LintIssue(rule="S4", severity="error", message=message, detail=f"line {node.lineno}"))
    return issues


def _check_injected_globals(s: _ScriptModel) -> list[LintIssue]:
    """S13 -- a bundled script never finds host-injected variables in ``globals()``."""
    return [
        LintIssue(
            rule="S13",
            severity="error",
            message=(
                f"Line {node.lineno}{_source_excerpt(s, _statement_of(node, s))} reads values from `globals()`. Inline "
                "sample code gets its inputs declared into the module by the host; a bundled script "
                "runs as its own process with only its arguments and environment, so this read never "
                "finds a value. Take business inputs as `--flag` arguments; deployment settings may "
                "stay in environment variables."
            ),
            detail=f"line {node.lineno}",
        )
        for node in ast.walk(s.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals"
    ]


def _documented_exit_codes(skill_md: str) -> set[int]:
    codes: set[int] = set()
    for line in _h2_body(skill_md, RESULT_SECTION).splitlines():
        if not line.lstrip().startswith("|"):
            continue
        match = re.match(r"\s*(\d+)\b", line.strip().strip("|").split("|")[0].replace("`", ""))
        if match:
            codes.add(int(match.group(1)))
    return codes


def _check_exit_codes(skill_md: str, s: _ScriptModel) -> list[LintIssue]:
    """S5 -- the script exits only 0, 1 or 3, and the exit-code table documents each."""
    sites: list[tuple[ast.AST, int | None]] = []
    for node in ast.walk(s.tree):
        expr = _exit_expr(node)
        if expr is _NO_EXIT:
            continue
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in s.exit_funcs:
            continue
        sites.append((node, _exit_code(expr, s.consts)))
    for node in ast.walk(s.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in s.exit_funcs:
            sites += [
                (ret, _exit_code(ret.value, s.consts))
                for ret in _own_nodes(node)
                if isinstance(ret, ast.Return)
            ]
    documented = _documented_exit_codes(skill_md)
    issues: list[LintIssue] = []
    reported: set[object] = set()
    for node, code in sorted(sites, key=lambda item: getattr(item[0], "lineno", 0)):
        line = getattr(node, "lineno", 0)
        if code is None:
            issues.append(LintIssue(
                rule="S5",
                message=(
                    f"The exit status at line {line} cannot be resolved to a literal. Use an "
                    "integer or a module-level integer constant so the exit contract can be checked."
                ),
                detail=f"line {line}",
            ))
        elif code not in SCRIPT_EXIT_CODES and code not in reported:
            reported.add(code)
            issues.append(LintIssue(
                rule="S5",
                severity="error",
                message=(
                    f"The script can exit with status {code} (line {line}). A script skill exits "
                    "0 (success, `[NEEDS_INFO]` or a business rejection), 1 (configuration) or 3 "
                    "(downstream failure); 2 is what argparse uses and EAA reports it as a failure."
                ),
                detail=str(code),
            ))
        elif code in SCRIPT_EXIT_CODES and code not in documented and ("doc", code) not in reported:
            reported.add(("doc", code))
            issues.append(LintIssue(
                rule="S5",
                message=(
                    f"The script can exit with status {code}, but the exit-code table in "
                    f"`## {RESULT_SECTION}` has no row for it."
                ),
                detail=str(code),
            ))
    return issues


def _check_needs_info_output(s: _ScriptModel) -> list[LintIssue]:
    """S6 -- ``[NEEDS_INFO]`` is the first stdout line, at most one JSON follows, exit 0."""
    issues: list[LintIssue] = []
    for node in ast.walk(s.tree):
        if not _is_needs_info_print(node):
            continue
        prior, after, ending = _needs_info_path(node, s)
        line = node.lineno
        if prior:
            issues.append(LintIssue(
                rule="S6",
                severity="error",
                message=(
                    f"`[NEEDS_INFO]` at line {line} is not the first stdout line: line "
                    f"{prior[0].lineno} can print before it on the same path. The host reads the "
                    "marker from the first line."
                ),
                detail=f"line {line}",
            ))
        if after > 1:
            issues.append(LintIssue(
                rule="S6",
                severity="error",
                message=(
                    f"After `[NEEDS_INFO]` at line {line} the script can print {after} more "
                    "stdout lines; at most one JSON object may follow the marker."
                ),
                detail=f"line {line}",
            ))
        if ending is not None:
            kind, code = ending
            if (kind == "exit" and code not in (0, None)) or kind == "raise":
                status = f"exits {code}" if kind == "exit" else "raises, which exits 1"
                issues.append(LintIssue(
                    rule="S6",
                    severity="error",
                    message=(
                        f"The path that prints `[NEEDS_INFO]` at line {line} {status}. A "
                        "`[NEEDS_INFO]` run must exit 0, or EAA reports a failure instead of "
                        "asking for the missing input."
                    ),
                    detail=f"line {line}",
                ))
    return issues


def _check_script_tokens(s: _ScriptModel) -> list[LintIssue]:
    """S7 -- an OBO token comes from the environment by index, never from an argument."""
    issues: list[LintIssue] = []
    for node in ast.walk(s.tree):
        if not (isinstance(node, ast.Call) and node.args and _is_env_read_call(node)):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.upper().endswith("_ACCESS_TOKEN"):
                issues.append(LintIssue(
                    rule="S7",
                    severity="error",
                    message=(
                        f"`{first.value}` is read with `.get()` / `os.getenv()`. Read OBO tokens "
                        f"with `os.environ[\"{first.value}\"]` so a missing token exits 1 as a "
                        "configuration error instead of reaching Graph as an empty string."
                    ),
                    detail=first.value,
                ))
    for flag in _argument_flags(s.tree):
        if _TOKEN_LIKE_FLAG_RE.search(flag):
            issues.append(LintIssue(
                rule="S7",
                severity="error",
                message=(
                    f"The script accepts `{flag}`. Arguments are written by the host model; a "
                    "token or secret must come from the platform environment, never from argv."
                ),
                detail=flag,
            ))
    return issues


def _check_eaa_runs_mention(skill_md: str) -> list[LintIssue]:
    """S8 -- the run directory is the platform's business, not the host's."""
    if not _EAA_RUNS_RE.search(_without_gatekeeper_addendum(skill_md)):
        return []
    return [
        LintIssue(
            rule="S8",
            message=(
                "SKILL.md mentions `eaa_runs`. Where the platform runs the script is not part of "
                "the skill's contract; the host only needs the arguments and the result."
            ),
            detail="eaa_runs",
        )
    ]


def _check_output_keys_documented(skill_md: str, s: _ScriptModel) -> list[LintIssue]:
    """S9 -- keys of a dict literal sent through ``json.dumps`` appear in ``## Reading the Result``."""
    documented = _h2_body(skill_md, RESULT_SECTION)
    keys: list[str] = []
    for node in ast.walk(s.tree):
        if not (_is_json_dumps(node) and node.args):
            continue
        arg = node.args[0]
        values = [arg] if isinstance(arg, ast.Dict) else (
            _assigned_values(s, arg.id, node) if isinstance(arg, ast.Name) else []
        )
        for value in values:
            if isinstance(value, ast.Dict):
                keys += [
                    k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
                ]
    return [
        LintIssue(
            rule="S9",
            message=(
                f"The script outputs the key `{key}`, but `## {RESULT_SECTION}` never names it. "
                "The host can only act on fields it was told about."
            ),
            detail=key,
        )
        for key in _unique(keys)
        if f"`{key}`" not in documented
    ]


def _system_exit_handler(node: ast.AST, s: _ScriptModel) -> ast.ExceptHandler | None:
    child, parent = node, s.parents.get(node)
    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(parent, ast.Try) and any(stmt is child for stmt in parent.body):
            for handler in parent.handlers:
                if handler.type is None or set(_handler_names(handler)) & _SYSTEM_EXIT_CATCHERS:
                    return handler
        child, parent = parent, s.parents.get(parent)
    return None


def _handler_asks(handler: ast.ExceptHandler, s: _ScriptModel) -> bool:
    """The handler's path prints ``[NEEDS_INFO]`` and ends in exit 0.

    A handler that does not end its own path falls through to the statements
    after its ``try``, which is where the fixture's ``except NeedsInfo`` prints.
    """
    func = s.function_of(handler)
    stmts = list(handler.body)
    if not _block_terminates(stmts, s, func):
        owner = s.parents.get(handler)
        holder = s.parents.get(owner) if owner is not None else None
        located = _locate(owner, holder) if holder is not None else None
        if located:
            block = located[1]
            stmts += block[_index_of(owner, block) + 1 :]
    emitted = False
    for stmt in stmts:
        how = _emits_needs_info(stmt, s)
        if how == "helper":
            return True
        emitted = emitted or how == "print"
        ending = _stmt_exit(stmt, s, func)
        if ending is not None:
            kind, code = ending
            return emitted and not (kind == "raise" or (kind == "exit" and code not in (0, None)))
    return emitted


def _check_parse_args_guard(s: _ScriptModel) -> list[LintIssue]:
    """S10 -- an argparse failure becomes ``[NEEDS_INFO]`` + exit 0, never argparse's exit 2."""
    issues: list[LintIssue] = []
    for node in ast.walk(s.tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PARSE_ARGS_ATTRS
        ):
            continue
        handler = _system_exit_handler(node, s)
        if handler is not None:
            if _handler_asks(handler, s):
                continue
            raised = [
                exc.func.id if isinstance(exc, ast.Call) else exc.id
                for sub in _own_nodes(handler)
                if isinstance(sub, ast.Raise)
                for exc in [sub.exc]
                if isinstance(exc, ast.Name) or (isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name))
            ]
            if any(
                _handler_asks(other, s)
                for other in ast.walk(s.tree)
                if isinstance(other, ast.ExceptHandler)
                and other is not handler
                and set(_handler_names(other)) & (set(raised) - {"SystemExit"})
            ):
                continue
        where = (
            f"the `except SystemExit` at line {handler.lineno} neither prints `[NEEDS_INFO]` and "
            "exits 0 nor raises an exception whose handler does"
            if handler is not None
            else "it is not inside `try` / `except SystemExit`"
        )
        issues.append(LintIssue(
            rule="S10",
            severity="error",
            message=(
                f"`{node.func.attr}` at line {node.lineno}: {where}. argparse exits 2 on a bad "
                "argument, which EAA reports as a failed run; turn it into `[NEEDS_INFO]` + exit 0 "
                "so the host can fix the call."
            ),
            detail=f"line {node.lineno}",
        ))
    return issues


def _check_help_disabled(s: _ScriptModel) -> list[LintIssue]:
    """S10b -- ``--help`` prints usage to stdout, which must carry JSON only."""
    issues: list[LintIssue] = []
    for node in ast.walk(s.tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "ArgumentParser"):
            continue
        if any(
            kw.arg == "add_help" and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in node.keywords
        ):
            continue
        issues.append(LintIssue(
            rule="S10b",
            severity="error",
            message=(
                f"`ArgumentParser` at line {node.lineno} keeps `--help`, which prints usage text to "
                "stdout and exits 0. Pass `add_help=False`."
            ),
            detail=f"line {node.lineno}",
        ))
    return issues


def _render_json(node: ast.AST) -> str:
    """Approximately what ``json.dumps`` writes for a literal; unknown values become null."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return json.dumps(node.value, ensure_ascii=False)
        return "null"
    if isinstance(node, ast.JoinedStr):
        return json.dumps(_render_text(node), ensure_ascii=False)
    if isinstance(node, ast.Dict):
        entries = [
            f"{json.dumps(str(k.value), ensure_ascii=False)}: {_render_json(v)}"
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant)
        ]
        return "{" + ", ".join(entries) + "}"
    if isinstance(node, (ast.List, ast.Tuple)):
        return "[" + ", ".join(_render_json(e) for e in node.elts) + "]"
    return "null"


def _render_text(node: ast.JoinedStr) -> str:
    return "".join(
        part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def _exit_zero_texts(s: _ScriptModel) -> list[tuple[int, str]]:
    """Literal text the script can print on a path that does not end in a non-zero exit."""
    excluded: set[int] = set()
    for node in ast.walk(s.tree):
        func = s.function_of(node)
        blocks: list[Sequence[ast.stmt]] = []
        if isinstance(node, ast.If):
            blocks = [node.body, node.orelse]
        elif isinstance(node, ast.ExceptHandler):
            blocks = [node.body]
        for block in blocks:
            ending = _stmt_exit(block[-1], s, func) if block else None
            if ending and ending[0] == "exit" and ending[1] not in (0, None):
                excluded.update(id(sub) for stmt in block for sub in ast.walk(stmt))
        if _is_stderr_write(node) or (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        ):
            excluded.update(id(sub) for sub in ast.walk(node))

    texts: list[tuple[int, str]] = []
    for node in ast.walk(s.tree):
        if id(node) in excluded:
            continue
        parent = s.parents.get(node)
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Dict) and not isinstance(parent, ast.Dict):
            texts.append((line, _render_json(node)))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    key = json.dumps(target.slice.value, ensure_ascii=False)
                    texts.append((line, "{" + f"{key}: {_render_json(node.value)}" + "}"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            entries = [
                f"{json.dumps(kw.arg)}: {_render_json(kw.value)}" for kw in node.keywords if kw.arg
            ]
            texts.append((line, "{" + ", ".join(entries) + "}"))
        elif isinstance(node, ast.JoinedStr):
            texts.append((line, _render_text(node)))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and not isinstance(parent, (ast.JoinedStr, ast.FormattedValue))
        ):
            texts.append((line, node.value))
    return texts


def _check_exit_zero_content(s: _ScriptModel) -> list[LintIssue]:
    """S11 -- exit-0 output must not trip EAA's HARD content-error patterns."""
    issues: list[LintIssue] = []
    seen: set[tuple[str, int]] = set()
    for line, text in _exit_zero_texts(s):
        for pattern, compiled in _CONTENT_ERROR_RES:
            match = compiled.search(text)
            if not match or (pattern, line) in seen:
                continue
            seen.add((pattern, line))
            issues.append(LintIssue(
                rule="S11",
                severity="error",
                message=(
                    f"Line {line} can put `{match.group(0)}` into the output of an exit-0 run. EAA "
                    f"scans that output with `{pattern}` and turns a hit into a failed run. Rename "
                    "the key or reword the text; real failures exit 3."
                ),
                detail=f"line {line}: {pattern}",
            ))
    return issues


def _check_inline_template(s: _ScriptModel) -> list[LintIssue]:
    """S12 -- a literal ``request_inputs`` dict is the inline template the host rewrites per query."""
    for node in ast.walk(s.tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Dict) and any(
            isinstance(t, ast.Name) and t.id == "request_inputs" for t in targets
        ):
            return [LintIssue(
                rule="S12",
                severity="error",
                message=(
                    f"Line {node.lineno} assigns a literal `request_inputs` dict. That is inline "
                    "sample code: a template the host rewrites with each request's values. A "
                    "bundled script runs unchanged, so these values would apply to every run "
                    "whatever the user asked; script form takes business inputs as `--flag` arguments."
                ),
                detail=f"line {node.lineno}",
            )]
    return []


def _lint_script(skill_md: str, tree: ast.AST, source: str = "") -> list[LintIssue]:
    s = _script_model(tree, source)
    return (
        _check_script_fence(skill_md)
        + _check_script_args(skill_md, s)
        + _check_stdout_shape(s)
        + _check_injected_globals(s)
        + _check_exit_codes(skill_md, s)
        + _check_needs_info_output(s)
        + _check_script_tokens(s)
        + _check_eaa_runs_mention(skill_md)
        + _check_output_keys_documented(skill_md, s)
        + _check_parse_args_guard(s)
        + _check_help_disabled(s)
        + _check_exit_zero_content(s)
        + _check_inline_template(s)
    )


def script_only_errors(script: str) -> list[LintIssue]:
    """Error-level S rules decidable without a SKILL.md (S4, S5 code set, S6, S7, S10, S10b, S11, S12, S13).

    Raises ``SyntaxError`` when the script does not parse.
    """
    s = _script_model(ast.parse(script), script)
    issues = (
        _check_stdout_shape(s)
        + _check_injected_globals(s)
        + _check_exit_codes("", s)
        + _check_needs_info_output(s)
        + _check_script_tokens(s)
        + _check_parse_args_guard(s)
        + _check_help_disabled(s)
        + _check_exit_zero_content(s)
        + _check_inline_template(s)
    )
    return [issue for issue in issues if issue.severity == "error"]


def script_argument_flags(script: str) -> list[str]:
    """The ``--flag`` names the script declares with ``add_argument`` (the S3 accepted set)."""
    return _argument_flags(ast.parse(script))


def script_argument_names(script: str) -> set[str]:
    """Upper-cased ``dest`` of every ``add_argument`` (``--target-tables`` -> ``TARGET_TABLES``)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(script)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        dest = next((kw.value for kw in node.keywords if kw.arg == "dest"), None)
        if isinstance(dest, ast.Constant) and isinstance(dest.value, str):
            names.add(dest.value.upper())
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        long_flag = next((f for f in flags if f.startswith("--")), None)
        chosen = long_flag or (flags[0] if flags else None)
        if chosen:
            names.add(chosen.lstrip("-").replace("-", "_").upper())
    return names


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
    obo_registry_keys: Collection[str] | None = None,
    script: str | None = None,
) -> list[LintIssue]:
    """Return every content issue in the artifact. Only A1 is an error.

    ``code_override`` swaps the python block for another script -- the code the
    runtime PREPARED for a test sample. That script is regenerated from the body
    prose, so it is a different artifact from the sample code and nothing else
    ever looked at it; the declarations it is reconciled against still come from
    ``skill_md``.

    ``script`` marks a script-form skill: the bundled ``scripts/<name>.py`` is the
    code, declarations come from ``## Prerequisites`` / ``## Required Inputs``,
    and the S rules apply. It takes precedence over ``code_override``.
    """
    if not (skill_md or "").strip():
        return []
    if kind is SkillKind.SCENARIO:
        script = None
    platform_code = script if script is not None else code_override
    platform = (
        _check_platform_secrets(skill_md, platform_code, script)
        + _check_credential_placeholder(skill_md, platform_code)
        + _check_execution_environment(skill_md, platform_code)
    )
    if kind is SkillKind.SCENARIO:
        return _lint_scenario(
            skill_md, child_full_md=child_full_md, host_capabilities=host_capabilities
        ) + platform
    registry = frozenset(obo_registry_keys) if obo_registry_keys else STATIC_OBO_REGISTRY_KEYS
    return _lint_capability(skill_md, code_override, registry, script) + platform
