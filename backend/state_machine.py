"""Stage transition, quality gates, and system-prompt assembly for Skill Generator v2 (5-stage)."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from .eaa_platform import PLATFORM_SECRET_DENYLIST, obo_registry_mapping, script_flags_off
from .input_contract import input_contract_errors

from .material_fidelity import (
    CONTEXT_ONLY_KINDS,
    NO_INVENTION_KINDS,
    VERBATIM_KINDS,
    materials_for_prompt,
)
from .models import (
    ChatMessage,
    MaterialKind,
    MessageRole,
    Mode,
    PrepareBrief,
    Session,
    SkillKind,
    Stage,
    TestResult,
    prepare_checklist_for,
)
from .skill_lint import is_needs_info_response, needs_info_line, script_argument_names, script_only_errors
from .topology import declared_children, parse_frontmatter_block

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"

STAGE_PROMPT_MAP: dict[Stage, str] = {
    Stage.PREPARE: "01_prepare.md",
    Stage.DRAFT: "02_draft.md",
    Stage.REFINE: "03_refine.md",
    Stage.TEST: "04_test.md",
    Stage.DONE: "05_done.md",
}

# Stage prompts that are REPLACED wholesale for a given kind.
KIND_PROMPT_OVERRIDES: dict[SkillKind, dict[Stage, str]] = {
    SkillKind.SCENARIO: {
        Stage.DRAFT: "02_draft_scenario.md",
    },
}

# Stage prompts that are kept and APPENDED to for a given kind. Forking a whole
# stage prompt duplicates everything that is not layer-specific, and the copies
# drift apart; an addendum only carries the differences.
KIND_STAGE_ADDENDA: dict[SkillKind, dict[Stage, str]] = {
    SkillKind.SCENARIO: {
        Stage.PREPARE: "01_prepare_scenario_addendum.md",
        Stage.TEST: "04_test_scenario_addendum.md",
    },
}

KIND_FORMAT_SPEC: dict[SkillKind, str] = {
    SkillKind.CAPABILITY: "10_format_spec.md",
    SkillKind.SCENARIO: "10_format_spec_scenario.md",
}

# Appended to the stage prompt by skill FORM, which is orthogonal to SkillKind (see _form_prompt_key).
# "script_candidate" = a NEW capability skill whose form is not locked yet, with a code material and the flags on.
FORM_STAGE_ADDENDA: dict[str, dict[Stage, tuple[str, ...]]] = {
    "script_candidate": {
        Stage.PREPARE: ("01_prepare_script_addendum.md",),
    },
    "script": {
        Stage.DRAFT: ("02_draft_script_addendum.md", "13_script_save.md"),
        Stage.REFINE: ("02_draft_script_addendum.md", "13_script_save.md"),
        Stage.TEST: ("04_test_script_addendum.md", "13_script_save.md"),
        Stage.DONE: ("13_script_save.md",),
    },
}

# One line appended to the eaa_flags reason in `## Skill Form`; flags-off sessions get no addendum.
SKILL_FORM_FLAGS_OFF_NOTE = "01_prepare_script_flags_off.md"
SKILL_FORM_LOOKUP_FAILED_NOTE = "01_prepare_script_lookup_failed.md"

# Appended only while the session carries user-supplied assets.
ASSET_STAGE_ADDENDA: dict[Stage, str] = {
    Stage.DRAFT: "14_skill_assets.md",
    Stage.REFINE: "14_skill_assets.md",
    Stage.TEST: "14_skill_assets.md",
}


def stage_prompt_for(kind: SkillKind, stage: Stage) -> str:
    return KIND_PROMPT_OVERRIDES.get(kind, {}).get(stage, STAGE_PROMPT_MAP[stage])


# Allowed direct transitions; reason explains why the move is appropriate.
TRANSITION_TABLE: dict[Stage, dict[Stage, str]] = {
    Stage.PREPARE: {
        Stage.DRAFT: "Prepare quality gate is satisfied; generate the first complete SKILL.md.",
    },
    Stage.DRAFT: {
        Stage.REFINE: "User accepted the initial SKILL.md; continue with focused V4A patches.",
        Stage.PREPARE: "User wants to rethink the scope or direction; clear the draft and re-plan.",
    },
    Stage.REFINE: {
        Stage.TEST: "Refinements look good; run skill-selection tests.",
        Stage.PREPARE: "User changed direction substantially; rebuild the Prepare brief.",
        Stage.DONE: "Refinement is complete and validated.",
    },
    Stage.TEST: {
        Stage.REFINE: "Test results processed via reflection; iterate on the draft.",
        Stage.PREPARE: "Test outcomes revealed a fundamental scope problem; rethink the plan.",
        Stage.DONE: "Test results are acceptable; finalize.",
    },
    Stage.DONE: {
        Stage.REFINE: "User reopened with a focused edit request.",
        Stage.PREPARE: "User reopened with a major scope change.",
        Stage.TEST: "User reopened to re-validate the skill.",
    },
}

ALLOWED_TRANSITIONS: dict[Stage, set[Stage]] = {
    src: set(targets.keys()) for src, targets in TRANSITION_TABLE.items()
}


IMPORT_MODE_ADDENDUM = """\
## Import Mode Addendum

The user is bringing an existing non-system skill. Preserve useful behavior, but
rewrite the output into the required Skill Generator format. Explicitly identify
format gaps during PREPARE before drafting.
"""

MODIFY_MODE_ADDENDUM = """\
## Modify Mode Addendum

The user is modifying an existing system skill. Default to conservative edits.
Avoid broad rewrites unless the user explicitly asks for them or the section is
structurally incompatible with the requested change.
"""


SCENARIO_KIND_ADDENDUM = """\
## Scenario Skill Addendum

This session authors a SCENARIO (orchestration) skill, not a capability skill.

- It is never executed by the backend. It tells the HOST agent what to do with
  its own capabilities, what to delegate, and how to react to each returned
  branch.
- It declares `metadata.children` (a non-empty YAML list) and
  `metadata.skill_type: scenario-orchestration`. Those two imply each other and
  must never be removed or reshaped by a patch.
- `metadata.children` is a WHITELIST of every skill this scenario's flow uses,
  not just the ones it delegates a payload to. A skill left out never becomes
  available to the host and there is NO error message, so when in doubt, list
  it. Skills named as routing alternatives ("not applicable -> use X") are not
  part of the flow and do not belong there.
- It does NOT restate a child's field contract. Field names, types, required
  flags, code lookup tables and payload structure stay in the child's body and
  are reached with `fetch_skill(skill_name=..., sections="A, B")`. A copy drifts
  in one direction only. What stays here is what only the host can do: reading
  images, designing question rounds, judging output format, branching on the
  result, and continuing a multi-turn exchange.
- The body must state that these skills are absent from the host's
  `list_skills` catalog and that being absent does not forbid fetching their
  body -- and that their names may appear only as `fetch_skill` arguments.
- It has NO variables of its own: no `## Environment Variables`, no
  `## OBO Token Scopes`, no `## API Reference / Sample Code`. Those belong to
  the child capability skill. `record_variables` is rejected in this session.
- The third PREPARE checkpoint is `delegation_ok`, recorded with
  `record_delegation`, not `variables_ok`.
- Edit a child with `propose_child_edit`, never by drafting the child here.
  Renaming one of a child's `##` headings is a breaking change: every parent
  pointing at that name must be updated in the same turn.
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QualityGateError(Exception):
    """Raised when PREPARE -> DRAFT is requested before the gate is satisfied."""

    def __init__(self, missing: list[str], message: str = "Quality gate failed") -> None:
        super().__init__(message)
        self.missing = list(missing)


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def load_prompt(filename: str) -> str:
    path = PROMPT_DIR / filename
    if not path.exists():
        return f"<!-- Missing prompt: {filename} -->"
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------


