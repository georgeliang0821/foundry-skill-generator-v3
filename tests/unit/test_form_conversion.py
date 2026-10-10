from __future__ import annotations

import pytest

from backend.blob_store import LocalSkillStore
from backend.form_conversion import FormConversionError, convert_session_form, inline_to_script, script_to_inline
from backend.models import Mode, PatchRecord, Session, SkillAsset, SkillFiles, SkillKind

CODE = 'import json\nprint(json.dumps({"status": "ok"}))\n'
INLINE_MD = f"---\nname: demo\n---\n\n## Overview\n\nBody.\n\n## API Reference / Sample Code\n\n```python\n{CODE}```\n\n## Prerequisites\n\nNone.\n"


def _session(**kwargs) -> Session:
    session = Session(mode=Mode.MODIFY, skill_form=kwargs.pop("form", "inline"), **kwargs)
    session.current_skill.skill_md = INLINE_MD
    return session


def test_inline_to_script_moves_the_single_block() -> None:
    md, script = inline_to_script(INLINE_MD)

    assert script == CODE
    assert "```" not in md
    assert "## API Reference / Sample Code\n\n## Prerequisites" in md


@pytest.mark.parametrize(
    "md, fragment",
    [
        ("---\nname: demo\n---\n\n## Overview\n\nNo code.\n", "0 Python"),
        (INLINE_MD + "\n```python\nprint(2)\n```\n", "2 Python"),
        ("---\nname: demo\n---\n\n```python\ndef broken(:\n```\n", "not valid Python"),
    ],
)
def test_inline_to_script_refuses_anything_but_one_valid_block(md: str, fragment: str) -> None:
    with pytest.raises(FormConversionError, match=fragment):
        inline_to_script(md)


def test_gatekeeper_addendum_blocks_are_not_counted() -> None:
    md = INLINE_MD + "\n## Gatekeeper Addendum\n\n```python\nprint('rule')\n```\n"

    new_md, script = inline_to_script(md)

    assert script == CODE
    assert "print('rule')" in new_md


def test_script_to_inline_fills_the_sample_code_section() -> None:
    md_without_block, _ = inline_to_script(INLINE_MD)

    assert script_to_inline(md_without_block, CODE) == INLINE_MD


def test_script_to_inline_adds_the_section_before_the_gatekeeper_addendum() -> None:
    md = "---\nname: demo\n---\n\n## Overview\n\nBody.\n\n## Gatekeeper Addendum\n\nLearned.\n"

    out = script_to_inline(md, CODE)

    assert out.index("## API Reference / Sample Code") < out.index("## Gatekeeper Addendum")
    assert f"```python\n{CODE.rstrip()}\n```" in out


def test_convert_inline_session_to_script() -> None:
    session = _session()

    convert_session_form(session, "script")

    assert (session.skill_form, session.current_skill.script) == ("script", CODE)
    assert "```" not in session.current_skill.skill_md
    assert session.conversation[-1].role == "system" and "inline to script" in session.conversation[-1].content


def test_convert_script_session_to_inline_marks_the_script_for_removal() -> None:
    session = _session(form="script")
    session.current_skill.skill_md, _ = inline_to_script(INLINE_MD)
    session.current_skill.script = CODE

    convert_session_form(session, "inline")

    assert (session.skill_form, session.current_skill.script, session.script_removed) == ("inline", None, True)
    assert CODE.strip() in session.current_skill.skill_md


@pytest.mark.parametrize(
    "mutate, target, fragment",
    [
        (lambda s: setattr(s, "mode", Mode.NEW), "script", "modify session"),
        (lambda s: setattr(s, "skill_kind", SkillKind.SCENARIO), "script", "scenario"),
        (lambda s: None, "inline", "already"),
        (lambda s: s.patch_history.append(PatchRecord(target_file="SKILL.md", v4a_patch="x")), "script", "edits"),
        (lambda s: s.assets.append(SkillAsset(path="assets/a.txt", content="x")), "script", "asset"),
    ],
)
def test_convert_refusals_change_nothing(mutate, target: str, fragment: str) -> None:
    session = _session()
    mutate(session)

    with pytest.raises(FormConversionError, match=fragment):
        convert_session_form(session, target)

    assert (session.current_skill.skill_md, session.current_skill.script) == (INLINE_MD, None)


def test_script_to_inline_needs_a_script() -> None:
    session = _session(form="script")

    with pytest.raises(FormConversionError, match="no script"):
        convert_session_form(session, "inline")


def test_local_store_removes_the_script_on_request() -> None:
    store = LocalSkillStore()
    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\n", script=CODE))

    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\nEdited.\n", remove_script=True))

    assert not store.has_script("demo")
