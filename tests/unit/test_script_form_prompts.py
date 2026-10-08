from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from backend import state_machine
from backend.models import Material, MaterialKind, Session, Stage, TestResult as RunResult, TestRun as Run
from backend.state_machine import (
    FORM_STAGE_ADDENDA,
    SKILL_FORM_FLAGS_OFF_NOTE,
    SKILL_FORM_LOOKUP_FAILED_NOTE,
    build_system_prompt,
    load_prompt,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "skills" / "ms-graph-room-finder"
MD = (FIXTURE / "SKILL.md").read_text(encoding="utf-8")
PY = (FIXTURE / "scripts" / "ms-graph-room-finder.py").read_text(encoding="utf-8")
FLAGS_ON = {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "true"}}
FLAGS_OFF = {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "false"}}
# sha256 of each inline prompt, captured before Phase 3 added any script-form prompt text.
BASELINE = json.loads((Path(__file__).parent / "inline_prompt_baseline.json").read_text(encoding="utf-8"))


def _material(material_id: str, kind: MaterialKind, content: str) -> Material:
    return Material(id=material_id, kind=kind, content=content, created_at="2026-09-29T00:00:00+00:00")


TEXT = _material("text-1", MaterialKind.TEXT, "Rooms are booked through ms-graph-calendar.")
SPEC = _material("spec-1", MaterialKind.API_SPEC, "GET /places/microsoft.graph.room")


def _session(stage: Stage, *, mode: str = "new", kind: str = "capability", aca=FLAGS_ON, materials=None, form=None) -> Session:
    session = Session(
        id="golden",
        mode=mode,
        skill_kind=kind,
        current_stage=stage,
        aca_env_result=aca,
        materials=[TEXT, SPEC] if materials is None else materials,
        skill_form=form,
    )
    if stage is not Stage.PREPARE:
        session.current_skill.skill_md = MD
        session.current_skill.version_hash = "golden-hash"
    return session


def inline_cases() -> dict[str, Session]:
    """Sessions with no code material: Phase 3 must leave their prompts byte-identical."""
    cases = {f"new-{stage.value}": _session(stage, form=None if stage is Stage.PREPARE else "inline") for stage in Stage}
    cases["new-prepare-no-aca"] = _session(Stage.PREPARE, aca=None)
    cases["modify-refine"] = _session(Stage.REFINE, mode="modify", form="inline")
    cases["import-prepare"] = _session(Stage.PREPARE, mode="import")
    cases["scenario-prepare"] = _session(Stage.PREPARE, kind="scenario", aca=None)
    return cases


def flags_off_code_session() -> Session:
    return _session(Stage.PREPARE, aca=FLAGS_OFF, materials=[TEXT, _material("code-1", MaterialKind.CODE, PY)])


def prompt_without_skill_form(session: Session) -> str:
    original = state_machine._format_skill_form
    state_machine._format_skill_form = lambda _session: None
    try:
        return build_system_prompt(session)
    finally:
        state_machine._format_skill_form = original


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capture_baseline() -> dict[str, str]:
    baseline = {name: _sha(build_system_prompt(session)) for name, session in inline_cases().items()}
    baseline["flags-off-code-prepare-without-skill-form"] = _sha(prompt_without_skill_form(flags_off_code_session()))
    return baseline


# --- inline sessions are untouched -------------------------------------------


@pytest.mark.parametrize("name", sorted(inline_cases()))
def test_an_inline_session_without_code_keeps_its_prompt(name) -> None:
    assert _sha(build_system_prompt(inline_cases()[name])) == BASELINE[name]


SCRIPT_FILES = sorted({name for stages in FORM_STAGE_ADDENDA.values() for files in stages.values() for name in files})
CODE = _material("code-1", MaterialKind.CODE, PY)


def _script_instructions(prompt: str) -> list[str]:
    return [name for name in SCRIPT_FILES if load_prompt(name) in prompt]


def test_flags_off_with_code_adds_only_the_flag_line() -> None:
    session = flags_off_code_session()
    prompt = build_system_prompt(session)
    block = state_machine._format_skill_form(session)

    *head, flag_line = block.split("\n")
    assert head == ["## Skill Form", "", "form: inline"]
    assert flag_line.startswith("- eaa_flags: ") and flag_line.endswith(load_prompt(SKILL_FORM_FLAGS_OFF_NOTE))
    assert _sha(prompt.replace(block + "\n\n", "", 1)) == BASELINE["flags-off-code-prepare-without-skill-form"]
    assert _script_instructions(prompt) == [] and "## Bundled Script" not in prompt


# --- script form carries the script instructions ------------------------------


def _script_session(stage: Stage, **overrides) -> Session:
    session = _session(stage, form="script", **{"materials": [TEXT, CODE], **overrides})
    session.current_skill.script = PY
    return session


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (_session(Stage.PREPARE, materials=[TEXT, CODE]), ["01_prepare_script_addendum.md"]),
        (_script_session(Stage.PREPARE), []),
        (_script_session(Stage.DRAFT), ["02_draft_script_addendum.md", "13_script_save.md"]),
        (_script_session(Stage.REFINE), ["02_draft_script_addendum.md", "13_script_save.md"]),
        (_script_session(Stage.TEST), ["04_test_script_addendum.md", "13_script_save.md"]),
        (_script_session(Stage.DONE), ["13_script_save.md"]),
        (_script_session(Stage.REFINE, mode="modify"), ["02_draft_script_addendum.md", "13_script_save.md"]),
    ],
    ids=["prepare-candidate", "prepare-locked", "draft", "refine", "test", "done", "modify-refine"],
)
def test_script_instructions_follow_the_form_and_stage(session, expected) -> None:
    assert _script_instructions(build_system_prompt(session)) == expected