# Single source of truth: derive from the canonical default for the session's
# kind, so order/renames only need to change in models.
def _checklist_keys(session: Session) -> tuple[str, ...]:
    return tuple(prepare_checklist_for(session.skill_kind))


def _check_scenario_children(session: Session, missing: list[str]) -> None:
    """T6/T7 as far as they can be decided before a draft exists.

    Both are answered from ``child_full_md``, which is loaded best-effort on
    stage entry. A child that could not be loaded is reported as unresolved
    rather than assumed fine -- a scenario skill whose child is missing has no
    working delegation at all.
    """
    if not session.children:
        missing.append("children")
        return
    for child in session.children:
        child_md = session.child_full_md.get(child)
        if not child_md:
            missing.append(f"children.unresolved:{child}")
            continue
        _, frontmatter = parse_frontmatter_block(child_md)
        if not frontmatter:
            missing.append(f"children.unparseable:{child}")
            continue
        # T6: the host matches children against the child's frontmatter name, so
        # a name that disagrees with the stored name breaks the link silently.
        actual = str(frontmatter.get("name") or "").strip()
        if actual != child:
            missing.append(f"children.name_mismatch:{child}")
        # T7: only one level of nesting.
        if declared_children(frontmatter):
            missing.append(f"children.nested:{child}")


def check_quality_gates(session: Session) -> list[str]:
    """Return a list of missing requirement keys for PREPARE -> DRAFT.

    Empty list means the gate passes.
    """

    missing: list[str] = []
    brief: PrepareBrief = session.prepare_brief
    kind = SkillKind(session.skill_kind)
    missing.extend(input_contract_errors(session))

    if not brief.understanding.skill_goal.strip():
        missing.append("understanding.skill_goal")
    if kind is SkillKind.SCENARIO:
        # A scenario skill reads nothing itself; what stands in for input
        # sources is what the HOST must do because the child cannot.
        if not any(d.host_capabilities for d in brief.delegation):
            missing.append("delegation.host_capabilities")
        _check_scenario_children(session, missing)
    elif len(brief.understanding.input_sources) < 1:
        missing.append("understanding.input_sources")
    if len(brief.understanding.key_capabilities) < 1:
        missing.append("understanding.key_capabilities")
    if not brief.understanding.differentiation.strip():
        missing.append("understanding.differentiation")
    if (
        len(brief.research.adjacent_skills) < 1
        and not (brief.research.summary or "").strip()
    ):
        missing.append("research.adjacent_skills")
    # existing_skills_overlap may legitimately be empty (no matches found),
    # but the field must have been considered: web_status not "skipped".
    if brief.research.web_status == "skipped":
        missing.append("research.web_status")
    for key in _checklist_keys(session):
        if not brief.verify_checklist.get(key, False):
            missing.append(f"verify_checklist.{key}")
    # If a very-high-similarity overlap exists, differentiation must mention it.
    overlaps = brief.research.existing_skills_overlap
    if overlaps:
        top = max(overlaps, key=lambda o: o.similarity)
        if top.similarity > 0.8 and top.name.lower() not in brief.understanding.differentiation.lower():
            missing.append(f"differentiation_must_mention:{top.name}")
    return missing


# ---------------------------------------------------------------------------
# Skill form (inline sample code vs bundled script)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormCheck:
    """One unmet script-form condition. States what failed, never how to fix it."""

    check: str
    message: str
    rule: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "message": self.message, "rule": self.rule}


@dataclass(frozen=True)
class SkillFormVerdict:
    form: Literal["inline", "script"]
    locked: bool
    failures: tuple[FormCheck, ...] = ()
    # The code material shipped verbatim when an unlocked verdict is script.
    script: str | None = None

    def to_dict(self) -> dict:
        return {
            "form": self.form,
            "locked": self.locked,
            "failures": [failure.to_dict() for failure in self.failures],
        }


def script_readiness_problems(session: Session, script: str) -> list[FormCheck]:
    """Why ``script`` cannot ship verbatim as this session's bundled script; empty when it can."""
    try:
        errors = script_only_errors(script)
    except SyntaxError as exc:
        return [FormCheck("parses", f"The code material is not valid Python: {exc.msg} (line {exc.lineno}).")]
    problems = [FormCheck("script_lint", issue.message, issue.rule) for issue in errors]
    if not _runs_when_executed(script):
        problems.append(FormCheck(
            "entry_point",
            "The code material only imports and defines functions, classes and constants, so running "
            "it as a script performs none of its operations.",
        ))
    accepted = script_argument_names(script)
    unbound = [
        v.name for v in session.prepare_brief.variables
        if v.kind == "runtime" and v.source == "request" and v.name and v.name.upper() not in accepted
    ]
    if unbound:
        problems.append(FormCheck(
            "inputs",
            "These request inputs have no matching `add_argument` flag: "
            + ", ".join(f"`{name}`" for name in unbound)
            + ". The host passes a bundled script's business inputs only as `--flag` arguments; "
            "environment variables carry deployment settings and credentials, never values from "
            "the user's request.",
        ))
    return problems


_DEFINITION_NODES = (
    ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Pass,
)


def _runs_when_executed(script: str) -> bool:
    """A top-level statement other than a definition, e.g. a call or an `if __name__ == "__main__":` block."""
    for node in ast.parse(script).body:
        if isinstance(node, _DEFINITION_NODES):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        return True
    return False


def evaluate_skill_form(session: Session) -> SkillFormVerdict:
    """Script form iff every condition holds; otherwise inline with each unmet condition listed."""
    if session.skill_form is not None:
        return SkillFormVerdict(form=session.skill_form, locked=True)
    if Mode(session.mode) is Mode.MODIFY:
        return SkillFormVerdict(
            form="script" if session.current_skill.script is not None else "inline", locked=True
        )
    failures: list[FormCheck] = []
    if session.prepare_brief.prefer_inline:
        failures.append(FormCheck("user_choice", "The user chose inline sample code over a bundled script."))
    if SkillKind(session.skill_kind) is SkillKind.SCENARIO:
        failures.append(FormCheck("skill_kind", "A scenario skill has no script of its own."))
    if Mode(session.mode) is not Mode.NEW:
        failures.append(FormCheck("mode", f"Only a new skill can take the script form; this session is {session.mode}."))
    if session.assets:
        failures.append(FormCheck(
            "assets",
            f"{len(session.assets)} skill asset(s) are attached; a script skill ships only SKILL.md and its script.",
        ))
    flags_off = script_flags_off(session.aca_env_result)
    if flags_off and flag_lookup_failed(session):
        failures.append(FormCheck(
            "eaa_flags",
            "Could not read the EAA script flags: the ACA lookup failed "
            f"({_short_error(session.aca_env_error)}). Until a lookup succeeds it is unknown whether "
            "EAA runs script skills.",
        ))
    elif flags_off:
        failures.append(FormCheck(
            "eaa_flags",
            f"EAA does not run script skills: {', '.join(flags_off)} is not true in the ACA "
            "architectural_config (missing counts as false).",
        ))
    codes = [m for m in session.materials if MaterialKind(m.kind) is MaterialKind.CODE]
    script: str | None = None
    if len(codes) != 1:
        failures.append(FormCheck("code_material", f"Exactly one code material is required; there are {len(codes)}."))
    else:
        script = codes[0].content
        failures += script_readiness_problems(session, script)
    brief = session.prepare_brief
    if not (brief.script_covers_operations and brief.verify_checklist.get("variables_ok", False)):
        failures.append(FormCheck(
            "covers_operations",
            "It is not confirmed in variables_ok that the code material covers every operation.",
        ))
    if failures:
        return SkillFormVerdict(form="inline", locked=False, failures=tuple(failures))
    return SkillFormVerdict(form="script", locked=False, script=script)


