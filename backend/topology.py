"""Structural validation of the two-tier skill topology.

This module deliberately encodes the most CONSERVATIVE shape that every known
frontmatter parser accepts, rather than mirroring any one runtime parser. That
framing matters: conservatism does not expire, so nothing here needs to be kept
in sync with an external codebase.

``backend/`` must never import the ``reference/`` runtime snapshot. The only
place reference code is allowed to appear is the opt-in differential test.

Rules come in three families:

* **T** -- frontmatter and section shape. Split into **A class** semantic rules
  (T4-T7, T9, T10) and **B class** output conservatism derived from parser
  implementations (T1-T3). B class is always safe to satisfy, so it is an error
  when we generate the file ourselves and a warning when we are editing
  something pre-existing.
* **P** -- the scenario layer's pointer contract. A parent no longer copies a
  child's field table; it names the child's sections. These rules check that
  every pointer resolves and that nothing is pointed at without being declared.
* **C** -- the capability layer's section names, which are a published API the
  moment a parent points at them. These are semantic and never degrade.

Messages state only the formal requirement. They must not describe downstream
runtime consequences, which is exactly the kind of claim that turns into a lie
when the runtime changes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import yaml
from pydantic import BaseModel

from .models import SCENARIO_SKILL_TYPE, Mode, SkillKind
from .sections import (
    ambiguous_titles,
    h2_sections,
    h2_titles,
    match_section,
    normalize_section,
)

# Every top-level frontmatter key is republished verbatim into the host skill
# catalog on every listing, so metadata size is a recurring context cost.
METADATA_SIZE_WARN_CHARS = 2000

SCENARIO_FORBIDDEN_SECTIONS = (
    "## Environment Variables",
    "## OBO Token Scopes",
    "## API Reference / Sample Code",
)

_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

# B class is about emitting a conservatively-parseable file, never about meaning.
_B_CLASS_RULES = frozenset({"T1", "T2", "T3"})

# A pointer is routinely wrapped across lines, so the argument list is matched
# with DOTALL rather than per-line.
_FETCH_SKILL_RE = re.compile(r"fetch_skill[ \t]*\(([^)]*)\)", re.DOTALL)
_SKILL_NAME_ARG_RE = re.compile(r"skill_name[ \t]*=[ \t]*[\"']([^\"']+)[\"']")
_SECTIONS_ARG_RE = re.compile(r"sections[ \t]*=[ \t]*[\"']([^\"']*)[\"']")
# A skill name is only recognised as a mention when it is backticked and
# hyphenated; anything looser matches ordinary prose.
_SKILL_MENTION_RE = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`")
_MISSING_CODE_RE = re.compile(r"missing=[A-Za-z0-9_,\- ]+")
_DECLARED_INPUT_RE = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
_NEEDS_INFO_MISSING_RE = re.compile(r"\[NEEDS_INFO\][ \t]*missing=([A-Za-z0-9_,\- ]+)")
_PY_FENCE_RE = re.compile(r"```(?:python|py)[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DEEP_HEADING_RE = re.compile(r"^(#{3,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# Section names a scenario skill is expected to point at by name.
_POINTABLE_MARKERS = ("requiredinputs", "needsinfo")

# Sections that exist to name OTHER skills as routing alternatives. The names
# in them are deliberately not dependencies, so they are excluded from P5.
_NEGATIVE_ROUTING_MARKERS = ("不適用", "notapplicable", "whennottouse")
_RETURN_BRANCH_MARKERS = ("returnbranch", "結果分支", "回傳分支")


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class TopologyIssue(BaseModel):
    rule: str
    severity: Severity
    message: str


def has_errors(issues: list[TopologyIssue]) -> bool:
    return any(issue.severity is Severity.ERROR for issue in issues)


def _severity_for(rule: str, mode: Mode) -> Severity:
    if rule in _B_CLASS_RULES and mode in (Mode.MODIFY, Mode.IMPORT):
        return Severity.WARNING
    return Severity.ERROR


def declared_children(frontmatter: dict) -> list[str]:
    """Children in the one shape every known parser accepts: a non-empty list of str.

    Anything else (scalar, tuple, empty list, top-level key) reads as "no
    children" here on purpose; the rules below report why.
    """
    meta = frontmatter.get("metadata")
    if not isinstance(meta, dict):
        return []
    children = meta.get("children")
    if not isinstance(children, list):
        return []
    return [c for c in children if isinstance(c, str) and c.strip()]


def parse_frontmatter_block(skill_md: str) -> tuple[str | None, dict | None]:
    """Return (raw block, parsed mapping). Either element is None when unusable."""
    text = (skill_md or "").lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, None
    block = match.group(1)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return block, None
    return block, data if isinstance(data, dict) else None


def validate_topology(
    skill_md: str,
    kind: SkillKind,
    *,
    mode: Mode,
    expected_name: str | None = None,
    child_resolver: Callable[[str], str | None] | None = None,
) -> list[TopologyIssue]:
    """Check a SKILL.md against the topology rules for its layer.

    ``child_resolver`` maps a child skill name to its SKILL.md, or None when it
    does not exist or the caller has no access. T7 is skipped when it is None.
    """
    issues: list[TopologyIssue] = []

    def add(rule: str, message: str, severity: Severity | None = None) -> None:
        issues.append(
            TopologyIssue(
                rule=rule,
                severity=severity or _severity_for(rule, mode),
                message=message,
            )
        )

    raw = skill_md or ""

    if raw.startswith("\ufeff"):
        add("T1", "The file must not start with a byte order mark.")
    stripped = raw.lstrip("\ufeff")
    if stripped[:1].isspace():
        add("T1", "The frontmatter fence must be the very first line; remove the leading blank line or whitespace.")

    block, frontmatter = parse_frontmatter_block(raw)

    if block is None:
        add("T1", "The file must open with a `---` fence on its own line and close it with another `---` line.")
        return issues

    if "---" in block:
        add("T2", "The frontmatter block must not contain `---` anywhere inside it, including within a quoted value.")

    if frontmatter is None:
        add("T3", "The frontmatter must be a YAML mapping that parses cleanly. Quote any value containing `: `.")
        return issues

    _check_name(frontmatter, expected_name, add)
    _check_children_shape(frontmatter, kind, add)
    _check_skill_type(frontmatter, add)
    _check_metadata_size(frontmatter, add)
    _check_section_contract(stripped, kind, add)

    if kind is SkillKind.SCENARIO:
        _check_scenario_body(stripped, add)
        if child_resolver is not None:
            children = declared_children(frontmatter)
            _check_children_resolve(children, child_resolver, add)
            _check_pointer_contract(
                stripped,
                children,
                child_resolver,
                add,
                own_name=str(frontmatter.get("name") or "").strip(),
            )

    return issues


def _check_name(frontmatter: dict, expected_name: str | None, add) -> None:
    if not expected_name:
        return
    actual = str(frontmatter.get("name") or "").strip()
    if actual != expected_name:
        add(
            "T6",
            f"The frontmatter `name` is `{actual or '(missing)'}` but the skill is stored as "
            f"`{expected_name}`. They must be identical.",
        )


def _check_children_shape(frontmatter: dict, kind: SkillKind, add) -> None:
    meta = frontmatter.get("metadata")
    meta_children = meta.get("children") if isinstance(meta, dict) else None
    has_top_level = "children" in frontmatter

    if kind is SkillKind.CAPABILITY:
        if meta_children is not None or has_top_level:
            add("T5", "A capability skill must not declare `children`.")
        return

    if has_top_level:
        add("T4", "`children` must be nested under `metadata`, not declared at the top level.")
    if not isinstance(meta, dict):
        add("T4", "A scenario skill must declare a `metadata` mapping containing `children`.")
        return
    if meta_children is None:
        add("T4", "A scenario skill must declare `metadata.children`.")
        return
    if isinstance(meta_children, str):
        add("T4", "`metadata.children` must be a YAML list. A bare string is not accepted by every parser.")
        return
    if not isinstance(meta_children, list):
        add("T4", "`metadata.children` must be a YAML list of skill names.")
        return
    if not meta_children:
        add("T4", "`metadata.children` must not be empty.")
        return
    if not all(isinstance(c, str) and c.strip() for c in meta_children):
        add("T4", "Every entry in `metadata.children` must be a non-empty string.")


def _check_skill_type(frontmatter: dict, add) -> None:
    meta = frontmatter.get("metadata")
    skill_type = str(meta.get("skill_type") or "").strip() if isinstance(meta, dict) else ""
    declares_scenario = skill_type == SCENARIO_SKILL_TYPE
    has_children = bool(declared_children(frontmatter))
    if declares_scenario and not has_children:
        add(
            "T10",
            f"`metadata.skill_type: {SCENARIO_SKILL_TYPE}` requires a non-empty `metadata.children` list.",
        )
    elif has_children and not declares_scenario:
        add(
            "T10",
            f"A skill declaring `metadata.children` must also set `metadata.skill_type: {SCENARIO_SKILL_TYPE}`.",
        )


def _check_metadata_size(frontmatter: dict, add) -> None:
    meta = frontmatter.get("metadata")
    if not isinstance(meta, dict):
        return
    size = len(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False))
    if size > METADATA_SIZE_WARN_CHARS:
        add(
            "T8",
            f"`metadata` serializes to {size} characters (soft limit {METADATA_SIZE_WARN_CHARS}). "
            "Move long prose into the body.",
            Severity.WARNING,
        )


def _check_scenario_body(skill_md: str, add) -> None:
    for heading in SCENARIO_FORBIDDEN_SECTIONS:
        if re.search(rf"^{re.escape(heading)}\s*$", skill_md, re.MULTILINE):
            add("T9", f"A scenario skill must not contain a `{heading}` section.")


def _check_children_resolve(
    children: list[str],
    child_resolver: Callable[[str], str | None],
    add,
) -> None:
    for child in children:
        child_md = child_resolver(child)
        if child_md is None:
            add("T7", f"Child skill `{child}` does not exist or is not accessible to you.")
            continue
        _, child_fm = parse_frontmatter_block(child_md)
        if child_fm and declared_children(child_fm):
            add("T7", f"Child skill `{child}` declares its own `children`; only one level of nesting is allowed.")


# ---------------------------------------------------------------------------
# P class -- the scenario layer's pointer contract
# ---------------------------------------------------------------------------


def _is_negative_routing(title: str) -> bool:
    folded = normalize_section(title)
    return any(marker in folded for marker in _NEGATIVE_ROUTING_MARKERS)


def _is_return_branch(title: str) -> bool:
    folded = normalize_section(title)
    return any(marker in folded for marker in _RETURN_BRANCH_MARKERS)


@dataclass(frozen=True)
class _Pointer:
    skill: str
    sections: tuple[str, ...]


def _pointers(skill_md: str) -> list[_Pointer]:
    out: list[_Pointer] = []
    for args in _FETCH_SKILL_RE.findall(skill_md or ""):
        name_match = _SKILL_NAME_ARG_RE.search(args)
        if not name_match:
            continue
        sections_match = _SECTIONS_ARG_RE.search(args)
        raw = sections_match.group(1) if sections_match else ""
        wanted = tuple(part.strip() for part in raw.split(",") if part.strip())
        out.append(_Pointer(skill=name_match.group(1).strip(), sections=wanted))
    return out


def _child_input_names(child_md: str) -> list[str]:
    for title, body in h2_sections(child_md):
        if "requiredinputs" in normalize_section(title):
            return list(dict.fromkeys(_DECLARED_INPUT_RE.findall(body)))
    return []


def _check_pointer_contract(
    skill_md: str,
    children: list[str],
    child_resolver: Callable[[str], str | None],
    add,
    *,
    own_name: str = "",
) -> None:
    pointers = _pointers(skill_md)
    declared = set(children)

    seen: dict[str, int] = {}
    for pointer in pointers:
        seen[pointer.skill] = seen.get(pointer.skill, 0) + 1

        if pointer.skill not in declared:
            add(
                "P1",
                f"The body calls `fetch_skill(skill_name=\"{pointer.skill}\")` but `{pointer.skill}` "
                "is not listed in `metadata.children`. Every skill this scenario uses must be "
                "declared there.",
            )
            continue

        if not pointer.sections:
            continue
        child_md = child_resolver(pointer.skill)
        if child_md is None:
            continue  # T7 already reports an unresolvable child.
        titles = h2_titles(child_md)
        for wanted in pointer.sections:
            if match_section(wanted, titles):
                continue
            available = "、".join(f"`{t}`" for t in titles) or "(none)"
            add(
                "P2",
                f"`fetch_skill(skill_name=\"{pointer.skill}\", sections=...)` names section "
                f"`{wanted}`, which is not a `##` heading of `{pointer.skill}`. "
                f"Available section names: {available}.",
            )

    for skill, count in seen.items():
        if count > 1:
            add(
                "P3",
                f"`{skill}` is fetched {count} times. Call `fetch_skill` once per skill and list "
                "every section you need in that single call.",
            )

    if pointers and "list_skills" not in (skill_md or ""):
        add(
            "P4",
            "The body points at child sections but never states that these skills are absent from "
            "`list_skills` and may still be fetched. Without that paragraph the pointer chain "
            "reads as forbidden.",
        )

    _check_undeclared_mentions(skill_md, declared, child_resolver, add, own_name=own_name)
    _check_restated_inputs(skill_md, children, child_resolver, add)


def _check_undeclared_mentions(
    skill_md: str,
    declared: set[str],
    child_resolver: Callable[[str], str | None],
    add,
    *,
    own_name: str = "",
) -> None:
    """P5 -- omitting a skill from ``children`` fails silently, so name it here."""
    seen: set[str] = set()
    for title, body in h2_sections(skill_md):
        if _is_negative_routing(title):
            continue
        for name in _SKILL_MENTION_RE.findall(body):
            if name in declared or name == own_name or name in seen:
                continue
            seen.add(name)
            if child_resolver(name) is None:
                continue  # Not a real skill, just a hyphenated token.
            add(
                "P5",
                f"The body refers to skill `{name}`, which is not listed in `metadata.children`. "
                "`children` is the whitelist of every skill this scenario may use.",
            )


def _check_restated_inputs(
    skill_md: str,
    children: list[str],
    child_resolver: Callable[[str], str | None],
    add,
) -> None:
    """P6 -- the child's body is the only source of the field contract."""
    scannable = "\n".join(
        _MISSING_CODE_RE.sub("", body)
        for title, body in h2_sections(skill_md)
        if not _is_negative_routing(title) and not _is_return_branch(title)
    )
    for child in children:
        child_md = child_resolver(child)
        if child_md is None:
            continue
        restated = [name for name in _child_input_names(child_md) if f"`{name}`" in scannable]
        if restated:
            add(
                "P6",
                f"The body restates input field(s) {', '.join('`' + n + '`' for n in restated)} "
                f"declared in `{child}`'s `## Required Inputs`. Point at that section instead; a "
                "copy drifts in one direction only.",
                Severity.WARNING,
            )


