"""Material tiering for the prompt, plus a warning-only fidelity scan of the draft.

The generator had no signal here at all: the model was told how many materials
were attached but never shown them under any instruction to honour them, so a
pasted reference implementation could be silently replaced by an invented one.
This module owns the tier policy, the prompt budget, and the check that the
draft's sample code actually came from the user's code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import Material, MaterialKind
from .sections import h2_sections, normalize_section

# Tier 1: reproduce, do not paraphrase.
VERBATIM_KINDS = frozenset({MaterialKind.CODE})
# Tier 2: the identifiers are authoritative, the surrounding code is not.
NO_INVENTION_KINDS = frozenset(
    {MaterialKind.API_SPEC, MaterialKind.FILE, MaterialKind.EXISTING_SKILL}
)
# Tier 3: prose. Deliberately excluded from the scan -- background text is not a
# source of code detail, and scanning it yields nothing but false positives.
CONTEXT_ONLY_KINDS = frozenset({MaterialKind.TEXT, MaterialKind.URL})

SCANNED_KINDS = VERBATIM_KINDS | NO_INVENTION_KINDS

MATERIAL_PROMPT_MAX_CHARS = 40_000
MATERIALS_PROMPT_TOTAL_MAX_CHARS = 120_000

_PY_FENCE_RE = re.compile(r"```(?:python|py)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
_CLASS_RE = re.compile(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)", re.MULTILINE)
_EXEC_RE = re.compile(r"\bEXEC(?:UTE)?\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_SQL_OBJECT_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_]\w*\.\w+)", re.IGNORECASE
)
_GET_FIELD_RE = re.compile(r"\.get\(\s*[\"'](\w+)[\"']")
_INDEX_FIELD_RE = re.compile(r"\w\[\s*[\"'](\w+)[\"']\s*\]")
# A closed value set the user's own schema states: a CHECK constraint, a MySQL
# ENUM column, or an `enum` key in a JSON/YAML API spec.
_CHECK_ENUM_RE = re.compile(r"CHECK\s*\(\s*\[?(\w+)\]?\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_SQL_ENUM_RE = re.compile(r"\[?(\w+)\]?\s+ENUM\s*\(([^)]*)\)", re.IGNORECASE)
_JSON_ENUM_RE = re.compile(
    r"[\"'](\w+)[\"']\s*:\s*\{[^{}]*?[\"']enum[\"']\s*:\s*\[([^\]]*)\]", re.DOTALL
)
_ENUM_MARKER_RE = re.compile(r"^([ \t]*)[\"']?enum[\"']?\s*:\s*(\[[^\]]*\])?[ \t]*,?[ \t]*$")
_PROPERTY_RE = re.compile(r"^([ \t]*)[\"']?(\w+)[\"']?\s*:")
_BLOCK_ITEM_RE = re.compile(r"^([ \t]*)-\s*(\S.*?)\s*$")
_REQUIRED_INPUTS = "requiredinputs"


@dataclass(frozen=True)
class FidelityIssue:
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


def _truncation_marker(shown: int, total: int) -> str:
    return (
        f"\n<<<TRUNCATED: {shown} of {total} chars shown. The rest is NOT available. "
        "Do NOT invent the missing part -- say so and ask the user to split the material.>>>"
    )


def materials_for_prompt(
    materials: Sequence[Material] | None,
) -> list[tuple[Material, str, bool]]:
    """Return ``(material, visible_text, truncated)`` under the prompt budget.

    The scan reuses this so a passage the model never received is never reported
    as something the model invented.
    """
    prepared: list[tuple[Material, str, bool]] = []
    budget = MATERIALS_PROMPT_TOTAL_MAX_CHARS
    for material in materials or []:
        content = material.content or ""
        allowance = min(MATERIAL_PROMPT_MAX_CHARS, max(budget, 0))
        if len(content) <= allowance:
            prepared.append((material, content, False))
            budget -= len(content)
            continue
        shown = content[:allowance]
        prepared.append((material, shown + _truncation_marker(len(shown), len(content)), True))
        budget -= allowance
    return prepared


def materials_prompt_chars(materials: Sequence[Material] | None) -> int:
    return sum(len(text) for _, text, _ in materials_for_prompt(materials))


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _required_inputs(skill_md: str) -> str:
    return "\n".join(
        body
        for title, body in h2_sections(skill_md or "")
        if _REQUIRED_INPUTS in normalize_section(title)
    )


def _split_values(raw: str) -> list[str]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if part[:1].upper() == "N" and part[1:2] in "\"'":  # T-SQL national literal
            part = part[1:]
        part = part.strip("\"'` \t")
        if part:
            values.append(part)
    return _unique(values)


def _indent_of(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _block_values(lines: Sequence[str], start: int, indent: int) -> list[str]:
    values: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            continue
        item = _BLOCK_ITEM_RE.match(line)
        if not item or _indent_of(line) <= indent:
            break
        values.append(item.group(2).strip("\"'` \t"))
    return _unique(v for v in values if v)


def _owning_property(lines: Sequence[str], marker: int, indent: int) -> str:
    for line in reversed(lines[:marker]):
        match = _PROPERTY_RE.match(line)
        if match and _indent_of(line) < indent and match.group(2).lower() != "properties":
            return match.group(2)
    return ""


def _enum_declarations(text: str) -> list[tuple[str, list[str]]]:
    """``(field, legal values)`` for every closed value set the material declares."""
    found: list[tuple[str, list[str]]] = []
    for pattern in (_CHECK_ENUM_RE, _SQL_ENUM_RE, _JSON_ENUM_RE):
        found += [(m.group(1), _split_values(m.group(2))) for m in pattern.finditer(text)]

    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ENUM_MARKER_RE.match(line)
        if not match:
            continue
        indent = _indent_of(line)
        inline = match.group(2)
        values = _split_values(inline[1:-1]) if inline else _block_values(lines, index + 1, indent)
        field = _owning_property(lines, index, indent)
        if field and values:
            found.append((field, values))
    return found


def scan_material_fidelity(
    skill_md: str, materials: Sequence[Material] | None
) -> list[FidelityIssue]:
    """Flag sample code that drifted from the user's own code. Never blocks."""
    visible = [
        (material, text, truncated)
        for material, text, truncated in materials_for_prompt(materials)
        if material.kind in SCANNED_KINDS
    ]
    if not visible:
        return []

    code = "\n".join(_PY_FENCE_RE.findall(skill_md or ""))
    if not code.strip():
        return []

    corpus = "\n".join(text for _, text, _ in visible)
    corpus_lower = corpus.lower()
    issues: list[FidelityIssue] = []

    for material, _text, truncated in visible:
        if truncated:
            issues.append(
                FidelityIssue(
                    rule="material_truncated",
                    severity="info",
                    message=(
                        f"Material `{material.id}` ({material.kind.value}) exceeded the prompt "
                        "budget and was truncated, so findings below cover only the part the "
                        "model actually received."
                    ),
                    detail=material.id,
                )
            )

    draft_symbols = set(_DEF_RE.findall(code)) | set(_CLASS_RE.findall(code))
    reported: set[str] = set()
    for material, text, _truncated in visible:
        if material.kind not in VERBATIM_KINDS:
            continue
        for name in _unique(_DEF_RE.findall(text) + _CLASS_RE.findall(text)):
            if name in draft_symbols or name in reported:
                continue
            reported.add(name)
            issues.append(
                FidelityIssue(
                    rule="dropped_from_material",
                    message=(
                        f"`{name}` is defined in the code material but does not appear in the "
                        "draft's sample code."
                    ),
                    detail=name,
                )
            )

    for name in _unique(_EXEC_RE.findall(code) + _SQL_OBJECT_RE.findall(code)):
        if name.lower() in corpus_lower:
            continue
        issues.append(
            FidelityIssue(
                rule="invented_sql_object",
                message=(
                    f"The sample code references SQL object `{name}`, which appears in no "
                    "material."
                ),
                detail=name,
            )
        )

    for name in _unique(_GET_FIELD_RE.findall(code) + _INDEX_FIELD_RE.findall(code)):
        if name in corpus:
            continue
        issues.append(
            FidelityIssue(
                rule="invented_payload_field",
                message=(
                    f"The sample code reads field `{name}`, which appears in no material."
                ),
                detail=name,
            )
        )

    issues.extend(_scan_undocumented_enums(skill_md, code, visible))
    return issues