def flag_lookup_failed(session: Session) -> bool:
    return session.aca_env_result is None and bool(session.aca_env_error)


def _short_error(error: str) -> str:
    # ACA returns a whole HTML page after the status line.
    head = error.split(" | ", 1)[0].strip()
    return head if len(head) <= 120 else head[:117] + "..."


def _awaits_flags(session: Session) -> bool:
    """A NEW capability skill with a code material whose form still depends on the EAA flags."""
    return (
        session.skill_form is None
        and not session.prepare_brief.prefer_inline
        and SkillKind(session.skill_kind) is SkillKind.CAPABILITY
        and Mode(session.mode) is Mode.NEW
        and bool(code_material_contents(session))
    )


def needs_flag_lookup_before_lock(session: Session) -> bool:
    """Leaving PREPARE would lock the form without ever having read the EAA flags."""
    return Stage(session.current_stage) is Stage.PREPARE and _awaits_flags(session) and session.aca_env_result is None


SCRIPT_LOCKED_INLINE_MESSAGE = (
    "The skill form is now locked as inline, not as a bundled script. These script-form conditions "
    "were unmet when PREPARE ended:\n{failures}\nIn your next reply, tell the user plainly that this "
    "skill ships inline sample code, name these conditions, and correct any earlier statement that it "
    "would be a bundled script. Switching to a bundled script needs a new session."
)

SCRIPT_LOCKED_SCRIPT_MESSAGE = (
    "The skill form is now locked as a bundled script: the code material ships verbatim next to "
    "SKILL.md. In your next reply, tell the user plainly that this skill is a bundled script and "
    "correct any earlier statement that it would ship inline sample code. Switching to inline sample "
    "code needs a new session."
)


def lock_skill_form(session: Session) -> None:
    """Fix the form for the rest of the session; a NEW script skill takes its code material verbatim."""
    if session.skill_form is not None:
        return
    candidate = _form_prompt_key(session) == "script_candidate" or (
        _awaits_flags(session) and flag_lookup_failed(session)
    )
    verdict = evaluate_skill_form(session)
    session.skill_form = verdict.form
    if not verdict.locked and verdict.form == "script":
        session.current_skill.script = verdict.script
        session.conversation.append(ChatMessage(role=MessageRole.SYSTEM, content=SCRIPT_LOCKED_SCRIPT_MESSAGE))
    elif candidate and verdict.failures:
        session.conversation.append(ChatMessage(
            role=MessageRole.SYSTEM,
            content=SCRIPT_LOCKED_INLINE_MESSAGE.format(
                failures="\n".join(_form_check_line(failure) for failure in verdict.failures)
            ),
        ))


def code_material_contents(session: Session) -> list[str]:
    return [m.content for m in session.materials if MaterialKind(m.kind) is MaterialKind.CODE]


def script_replacement_problems(session: Session) -> list[FormCheck]:
    """Why the code materials cannot replace a locked script; empty when there is nothing to replace."""
    codes = code_material_contents(session)
    if session.skill_form != "script" or not codes or codes == [session.current_skill.script]:
        return []
    if len(codes) != 1:
        return [FormCheck(
            "code_material",
            f"Exactly one code material is required to replace the script; there are {len(codes)}.",
        )]
    return script_readiness_problems(session, codes[0])


SCRIPT_REPLACED_MESSAGE = (
    "The bundled script was replaced by the new code material. Confirm with the user that the new "
    "code still covers every operation this skill documents."
)


def apply_code_material_change(session: Session, before: list[str]) -> None:
    """Run after any entry point changed ``session.materials``; ``before`` = code_material_contents beforehand."""
    if code_material_contents(session) == before:
        return
    session.material_patch_rejections = 0
    if session.skill_form is None:
        brief = session.prepare_brief
        brief.script_covers_operations = False
        brief.verify_checklist["variables_ok"] = False
        brief.verify_evidence.pop("variables_ok", None)
        return
    codes = code_material_contents(session)
    if (
        session.skill_form == "script"
        and len(codes) == 1
        and codes[0] != session.current_skill.script
        and not script_replacement_problems(session)
    ):
        session.current_skill.script = codes[0]
        session.conversation.append(ChatMessage(role=MessageRole.SYSTEM, content=SCRIPT_REPLACED_MESSAGE))


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def can_transition(current: Stage, target: Stage) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


# Module-level post-transition hook (registered by main.py).
PostTransitionHook = None  # type: ignore[var-annotated]


def register_post_transition_hook(hook) -> None:
    """Register a callable hook(session, source: Stage, target: Stage).

    The hook is invoked AFTER a successful stage change. Exceptions are
    swallowed and logged via the hook's own error handling.
    """
    global PostTransitionHook
    PostTransitionHook = hook


def transition(session: Session, target: Stage, summary: str = "") -> None:
    current = Stage(session.current_stage)
    if current == target:
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=f"Stage transition skipped: already in {target.value}. {summary}".strip(),
            )
        )
        session.touch()
        return
    if not can_transition(current, target):
        raise ValueError(f"Invalid stage transition: {current.value} -> {target.value}")
    if current == Stage.PREPARE and target == Stage.DRAFT:
        missing = check_quality_gates(session)
        if missing:
            raise QualityGateError(missing)
        lock_skill_form(session)
    if target == Stage.PREPARE and current in {Stage.DRAFT, Stage.REFINE, Stage.TEST, Stage.DONE}:
        # Coming back to PREPARE: clear the draft but keep the brief & materials.
        session.current_skill.skill_md = ""
        session.current_skill.version_hash = ""
        session.prepare_brief.revisit = True
    if target == Stage.DONE and session.iteration_reflections:
        # Finalizing accepts the skill as-is, so whatever is still open was declined.
        session.iteration_reflections[-1].skipped.extend(open_fix_items(session)[1])
    session.current_stage = target
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=f"Stage transition: {current.value} -> {target.value}. {summary}".strip(),
        )
    )
    session.touch()
    if PostTransitionHook is not None:
        try:
            PostTransitionHook(session, current, target)
        except Exception:  # noqa: BLE001 - hook errors must not break transitions.
            pass


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------


def _format_allowed_exits(stage: Stage) -> str:
    exits = TRANSITION_TABLE.get(stage) or {}
    if not exits:
        return "## Allowed Stage Transitions\n\nNo further transitions; this is a terminal stage."
    lines = [
        "## Allowed Stage Transitions",
        "",
        f"From `{stage.value}` you may emit `request_stage_transition` to:",
    ]
    for target, reason in exits.items():
        lines.append(f"- `{target.value}` -- {reason}")
    lines.append("")
    lines.append("Pick the transition whose reason matches the actual situation; do not invent new edges.")
    return "\n".join(lines)


def _item_label(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or value.get("id") or value)
    return str(value)


def _item_field(value, key: str) -> str:
    return str(value.get(key, "") or "") if isinstance(value, dict) else ""