@pytest.mark.parametrize(
    "session",
    [
        _session(Stage.PREPARE, mode="import", materials=[TEXT, CODE]),
        _session(Stage.PREPARE, form="inline", materials=[TEXT, CODE]),
        _session(Stage.REFINE, mode="modify", form="inline", materials=[TEXT, CODE]),
        _session(Stage.REFINE, form="inline", aca=FLAGS_OFF, materials=[TEXT, CODE]),
        _session(Stage.PREPARE, kind="scenario", materials=[TEXT, CODE]),
    ],
    ids=["import", "locked-inline-prepare", "modify-inline", "locked-inline-flags-off", "scenario"],
)
def test_code_without_a_script_form_gets_no_script_instructions(session) -> None:
    prompt = build_system_prompt(session)

    assert _script_instructions(prompt) == [] and "## Bundled Script" not in prompt


def test_the_script_section_set_is_the_golden_fixture() -> None:
    fixture = [heading for heading in re.findall(r"^## (.+)$", MD, re.M) if heading != "When to Use This Skill"]

    assert re.findall(r"^\d+\. `## ([^`]+)`", load_prompt("02_draft_script_addendum.md"), re.M) == fixture


@pytest.mark.parametrize(
    ("filename", "phrases"),
    [
        ("01_prepare_script_addendum.md", [
            "`record_variables(script_covers_operations=true)`", "`variables_ok`", "`propose_material_patch`",
            "only the edges", "Never change an external call", "offer to keep the inline form",
            "moves output from stdout to stderr, explain why", "in the tool's `reason`",
            "successful run count as a failure",
            "`origin=agent_patch, not run by the user`", "the Materials tab", "Copy and Download",
            "ask about the coverage again", "locked", "needs a new session",
            "copied from the `form:` line of `## Skill Form`", "never infer it from the conversation",
            "list every unmet condition",
            "One patch fixes every finding", "never onto its own stdout line",
            "Collect it into the result JSON and exit 3", "`globals()` never holds them",
            "`ArgumentParser(add_help=False)`", "`try` / `except SystemExit`",
            "A refused patch changes nothing", "carry every fix from the refused patch",
            "without asking again", "`Refusal N of 3`", "never infer the limit yourself",
        ]),
        ("02_draft_script_addendum.md", [
            "No Python code fence", "no `scripts/` path", "`eaa_runs`", "You never modify the script",
            "`propose_patch` edits SKILL.md only", "add the new version as a new code material",
            "remove the old code material", "confirm", "patch `## Required Inputs`", "(S3)",
            "Needs-info gets a row of its own whose first cell is `` 0, `status` = `needs_info` ``",
            "the key in the script's own stdout JSON, never the tool's",
            "`run_skill_script` reports this run as `success`",
        ]),
        ("04_test_script_addendum.md", [
            "`requested_scripts`", "`valid`", "`INVALID`", "`problems`", "args is not a list of strings",
            "is not declared by the script", "requested_scripts: (none)",
        ]),
        ("13_script_save.md", ["`script_flags_off`", "they save again", "Do not suggest converting the skill to inline"]),
        (SKILL_FORM_FLAGS_OFF_NOTE, ["Do not bring up the script form", "only if they ask"]),
        (SKILL_FORM_LOOKUP_FAILED_NOTE, ["EAA did not say no", "read again when PREPARE ends", "Do not promise either form"]),
    ],
)
def test_each_script_prompt_carries_its_instructions(filename, phrases) -> None:
    text = " ".join(load_prompt(filename).split())

    assert [phrase for phrase in phrases if phrase not in text] == []


# --- script-form runtime state ------------------------------------------------


def test_the_bundled_script_points_at_an_identical_code_material() -> None:
    prompt = build_system_prompt(_script_session(Stage.REFINE))

    assert "It is the code material `code-1` above, verbatim." in prompt
    assert prompt.count('GRAPH_BASE = "https://graph.microsoft.com/v1.0"') == 1


def test_the_bundled_script_is_shown_when_no_material_carries_it() -> None:
    prompt = build_system_prompt(_script_session(Stage.REFINE, mode="modify", materials=[TEXT]))

    assert f"## Bundled Script\n\nRead-only. Shipped next to SKILL.md; never edited in this session.\n\n```python\n{PY}\n```" in prompt


def _with_run(session: Session, requested: list[dict]) -> Session:
    result = RunResult(
        query="rooms in CLS for 8",
        expected_skill="ms-graph-room-finder",
        actual_skill="ms-graph-room-finder",
        skills_referenced=["ms-graph-room-finder"],
        passed=True,
        requested_scripts=requested,
    )
    session.test_runs.append(Run(run_id="run-1", positive_results=[result]))
    return session


REQUESTED = [
    {"skill": "ms-graph-room-finder", "script": "x", "args": ["--building", "CLS"], "valid": True, "problems": []},
    {"skill": "ms-graph-room-finder", "script": "x", "args": ["--bogus"], "valid": False,
     "problems": ["`--bogus` is not declared by the script"]},
]


def test_a_script_test_run_shows_the_requested_scripts() -> None:
    prompt = build_system_prompt(_with_run(_script_session(Stage.TEST), REQUESTED))

    assert (
        "  requested_scripts:\n"
        '  - `ms-graph-room-finder` args ["--building", "CLS"]: valid\n'
        '  - `ms-graph-room-finder` args ["--bogus"]: INVALID -- `--bogus` is not declared by the script'
    ) in prompt
    assert "requested_scripts: (none)" in build_system_prompt(_with_run(_script_session(Stage.TEST), []))


def test_an_inline_test_run_does_not_show_requested_scripts() -> None:
    prompt = build_system_prompt(_with_run(_session(Stage.TEST, form="inline"), REQUESTED))

    assert "requested_scripts" not in prompt