# ---------------------------------------------------------------------------
# C class -- a capability's section names are a published API
# ---------------------------------------------------------------------------


def _check_section_contract(skill_md: str, kind: SkillKind, add) -> None:
    titles = h2_titles(skill_md)

    for first, second in ambiguous_titles(titles):
        add(
            "C1",
            f"Sections `{first}` and `{second}` cannot be addressed independently: after "
            "normalization one name contains the other, so naming the shorter one always "
            "returns both. Rename one of them.",
        )

    _check_preamble(skill_md, add)

    if kind is SkillKind.CAPABILITY:
        _check_pointable_sections(skill_md, add)


def _check_pointable_sections(skill_md: str, add) -> None:
    """C2 -- what a parent points at must be its own ``##`` section."""
    for hashes, title in _DEEP_HEADING_RE.findall(skill_md or ""):
        folded = normalize_section(title)
        if any(marker in folded for marker in _POINTABLE_MARKERS):
            add(
                "C2",
                f"`{title}` is a `{hashes}` heading. A scenario skill can only name `##` "
                "sections; deeper headings are folded into the `##` above them and cannot be "
                "requested on their own.",
            )

    code = "\n".join(_PY_FENCE_RE.findall(skill_md or ""))
    codes = list(
        dict.fromkeys(
            part.strip()
            for group in _NEEDS_INFO_MISSING_RE.findall(code)
            for part in group.split(",")
            if part.strip()
        )
    )
    if not codes:
        return

    contract = next(
        (
            body
            for title, body in h2_sections(skill_md)
            if "needsinfo" in normalize_section(title)
        ),
        None,
    )
    if contract is None:
        add(
            "C2",
            "The sample code prints `[NEEDS_INFO]`, but no `##` section is dedicated to the "
            "needs-info contract. A scenario skill points at that section by name, so it must "
            "exist on its own.",
        )
        return
    undocumented = [item for item in codes if item not in contract]
    if undocumented:
        add(
            "C2",
            "The needs-info section does not list "
            f"{', '.join('`' + c + '`' for c in undocumented)}, which the sample code can print. "
            "That section must list every `missing=` code.",
        )


def _check_preamble(skill_md: str, add) -> None:
    """C3 -- only ``##`` sections are addressable, so nothing may live above the first one."""
    text = skill_md or ""
    match = _FRONTMATTER_RE.match(text)
    body = text[match.end() :] if match else text
    head, sep, _rest = body.partition("\n## ")
    if not sep:
        return
    substantive = [
        line
        for line in head.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if substantive:
        add(
            "C3",
            f"{len(substantive)} line(s) of content sit above the first `##` heading. Only `##` "
            "sections can be requested by name, so that text is unreachable. Move it into a "
            "section.",
            Severity.WARNING,
        )