def _format_prepare_brief(session: Session) -> str | None:
    brief = session.prepare_brief
    u = brief.understanding
    r = brief.research
    # Skip if nothing recorded yet (PREPARE just started).
    has_understanding = bool(u.skill_goal or u.key_capabilities or u.differentiation)
    has_research = (
        r.web_status != "skipped"
        or r.summary
        or r.adjacent_skills
        or r.existing_skills_overlap
    )
    if not (has_understanding or has_research or brief.revisit):
        return None
    lines = ["## Prepare Brief"]
    if brief.revisit:
        lines.append("")
        lines.append("(revisit=true; prior draft was discarded, brief preserved)")
    lines.append("")
    lines.append("### Understanding")
    lines.append(f"- skill_goal: {u.skill_goal or '(empty)'}")
    if u.input_sources:
        lines.append(f"- input_sources: {', '.join(u.input_sources)}")
    if u.key_capabilities:
        lines.append("- key_capabilities:")
        for c in u.key_capabilities:
            lines.append(f"  - {c}")
    if u.out_of_scope:
        lines.append("- out_of_scope: " + ", ".join(u.out_of_scope))
    if u.differentiation:
        lines.append(f"- differentiation: {u.differentiation}")
    if u.neighbor_skills:
        lines.append("- neighbor_skills (single source for routing_uniqueness):")
        for nb in u.neighbor_skills:
            lines.append(f"  - {nb.skill}: axis={nb.axis}; scenario={nb.scenario}")
    if u.open_questions:
        lines.append("- open_questions:")
        for q in u.open_questions:
            lines.append(f"  - {q}")
    lines.append("")
    lines.append("### Research")
    lines.append(f"- web_status: {r.web_status}" + (f" (error: {r.error})" if r.error else ""))
    if r.summary:
        lines.append(f"- summary: {r.summary}")
    if r.adjacent_skills:
        lines.append("- adjacent_skills:")
        for a in r.adjacent_skills[:10]:
            name = _item_label(a)
            url = _item_field(a, "url")
            rel = _item_field(a, "relation")
            suffix = f" ({rel}) {url}" if (rel or url) else ""
            lines.append(f"  - {name}{suffix}".rstrip())
    if r.existing_skills_overlap:
        lines.append("- existing_skills_overlap:")
        for o in r.existing_skills_overlap[:10]:
            lines.append(f"  - {o.name} sim={o.similarity:.2f} :: {o.differentiation_required}")
    if r.recommended_apis:
        lines.append("- recommended_apis:")
        for a in r.recommended_apis[:10]:
            name = _item_label(a)
            why = _item_field(a, "why")
            lines.append(f"  - {name}" + (f": {why}" if why else ""))
    if r.pitfalls:
        lines.append("- pitfalls:")
        for p in r.pitfalls[:10]:
            lines.append(f"  - {p}")
    # Routing samples (positive = user-authored, negative = structured suggestions)
    if brief.positive_samples or brief.negative_samples:
        lines.append("")
        lines.append("### Routing Samples")
        if brief.positive_samples:
            lines.append("- positive (should route HERE):")
            for q in brief.positive_samples[:12]:
                lines.append(f"  - {q}")
        if brief.negative_samples:
            lines.append("- negative (should route to a PEER, not here):")
            for ns in brief.negative_samples[:12]:
                peer = f" -> {ns.route_to_peer}" if ns.route_to_peer else ""
                why = f" ({ns.why_not_this})" if ns.why_not_this else ""
                lines.append(f"  - {ns.query}{peer}{why}")
    # Variables (env reuse/add + runtime)
    if brief.variables:
        lines.append("")
        lines.append("### Variables")
        for v in brief.variables:
            req = "required" if v.required else "optional"
            if v.kind == "aca_env":
                tag = "aca_env/exists" if v.in_aca else "aca_env/add"
            elif v.kind == "obo_token":
                tag = "obo_token/registered" if v.in_aca else "obo_token/add"
            elif v.kind == "platform_identity":
                tag = "platform_identity/injected"
            else:
                tag = f"runtime/{v.source}"
            desc = f" -- {v.description}" if v.description else ""
            ex = f" (e.g. {v.example})" if v.example else ""
            lines.append(f"- ({tag}) `{v.name}` ({req}){desc}{ex}")
            if v.kind == "runtime" and v.source == "credentials":
                lines.append(f"  - input data field: `{v.credentials_key or v.name}`; JSON member: `{v.payload_field or '(raw string)'}`")
    # Delegation (scenario skills; stands in for Variables)
    if brief.delegation:
        lines.append("")
        lines.append("### Delegation")
        for d in brief.delegation:
            lines.append(f"- child_skill: `{d.child_skill}`")
            if d.credentials_key:
                lines.append(f"  - credentials_key: `{d.credentials_key}`")
            for binding in d.input_bindings:
                lines.append(f"  - input_binding: {binding.model_dump_json()}")
            for cap in d.host_capabilities:
                lines.append(f"  - host_capability: {cap}")
            for hs in d.handshakes:
                lines.append(f"  - handshake: {hs}")
    # Verify checklist
    lines.append("")
    lines.append("### Verify Checklist")
    for key in _checklist_keys(session):
        mark = "x" if brief.verify_checklist.get(key) else " "
        lines.append(f"- [{mark}] {key}")
    return "\n".join(lines)


def _format_iteration_log(session: Session) -> str | None:
    if not session.patch_history and not session.iteration_reflections:
        return None
    lines = ["## Iteration Log", ""]
    if session.patch_history:
        lines.append("### Patch History")
        for p in session.patch_history:
            diff = (p.v4a_patch or "")
            if len(diff) > 800:
                diff = diff[:800] + "...(truncated for prompt; full diff preserved in session)"
            lines.append(f"- {p.applied_at} by {p.applied_by}: {p.reason or '(no reason)'}")
            if diff:
                lines.append("  ```diff")
                lines.append(diff)
                lines.append("  ```")
    if session.iteration_reflections:
        lines.append("")
        lines.append("### Reflections")
        for ref in session.iteration_reflections:
            lines.append(
                f"- {ref.created_at} (run {ref.test_run_id}) "
                f"delta={ref.confidence_delta:+.2f}"
            )
            for w in ref.what_went_wrong:
                lines.append(f"  - wrong: {w}")
            for c in ref.what_to_change:
                lines.append(f"  - change: {c}")
            if ref.raw:
                lines.append(f"  raw: {ref.raw[:400]}")
    return "\n".join(lines)


