"""Mechanical inline <-> script move for a MODIFY session.

Only the code moves; nothing is rewritten. Whatever the new form still
requires shows up in the normal lint and `## Skill Form` conditions for the
agent and the user to finish.
"""

from __future__ import annotations

import ast
import re
from typing import Literal

from .models import ChatMessage, Material, MaterialKind, MessageRole, Mode, Session, SkillKind
from .sections import h2_sections, normalize_section
from .skill_lint import GATEKEEPER_ADDENDUM_SECTION, body_python_fences

SAMPLE_CODE_SECTION = "API Reference / Sample Code"

FORM_CONVERTED_MESSAGE = (
    "The user converted this skill from {old} to {new} form. Only the code moved: {moved} "
    "SKILL.md was not rewritten, so its sections and wording may still describe the {old} form. "
    "Read `## Skill Form` and the lint results, then propose patches that bring SKILL.md in line "
    "with the {new} form. Tell the user plainly what still has to change."
)


class FormConversionError(ValueError):
    """The skill cannot be converted; the message says why."""


def _h2_line(text: str, title: str) -> re.Match[str] | None:
    return re.search(rf"^##[ \t]+{re.escape(title)}[ \t]*$", text, re.MULTILINE)


def inline_to_script(skill_md: str) -> tuple[str, str]:
    """``(SKILL.md without its sample block, script)`` for a skill with exactly one Python block."""
    text = skill_md.replace("\r\n", "\n")
    fences = body_python_fences(text)
    if len(fences) != 1:
        raise FormConversionError(
            f"SKILL.md has {len(fences)} Python code blocks; exactly one is needed to become the "
            "bundled script."
        )
    fence, code = fences[0]
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise FormConversionError(
            f"The Python code block is not valid Python: {exc.msg} (line {exc.lineno})."
        ) from exc
    start = text.find(fence)
    if start < 0:
        raise FormConversionError("The Python code block could not be located in SKILL.md.")
    head = text[:start].rstrip("\n")
    tail = text[start + len(fence):].lstrip("\n")
    return f"{head}\n\n{tail}" if tail else f"{head}\n", code


def script_to_inline(skill_md: str, script: str) -> str:
    """SKILL.md with ``script`` as the Python block of its sample-code section."""
    text = skill_md.replace("\r\n", "\n")
    block = f"```python\n{script.rstrip(chr(10))}\n```"
    wanted = normalize_section(SAMPLE_CODE_SECTION)
    title = next((t for t, _ in h2_sections(text) if wanted in normalize_section(t)), None)
    heading = _h2_line(text, title) if title else None
    if heading:
        rest = text[heading.end():].lstrip("\n")
        return f"{text[:heading.end()]}\n\n{block}\n\n{rest}" if rest else f"{text[:heading.end()]}\n\n{block}\n"
    addendum = next(
        (t for t, _ in h2_sections(text) if normalize_section(GATEKEEPER_ADDENDUM_SECTION) in normalize_section(t)),
        None,
    )
    at = _h2_line(text, addendum) if addendum else None
    section = f"## {SAMPLE_CODE_SECTION}\n\n{block}\n"
    if at:
        return f"{text[:at.start()].rstrip(chr(10))}\n\n{section}\n{text[at.start():]}"
    return f"{text.rstrip(chr(10))}\n\n{section}"


def convert_session_form(session: Session, target: Literal["inline", "script"]) -> None:
    """Switch a MODIFY session's form in place; raises ``FormConversionError`` and changes nothing."""
    if Mode(session.mode) is not Mode.MODIFY:
        raise FormConversionError("Only a modify session can convert the skill form.")
    if SkillKind(session.skill_kind) is not SkillKind.CAPABILITY:
        raise FormConversionError("A scenario skill has no script of its own.")
    if session.skill_form == target:
        raise FormConversionError(f"The skill is already in {target} form.")
    if session.patch_history:
        raise FormConversionError(
            "This session already has edits. Save or discard them first, then convert the form."
        )
    draft = session.current_skill
    if target == "script":
        if session.assets:
            raise FormConversionError(
                f"{len(session.assets)} skill asset(s) are attached; a script skill ships only "
                "SKILL.md and its script."
            )
        draft.skill_md, draft.script = inline_to_script(draft.skill_md)
        session.script_removed = False
        # The agent adapts the script's edges through this material; see script_edge_material().
        has_code_material = any(MaterialKind(m.kind) is MaterialKind.CODE for m in session.materials)
        if not has_code_material:
            session.materials.append(Material(kind=MaterialKind.CODE, content=draft.script))
        moved = (
            "the Python code block is now the bundled script"
            + ("." if has_code_material else ", and a code material identical to it was added so you "
               "can adapt its edges with `propose_material_patch`.")
        )
    else:
        if draft.script is None:
            raise FormConversionError("This skill has no script to move into SKILL.md.")
        draft.skill_md = script_to_inline(draft.skill_md, draft.script)
        session.materials = [
            m for m in session.materials
            if not (MaterialKind(m.kind) is MaterialKind.CODE and m.content == draft.script)
        ]
        draft.script = None
        session.script_removed = True
        moved = "the bundled script is now a Python code block in SKILL.md."
    old = "inline" if target == "script" else "script"
    session.skill_form = target
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=FORM_CONVERTED_MESSAGE.format(old=old, new=target, moved=moved),
        )
    )
    session.touch()