def _scan_undocumented_enums(
    skill_md: str, code: str, visible: Sequence[tuple[Material, str, bool]]
) -> list[FidelityIssue]:
    """Flag a closed value set the material states and the field contract omits.

    The material is the only place the legal values can come from when the sample
    code passes the field straight to a query: the code knows nothing to check
    against, so nothing else in the pipeline can tell that the contract is
    missing the one thing the caller most needs.
    """
    inputs = _required_inputs(skill_md)
    if not inputs.strip():
        return []
    payload_fields = set(_GET_FIELD_RE.findall(code) + _INDEX_FIELD_RE.findall(code))

    issues: list[FidelityIssue] = []
    seen: set[str] = set()
    for _material, text, _truncated in visible:
        for field, values in _enum_declarations(text):
            if field not in payload_fields or field in seen or len(values) < 2:
                continue
            missing = [value for value in values if value not in inputs]
            if not missing:
                continue
            seen.add(field)
            issues.append(
                FidelityIssue(
                    rule="undocumented_enum",
                    message=(
                        f"The material limits `{field}` to "
                        + ", ".join(f"`{value}`" for value in values)
                        + ", but "
                        + ", ".join(f"`{value}`" for value in missing)
                        + " appears nowhere in `## Required Inputs`. A caller that cannot see "
                        "the legal values sends the user's own wording instead."
                    ),
                    detail=f"{field}: {', '.join(missing)}",
                )
            )
    return issues


def fidelity_warning_count(issues: Sequence[FidelityIssue]) -> int:
    return sum(1 for issue in issues if issue.severity == "warning")