def _norm_fix_item(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def open_fix_items(session: Session) -> tuple[list[str], list[str]]:
    """Split the latest reflection's fix list into (closed, still open).

    A fix item is closed once an accepted patch applied AFTER that reflection
    named it in ``addresses``. Patches that carry no ``addresses`` close
    nothing, so an agent that skips the field is reminded rather than let off.
    Items the user skipped are in neither list, and a test run newer than the
    reflection makes the whole list stale.
    """
    if not session.iteration_reflections:
        return [], []
    reflection = session.iteration_reflections[-1]
    skipped = {_norm_fix_item(x) for x in reflection.skipped}
    items = [
        str(x) for x in reflection.what_to_change
        if str(x).strip() and _norm_fix_item(x) not in skipped
    ]
    if not items:
        return [], []
    since = _parse_iso(reflection.created_at)
    if session.test_runs and since is not None:
        latest_run = _parse_iso(session.test_runs[-1].ran_at)
        if latest_run is not None and latest_run > since:
            return [], []
    closed_keys: set[str] = set()
    for patch in session.patch_history:
        applied = _parse_iso(patch.applied_at)
        if since is not None and applied is not None and applied < since:
            continue
        closed_keys.update(_norm_fix_item(a) for a in patch.addresses)
    closed = [i for i in items if _norm_fix_item(i) in closed_keys]
    still_open = [i for i in items if _norm_fix_item(i) not in closed_keys]
    return closed, still_open


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _format_open_fixes(session: Session) -> str | None:
    closed, still_open = open_fix_items(session)
    if not closed and not still_open:
        return None
    skipped = [str(x) for x in session.iteration_reflections[-1].skipped]
    total = len(closed) + len(still_open) + len(skipped)
    lines = [
        "## Open Fix List",
        "",
        f"{len(still_open)} of {total} fix item(s) from the latest reflection are still open.",
        "",
    ]
    for item in closed:
        lines.append(f"- [x] {item}")
    for item in skipped:
        lines.append(f"- [-] {item} (skipped by the user -- do not raise it again)")
    for item in still_open:
        lines.append(f"- [ ] {item}")
    lines.append("")
    if still_open:
        lines.append(
            "The user's latest decision wins over this list. If they chose to fix only some "
            "items or none, do not push the rest: propose patches only for what they accepted, "
            "and tell them the 'Skip remaining fixes' button above the chat input closes the "
            "others. Otherwise work through the open items one at a time: propose ONE narrow "
            "`propose_patch` for the first open item, set its `addresses` to that item VERBATIM, "
            "and after it is accepted propose the next one. Prefer not to test between fixes -- "
            "each run costs a full router round-trip -- but when the user asks to test, test."
        )
    else:
        lines.append(
            "All items are closed. Ask the user (via `ask_user_input`) whether to re-run the "
            "skill-selection test now."
        )
    return "\n".join(lines)


def _format_skill_form(session: Session) -> str | None:
    has_code = any(MaterialKind(m.kind) is MaterialKind.CODE for m in session.materials)
    if SkillKind(session.skill_kind) is SkillKind.SCENARIO or not (has_code or session.skill_form == "script"):
        return None
    verdict = evaluate_skill_form(session)
    flag_failures = [failure for failure in verdict.failures if failure.check == "eaa_flags"]
    if flag_failures:
        # With the flags off nothing else can make it a script; the other failures are noise.
        note = SKILL_FORM_LOOKUP_FAILED_NOTE if flag_lookup_failed(session) else SKILL_FORM_FLAGS_OFF_NOTE
        return (
            f"## Skill Form\n\nform: inline\n{_form_check_line(flag_failures[0])} "
            f"{load_prompt(note)}"
        )
    lines = ["## Skill Form", "", f"form: {verdict.form}", f"locked: {str(verdict.locked).lower()}"]
    choice = [failure for failure in verdict.failures if failure.check == "user_choice"]
    if choice:
        # The user's choice decides the form; the other failures are noise.
        return "\n".join(lines + [_form_check_line(choice[0])])
    if verdict.failures:
        lines += ["", "Unmet script-form conditions:"]
        lines += [_form_check_line(failure) for failure in verdict.failures]
    replacement = script_replacement_problems(session)
    if replacement:
        lines += ["", "The code material did not replace the script; the previous script is kept:"]
        lines += [_form_check_line(failure) for failure in replacement]
    return "\n".join(lines)


def _form_check_line(failure: FormCheck) -> str:
    return f"- {failure.check}{f' ({failure.rule})' if failure.rule else ''}: {failure.message}"


def _form_prompt_key(session: Session) -> str | None:
    if session.skill_form == "script":
        return "script"
    if (
        session.skill_form is None
        and not session.prepare_brief.prefer_inline
        and SkillKind(session.skill_kind) is SkillKind.CAPABILITY
        and Mode(session.mode) is Mode.NEW
        and code_material_contents(session)
        and not script_flags_off(session.aca_env_result)
    ):
        return "script_candidate"
    return None


def _format_bundled_script(session: Session) -> str | None:
    script = session.current_skill.script
    if session.skill_form != "script" or script is None:
        return None
    heading = "## Bundled Script\n\nRead-only. Shipped next to SKILL.md; never edited in this session."
    for material in session.materials:
        if MaterialKind(material.kind) is MaterialKind.CODE and material.content == script:
            return f"{heading}\n\nIt is the code material `{material.id}` above, verbatim."
    return f"{heading}\n\n```python\n{script}\n```"


def _format_aca_environment(session: Session) -> str | None:
    if session.aca_env_error:
        return f"## Existing ACA Environment Variables\n\nLookup status: failed\n\nError: {session.aca_env_error}"
    if not session.aca_env_result:
        return None
    variables = [
        variable for variable in session.aca_env_result.get("variables", [])
        # Present on the ACA app but stripped from every skill's environment.
        if str(variable) not in PLATFORM_SECRET_DENYLIST
    ]
    obo_registry = obo_registry_mapping(session.aca_env_result)
    lines = [
        "## Existing ACA Environment Variables",
        "",
        f"revision: {session.aca_env_result.get('revision', '')}",
        "",
        "When confirming `variables_ok`, reconcile the variables into THREE kinds and persist with record_variables: kind='aca_env' for ACA environment variables (set in_aca=true when one of the existing ACA variables below already covers the purpose, false when it must still be added; in_aca does NOT change the SKILL.md text); kind='obo_token' for OBO token variables (set in_aca=true when it is already in the OBO token registry below); kind='runtime' for values that differ on every user query (resource id, file name, target URL, ...), documented as Required Inputs and emitting [NEEDS_INFO] when missing. aca_env and obo_token are written as SEPARATE SKILL.md sections; only runtime missing leads to [NEEDS_INFO]+exit 0, while aca_env/obo_token missing is a non-zero deployment error. Proactively prefill your best guesses, then ask the user to confirm or edit before marking `variables_ok`.",
    ]
    if variables:
        lines.append("")
        lines.append("Existing variables:")
        for variable in variables:
            lines.append(f"- `{variable}`")
    if obo_registry:
        lines.append("")
        lines.append("Existing OBO token variables:")
        for token_var, scope in obo_registry.items():
            lines.append(f"- `{token_var}`: {scope}")
    return "\n".join(lines)


# A prepared script is included whole or not at all. Slicing one breaks the
# declared-vs-read name comparison in prompts/04_test.md: the os.environ reads
# cluster near the end of the file, so a prefix shows the declarations with none
# of the reads and reads as a mismatch that is not there.
_TEST_CODE_BUDGET_CHARS = 45000
_TEST_CODE_MAX_SCRIPTS = 3


def _selected_target_skill(result: TestResult, *, positive: bool) -> bool:
    """Whether the runtime routed this sample to the skill under test.

    Derived from ``passed`` rather than re-matching names, so it cannot drift
    from the verdict the test runner already computed.
    """
    if result.passed is None:
        return False
    return result.passed if positive else not result.passed


def _format_prepared_code(results: list[TestResult]) -> list[str]:
    if not results:
        return []
    lines = [
        "",
        "### Prepared code",
        "",
        "The code the runtime prepared for the samples that routed to this skill.",
        "Nothing ran. Samples that routed elsewhere are omitted on purpose: that",
        "code belongs to another skill and is not evidence about this one.",
    ]
    by_body: dict[str, list[str]] = {}
    findings: dict[str, list[dict]] = {}
    order: list[tuple[str, str]] = []
    for result in results:
        key = sha256(result.apim_response.encode("utf-8")).hexdigest()
        if key in by_body:
            by_body[key].append(result.query)
            continue
        by_body[key] = [result.query]
        findings[key] = result.prepared_code_lint
        order.append((key, result.apim_response))
    budget = _TEST_CODE_BUDGET_CHARS
    shown = 0
    omitted: list[str] = []
    for key, body in order:
        if shown >= _TEST_CODE_MAX_SCRIPTS or len(body) > budget:
            omitted.extend(by_body[key])
            continue
        budget -= len(body)
        shown += 1
        lines.append("")
        lines.append(f"From: {'; '.join(by_body[key])}")
        lines.append(f"```python\n{body}\n```")
        lines.extend(_format_prepared_code_lint(findings.get(key) or []))
    if omitted:
        lines.append("")
        lines.append(
            "Omitted to stay within the context budget: "
            + "; ".join(omitted)
            + ". Do not review those from memory -- ask the user to check them in the UI."
        )
    return lines


def _format_needs_info_responses(results: list[TestResult]) -> list[str]:
    if not results:
        return []
    lines = [
        "",
        "### Needs-info responses",
        "",
        "For these samples the runtime found a required input missing from the query and",
        "answered with the `[NEEDS_INFO]` contract instead of preparing code. That is the",
        "correct handling of missing fields, not a finding. There is no code to review here.",
    ]
    for result in results:
        lines.append(f"- From: {result.query} -> `{needs_info_line(result.apim_response)}`")
    return lines


def _format_prepared_code_lint(issues: list[dict]) -> list[str]:
    """Static findings against the script above. Already decided -- do not re-derive them."""
    if not issues:
        return []
    lines = [
        "",
        "Static findings on that script (already checked -- read them, do not re-derive them):",
    ]
    for issue in sorted(issues, key=lambda i: str(i.get("severity", "")).lower() != "error"):
        rule = issue.get("rule", "?")
        severity = str(issue.get("severity") or "warning").lower()
        detail = issue.get("detail") or ""
        suffix = f" [{detail}]" if detail else ""
        lines.append(f"- [{severity}] {rule}{suffix}: {issue.get('message', '')}")
    return lines


def _format_requested_scripts(entries: list[dict]) -> list[str]:
    if not entries:
        return ["  requested_scripts: (none)"]
    lines = ["  requested_scripts:"]
    for entry in entries:
        verdict = "valid" if entry.get("valid") else "INVALID -- " + "; ".join(entry.get("problems") or [])
        args = json.dumps(entry.get("args"), ensure_ascii=False)
        lines.append(f"  - `{entry.get('skill', '')}` args {args}: {verdict}")
    return lines


def _format_latest_test_run(session: Session) -> str | None:
    if not session.test_runs:
        return None
    run = session.test_runs[-1]
    lines = [
        "## Latest Test Run",
        "",
        f"run_id: {run.run_id}",
        f"skill_version_hash: {run.skill_version_hash}",
        f"positive_hit_rate: {run.positive_hit_rate:.0%}",
        f"negative_correct_reject_rate: {run.negative_correct_reject_rate:.0%}",
    ]
    routed_here: list[TestResult] = []
    for label, results, positive in (
        ("Positive samples", run.positive_results, True),
        ("Negative samples", run.negative_results, False),
    ):
        if not results:
            continue
        lines.append("")
        lines.append(f"{label}:")
        for result in results:
            verdict = "PASS" if result.passed else "FAIL"
            lines.append(f"- [{verdict}] {result.query}")
            lines.append(
                f"  expected: {result.expected_skill or 'none'}; "
                f"actual: {result.actual_skill or 'none'}"
            )
            lines.append(
                f"  skills_referenced: {', '.join(result.skills_referenced) or '(none)'}"
            )
            if result.error:
                lines.append(f"  error: {result.error}")
            elif result.reasoning:
                lines.append(f"  {result.reasoning}")
            if session.skill_form == "script":
                lines.extend(_format_requested_scripts(result.requested_scripts))
            if result.apim_response and _selected_target_skill(result, positive=positive):
                routed_here.append(result)
    lines.extend(_format_prepared_code([r for r in routed_here if not is_needs_info_response(r.apim_response)]))
    lines.extend(_format_needs_info_responses([r for r in routed_here if is_needs_info_response(r.apim_response)]))
    return "\n".join(lines)


def _format_remote_skill_state(session: Session) -> str:
    remote_name = session.remote_skill_id or session.target_skill_id or ""
    local_version = session.current_skill.version_hash or ""
    remote_version = session.remote_version_hash or ""
    if not remote_name:
        binding = "NEW"
        status = "not saved to Blob"
    elif remote_version and local_version and remote_version != local_version:
        binding = remote_name
        status = "UNSAVED local changes exist"
    elif remote_version:
        binding = remote_name
        status = "saved to Blob"
    else:
        binding = remote_name
        status = "remote version unknown"
    return "\n".join(
        [
            "## Blob Skill Binding",
            "",
            f"blob_skill: {binding}",
            f"status: {status}",
            f"remote_version_hash: {remote_version or 'none'}",
            f"local_version_hash: {local_version or 'none'}",
            "",
            "Selection tests validate the saved Blob/runtime skill. If status is UNSAVED, tell the user to save before interpreting tests as the current local files.",
        ]
    )


DONE_REENTRY_GUIDANCE = """\
## DONE Re-entry Routing

The session was previously finalized but the user has sent a new message. Classify
the intent into exactly ONE bucket and emit `request_stage_transition`:

- A) Small fix / wording / one section wrong -> `target_stage="refine"`
- B) New feature / change of direction / scope rethink -> `target_stage="prepare"` (the brief will be preserved, revisit flag will be set)
- C) Run the tests / validate -> `target_stage="test"`
- D) Ambiguous -> DO NOT transition; call `ask_user_input` with the three options above.
"""


PEER_CROSS_LAYER_CAPABILITY_NOTE = (
    "These declare `metadata.children`, so they are scenario-orchestration parents "
    "and are never materialized into the backend candidate set a capability skill "
    "is routed from. They are NOT rivals and cannot be reached from here: do not "
    "compare boundaries against them, and never write a \"use <skill> instead\" line "
    "pointing at one. They are listed only so you recognise the names."
)

PEER_CROSS_LAYER_SCENARIO_NOTE = (
    "These are internal children of some scenario, so they are never projected into "
    "the host directory a scenario skill is selected from. They are NOT rivals and "
    "cannot be reached from here: do not compare boundaries against them, and never "
    "write a \"use <skill> instead\" line pointing at one. They are listed only so you "
    "recognise the names."
)

_PEER_RENDER_CAP = 30


def _peer_entry_lines(peer: dict) -> list[str]:
    name = str(peer.get("name", "")).strip() or "(unnamed)"
    desc = str(peer.get("description", "")).strip()
    wtu = str(peer.get("when_to_use", "")).strip()
    wnot = str(peer.get("when_not_to_use", "")).strip()
    lines = [f"### {name}"]
    if desc:
        lines.append(f"- description: {desc}")
    if wtu:
        lines.append(f"- when_to_use: {wtu.splitlines()[0][:300]}")
        extra = " ".join(s.strip() for s in wtu.splitlines()[1:] if s.strip())
        if extra:
            lines.append(f"  {extra[:400]}")
    if wnot:
        lines.append(f"- when_not_to_use: {wnot[:300]}")
    lines.append("")
    return lines


def _partition_peers_by_layer(
    session: Session, peers: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Split peers into (routing rivals, cross-layer bystanders).

    A capability skill competes in the backend candidate set, which excludes
    parents; a scenario skill competes in the host directory, which excludes
    internal children. Peers loaded before these signals existed carry neither
    key and stay rivals, which is the pre-split behaviour.
    """
    if SkillKind(session.skill_kind) is SkillKind.SCENARIO:
        # `is_internal` is only stamped once some parent claims the child, so a
        # child that is merely named by a peer is cross-layer too.
        claimed = {
            str(c).strip()
            for peer in peers
            for c in (peer.get("children") or [])
            if str(c).strip()
        }

        def is_cross_layer(peer: dict) -> bool:
            return bool(peer.get("is_internal")) or str(peer.get("name", "")).strip() in claimed

    else:

        def is_cross_layer(peer: dict) -> bool:
            return bool(peer.get("children"))

    rivals = [p for p in peers if not is_cross_layer(p)]
    cross = [p for p in peers if is_cross_layer(p)]
    return rivals, cross


def _format_peer_skills(session: Session) -> str | None:
    """Render the boundary digest of the user's accessible skills.

    This is the source material for (a) differentiation, (b) the new skill's
    When NOT to Use router, and (c) old-skill modification suggestions.
    """
    research = session.prepare_brief.research
    peers = getattr(research, "peer_skills", None) or []
    status = getattr(research, "peer_skills_status", "skipped")
    error = (getattr(research, "peer_skills_error", "") or "").strip()
    # Filtering here rather than at load time keeps this correct no matter when
    # the children were declared relative to the PREPARE-entry peer load.
    children = {str(c).strip() for c in (getattr(session, "children", None) or []) if str(c).strip()}
    delegated = sorted(n for n in (str(p.get("name", "")).strip() for p in peers) if n in children)
    peers = [p for p in peers if str(p.get("name", "")).strip() not in children]
    delegated_note = (
        "Excluded as delegated children of this scenario, NOT routing peers -- never "
        f"write a \"use <skill> instead\" line pointing at one: {', '.join(delegated)}."
        if delegated
        else ""
    )
    if not peers:
        if error:
            return (
                "## Peer Skills (your accessible skills)\n\n"
                f"LOAD FAILED (status: {status}) -- {error}\n\n"
                "This is an INFRASTRUCTURE failure, not evidence that the user has no "
                "peer skills. Do NOT tell the user there are no neighbouring skills and "
                "do NOT confirm `routing_uniqueness_confirmed` on that basis. Report the "
                "error above verbatim, tell them to fix access and press 'Refresh peer "
                "skills', and meanwhile ask them to name any similar existing skill."
            )
        if status in {"failed", "skipped"}:
            return None
        empty = (
            "## Peer Skills (your accessible skills)\n\n"
            "(none -- you currently have no other granted skills)"
        )
        return f"{empty}\n\n{delegated_note}" if delegated_note else empty
    rivals, cross_layer = _partition_peers_by_layer(session, peers)
    lines = [
        "## Peer Skills (your accessible skills)",
        "",
        "These are the OTHER skills this user can already route to (granted in SQL",
        "AND present in Blob). Use them to: (1) write `differentiation` naming any",
        "overlapping skill; (2) build a routing section in the new skill's",
        "`## When NOT to Use This Skill` -- for each adjacent/confusable need, name",
        "the peer skill to use instead; (3) suggest concrete edits to the affected",
        "old skills in your chat message when the new skill shifts their boundary.",
        f"(status: {status}; {len(rivals)} rival(s), {len(cross_layer)} cross-layer)",
        "",
    ]
    if delegated_note:
        lines.extend([delegated_note, ""])
    elif SkillKind(session.skill_kind) is SkillKind.SCENARIO and not children:
        lines.extend([
            "No child is declared yet, so nothing has been excluded from this list: it may "
            "still contain the skills this scenario will delegate to. Establish the children "
            "before comparing against it.",
            "",
        ])
    shown = rivals[:_PEER_RENDER_CAP]
    for peer in shown:
        lines.extend(_peer_entry_lines(peer))
    if cross_layer:
        lines.extend([
            "## Cross-layer skills (NOT routing rivals)",
            "",
            PEER_CROSS_LAYER_SCENARIO_NOTE
            if SkillKind(session.skill_kind) is SkillKind.SCENARIO
            else PEER_CROSS_LAYER_CAPABILITY_NOTE,
            "",
        ])
        for peer in cross_layer[: max(0, _PEER_RENDER_CAP - len(shown))]:
            lines.extend(_peer_entry_lines(peer))
    if error:
        lines.append(f"INCOMPLETE -- {error}")
    return "\n".join(lines).rstrip()


def _format_selected_neighbor_skills(session: Session) -> str | None:
    """Render the FULL current SKILL.md of the selected neighbor skills so the
    agent can edit them with a V4A patch without asking the user for the text.
    Populated best-effort onto session.neighbor_full_md before the turn."""
    full = getattr(session, "neighbor_full_md", None) or {}
    if not full:
        return None
    lines = [
        "## Neighbor Skills (full SKILL.md for editing)",
        "",
        "These are the COMPLETE current SKILL.md files of the selected neighbor "
        "skills (the exact text any edit will be applied to). When you call "
        "propose_neighbor_edit, copy the unchanged CONTEXT lines of your V4A patch "
        "VERBATIM from between the markers below -- do NOT ask the user for these "
        "files. The text is NOT fenced (these files contain their own code "
        "blocks), so everything between <<<BEGIN ...>>> and <<<END ...>>> is the "
        "literal file content.",
    ]
    for name, md in full.items():
        lines.append("")
        lines.append(f"### {name}")
        lines.append(f"<<<BEGIN SKILL.md: {name}>>>")
        lines.append(md)
        lines.append(f"<<<END SKILL.md: {name}>>>")
    return "\n".join(lines)


def _format_child_skills(session: Session) -> str | None:
    """Render the FULL current SKILL.md of each declared child capability skill.

    The scenario body's payload contract, its failure branches and its child
    edits are all derived from these files, so the agent must never have to ask
    the user for them. Populated best-effort onto session.child_full_md before
    the turn."""
    full = getattr(session, "child_full_md", None) or {}
    if not full:
        return None
    lines = [
        "## Child Skills (full SKILL.md)",
        "",
        "These are the COMPLETE current SKILL.md files of the capability skills "
        "this scenario skill delegates to. Read them to write CORRECT POINTERS "
        "and correct return branches -- the exact `##` heading names to fetch, "
        "every `[NEEDS_INFO] missing=` code, and every business error key. Do "
        "NOT copy their field tables or payload examples into the scenario "
        "body; point at the section instead. When you call propose_child_edit, "
        "copy the unchanged CONTEXT lines of your V4A patch VERBATIM from "
        "between the markers below. The text is NOT fenced (these files contain "
        "their own code blocks), so everything between <<<BEGIN ...>>> and "
        "<<<END ...>>> is the literal file content.",
    ]
    for name, md in full.items():
        lines.append("")
        lines.append(f"### {name}")
        lines.append(f"<<<BEGIN SKILL.md: {name}>>>")
        lines.append(md)
        lines.append(f"<<<END SKILL.md: {name}>>>")
    return "\n".join(lines)


def _format_child_sections(session: Session) -> str | None:
    """Render the ``##`` heading index of every declared child.

    A pointer can name a section of any whitelisted skill, but only a few of
    them have their full body injected above. Without this index the agent has
    to guess heading names, and a guessed name is a hard error at save time.
    """
    index = getattr(session, "child_sections", None) or {}
    if not index:
        return None
    lines = [
        "## Dependency Skills (## section index)",
        "",
        "Every skill declared in `metadata.children`, with the `##` headings it "
        "publishes. These names are the ONLY values accepted in "
        "`fetch_skill(sections=\"...\")`; `sections` is a comma-separated STRING, "
        "not a list. Decorative emoji and punctuation may be omitted, but no "
        "word may be invented. Fetch each skill once and name every section you "
        "need in that single call.",
    ]
    for name, titles in index.items():
        rendered = "、".join(f"`{t}`" for t in titles) or "(no `##` sections)"
        lines.append("")
        lines.append(f"- **{name}**: {rendered}")
    return "\n".join(lines)


_MATERIAL_TIERS: tuple[tuple[str, frozenset[MaterialKind], str], ...] = (
    (
        "Tier 1 -- REPRODUCE, DO NOT PARAPHRASE",
        VERBATIM_KINDS,
        "This is the user's own working implementation and it is authoritative. When a "
        "Tier 1 material covers an operation this skill performs, the "
        "`## API Reference / Sample Code` block MUST be derived from that file rather "
        "than written afresh. Preserve its guard clauses, the ORDER of its security "
        "gates, its error taxonomy, its helper functions and its output language. You "
        "may only (a) rename identifiers so they match the declared variables, "
        "(b) delete code unrelated to this skill, and (c) add the `[NEEDS_INFO]` "
        "contract if it is absent. Any other deviation must be stated explicitly in "
        "your `text` field with a reason. NEVER invent a SQL object, stored procedure, "
        "table, column, endpoint or payload field that does not appear in this file.",
    ),
    (
        "Tier 2 -- IDENTIFIERS ARE AUTHORITATIVE",
        NO_INVENTION_KINDS,
        "Endpoint paths, method names, parameter names and response field names must "
        "be taken from here verbatim. The surrounding code may be written fresh.",
    ),
    (
        "Tier 3 -- CONTEXT ONLY",
        CONTEXT_ONLY_KINDS,
        "Background. Use it for scope, prose and the routing description only. It is "
        "NEVER a source of API or code detail, and its wording must not be copied into "
        "the skill body.",
    ),
)


def _format_materials(session: Session) -> str | None:
    """Render the user's materials in full, grouped by the fidelity tier they carry.

    Neighbour and child skills already get this treatment; materials used to get
    only a count, which is why pasted reference code was routinely rewritten.
    """
    prepared = materials_for_prompt(getattr(session, "materials", None))
    if not prepared:
        return None
    lines = [
        "## Materials",
        "",
        f"{len(prepared)} material(s) supplied by the user. Everything between "
        "<<<BEGIN MATERIAL ...>>> and <<<END MATERIAL ...>>> is literal content and is "
        "NOT fenced (materials may contain their own code blocks). The "
        "'never copy verbatim' rule in the stage instructions covers ROUTING TEST "
        "SAMPLES only -- it must never be applied to materials.",
    ]
    for title, kinds, policy in _MATERIAL_TIERS:
        group = [(material, text) for material, text, _ in prepared if material.kind in kinds]
        if not group:
            continue
        lines += ["", f"### {title}", "", policy]
        for material, text in group:
            origin = ", origin=agent_patch, not run by the user" if material.origin == "agent_patch" else ""
            lines += [
                "",
                f"<<<BEGIN MATERIAL {material.id} (kind={material.kind.value}{origin})>>>",
                text,
                f"<<<END MATERIAL {material.id}>>>",
            ]
    return "\n".join(lines)


def _format_skill_assets(session: Session) -> str | None:
    """Every attached asset in full; never truncated, since the skill ships them verbatim."""
    if not session.assets:
        return None
    lines = [
        "## Skill Assets",
        "",
        f"{len(session.assets)} file(s) uploaded by the user, shipped verbatim with this skill. "
        "Read-only: only the user can replace or remove them. Everything between "
        "<<<BEGIN ASSET ...>>> and <<<END ASSET ...>>> is literal file content.",
    ]
    for asset in session.assets:
        lines += [
            "",
            f"<<<BEGIN ASSET {asset.path} ({asset.size} chars)>>>",
            asset.content,
            f"<<<END ASSET {asset.path}>>>",
        ]
    return "\n".join(lines)


def build_system_prompt(session: Session) -> str:
    stage = Stage(session.current_stage)
    mode = Mode(session.mode)
    kind = SkillKind(session.skill_kind)
    parts: list[str] = [
        load_prompt("00_global_system.md"),
        load_prompt(stage_prompt_for(kind, stage)),
    ]
    addendum = KIND_STAGE_ADDENDA.get(kind, {}).get(stage)
    if addendum:
        parts.append(load_prompt(addendum))
    form_key = _form_prompt_key(session)
    for filename in FORM_STAGE_ADDENDA.get(form_key or "", {}).get(stage, ()):
        parts.append(load_prompt(filename))
    if session.assets and stage in ASSET_STAGE_ADDENDA:
        parts.append(load_prompt(ASSET_STAGE_ADDENDA[stage]))
    runtime_state = f"## Runtime State\n\nmode: {mode.value}\nstage: {stage.value}\nskill_kind: {kind.value}"
    if kind is SkillKind.SCENARIO:
        declared = [str(c).strip() for c in (getattr(session, "children", None) or []) if str(c).strip()]
        runtime_state += "\nchildren: " + (
            ", ".join(declared)
            if declared
            else "(none declared yet -- routing_uniqueness_confirmed cannot be confirmed until they are)"
        )
    parts += [
        load_prompt("09_best_practices.md"),
        load_prompt(KIND_FORMAT_SPEC[kind]),
        runtime_state,
        _format_remote_skill_state(session),
    ]
    if stage == Stage.DONE:
        parts.append(DONE_REENTRY_GUIDANCE)
    if kind is SkillKind.SCENARIO:
        parts.append(SCENARIO_KIND_ADDENDUM)
    if mode == Mode.IMPORT:
        parts.append(IMPORT_MODE_ADDENDUM)
    if mode == Mode.MODIFY:
        parts.append(MODIFY_MODE_ADDENDUM)
    if session.materials:
        materials_section = _format_materials(session)
        if materials_section:
            parts.append(materials_section)
    assets_section = _format_skill_assets(session)
    if assets_section:
        parts.append(assets_section)
    brief_section = _format_prepare_brief(session)
    if brief_section:
        parts.append(brief_section)
    form_section = _format_skill_form(session)
    if form_section:
        parts.append(form_section)
    if stage in {Stage.PREPARE, Stage.DRAFT, Stage.REFINE}:
        peer_section = _format_peer_skills(session)
        if peer_section:
            parts.append(peer_section)
        neighbor_full_section = _format_selected_neighbor_skills(session)
        if neighbor_full_section:
            parts.append(neighbor_full_section)
    if kind is SkillKind.SCENARIO and stage in {Stage.PREPARE, Stage.DRAFT, Stage.REFINE, Stage.TEST}:
        child_section = _format_child_skills(session)
        if child_section:
            parts.append(child_section)
        sections_index = _format_child_sections(session)
        if sections_index:
            parts.append(sections_index)
    iteration_section = _format_iteration_log(session)
    if iteration_section:
        parts.append(iteration_section)
    if stage in {Stage.REFINE, Stage.TEST}:
        open_fix_section = _format_open_fixes(session)
        if open_fix_section:
            parts.append(open_fix_section)
    if session.research_summary:
        parts.append(f"## Research Summary\n\n{session.research_summary}")
    aca_env_section = _format_aca_environment(session)
    if aca_env_section and stage in {Stage.PREPARE, Stage.DRAFT, Stage.REFINE, Stage.TEST, Stage.DONE}:
        parts.append(aca_env_section)
    test_run_section = _format_latest_test_run(session)
    if test_run_section and stage in {Stage.REFINE, Stage.TEST, Stage.DONE}:
        parts.append(test_run_section)
    if session.current_skill.skill_md:
        parts.append(
            "## Current Draft\n\n"
            f"version_hash: {session.current_skill.version_hash}\n\n"
            f"```markdown\n{session.current_skill.skill_md}\n```"
        )
    bundled_script = _format_bundled_script(session)
    if bundled_script:
        parts.append(bundled_script)
    # Keep transition guidance last so the Agent ends the system message with
    # the exact outgoing edges it may choose from for this stage.
    parts.append(load_prompt("12_input_sources.md"))
    parts.append(_format_allowed_exits(stage))
    return "\n\n".join(p for p in parts if p.strip())
