from __future__ import annotations

from pathlib import Path

import pytest

from backend import state_machine
from backend.eaa_platform import script_flags_off
from backend.models import Material, MaterialKind, Session, SkillVariable, Stage, TestResult as RunResult
from backend.skill_lint import script_argument_flags, script_only_errors
from backend.state_machine import build_system_prompt, evaluate_skill_form, lock_skill_form, transition
from backend.testing import _check_requested_scripts, run_selection_tests

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "skills" / "ms-graph-room-finder"
MD = (FIXTURE / "SKILL.md").read_text(encoding="utf-8")
PY = (FIXTURE / "scripts" / "ms-graph-room-finder.py").read_text(encoding="utf-8")
FLAGS_ON = {"architectural_config": {"SKILL_SCRIPTS_ENABLED": "true"}}
FLAGS_OFF = {"architectural_config": {"SKILL_SCRIPTS_ENABLED": "false"}}


def _session(**overrides) -> Session:
    session = Session(
        materials=[Material(kind=MaterialKind.CODE, content=PY)],
        aca_env_result=FLAGS_ON,
    )
    session.prepare_brief.script_covers_operations = True
    session.prepare_brief.verify_checklist["variables_ok"] = True
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _checks(session: Session) -> list[str]:
    return [failure.check for failure in evaluate_skill_form(session).failures]


# --- eligibility ------------------------------------------------------------


def test_fixture_is_script_form_when_the_flags_are_on() -> None:
    verdict = evaluate_skill_form(_session())

    assert (verdict.form, verdict.locked, verdict.failures) == ("script", False, ())
    assert verdict.script == PY


MANUAL_ARGS = '''import json
import sys

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
print(json.dumps({"chat": args.get("--chat"), "message": args.get("--message")}))
'''


def test_manual_arguments_do_not_fail_script_readiness() -> None:
    session = _session(materials=[Material(kind=MaterialKind.CODE, content=MANUAL_ARGS)])
    session.prepare_brief.variables = [
        SkillVariable(name=name, kind="runtime", source="request") for name in ("CHAT", "MESSAGE")
    ]

    assert evaluate_skill_form(session).form == "script"


def _flag_false(s: Session) -> None:
    s.aca_env_result = FLAGS_OFF


def _two_code_materials(s: Session) -> None:
    s.materials.append(Material(kind=MaterialKind.CODE, content="print(1)\n"))


def _no_code_material(s: Session) -> None:
    s.materials = [Material(kind=MaterialKind.TEXT, content=PY)]


def _does_not_parse(s: Session) -> None:
    s.materials = [Material(kind=MaterialKind.CODE, content=PY + "\ndef broken(:\n")]


def _script_lint_error(s: Session) -> None:
    s.materials = [Material(kind=MaterialKind.CODE, content=PY.replace(", add_help=False)", ")", 1))]


def _coverage_unconfirmed(s: Session) -> None:
    s.prepare_brief.script_covers_operations = False


def _variables_not_ok(s: Session) -> None:
    s.prepare_brief.verify_checklist["variables_ok"] = False


def _request_input_without_flag(s: Session) -> None:
    s.prepare_brief.variables = [SkillVariable(name="FLOOR", kind="runtime", source="request")]


def _user_chose_inline(s: Session) -> None:
    s.prepare_brief.prefer_inline = True


LIBRARY_ONLY = '''"""Helpers only."""
import json

LIMIT: int = 3


def run() -> str:
    return json.dumps({"limit": LIMIT})
'''


def _library_only(s: Session) -> None:
    s.materials = [Material(kind=MaterialKind.CODE, content=LIBRARY_ONLY)]


@pytest.mark.parametrize(
    ("breaker", "check"),
    [
        (_flag_false, "eaa_flags"),
        (_two_code_materials, "code_material"),
        (_no_code_material, "code_material"),
        (_does_not_parse, "parses"),
        (_script_lint_error, "script_lint"),
        (_coverage_unconfirmed, "covers_operations"),
        (_variables_not_ok, "covers_operations"),
        (_request_input_without_flag, "inputs"),
        (_user_chose_inline, "user_choice"),
        (_library_only, "entry_point"),
    ],
)
def test_each_unmet_condition_makes_it_inline(breaker, check) -> None:
    session = _session()
    breaker(session)

    verdict = evaluate_skill_form(session)

    assert verdict.form == "inline" and verdict.script is None
    assert _checks(session) == [check]


def test_script_lint_failure_names_the_rule() -> None:
    session = _session()
    _script_lint_error(session)

    assert [(f.check, f.rule) for f in evaluate_skill_form(session).failures] == [("script_lint", "S10b")]


def test_request_inputs_need_a_flag_but_env_backed_variables_do_not() -> None:
    session = _session()
    session.prepare_brief.variables = [
        SkillVariable(name="BUILDING", kind="runtime", source="request"),
        SkillVariable(name="ROOM_LIST", kind="runtime", source="request"),
        SkillVariable(name="TENANT_HINT", kind="runtime", source="credentials", credentials_key="TENANT_HINT"),
        SkillVariable(name="DEFAULT_BUILDING", kind="aca_env"),
        SkillVariable(name="GRAPH_ACCESS_TOKEN", kind="obo_token"),
    ]

    assert evaluate_skill_form(session).failures == ()


def test_storage_cleanup_material_is_still_refused_when_arguments_are_unknown() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    session = _session(materials=[Material(
        kind=MaterialKind.CODE, content=(fixtures / "storage_cleanup_material.py").read_text(encoding="utf-8"),
    )])
    session.prepare_brief.variables = [
        SkillVariable(name=name, kind="runtime", source="request")
        for name in ("TARGET_TABLES", "TARGET_CONTAINERS", "BLOB_PREFIX", "RETENTION_RULE", "DRY_RUN")
    ]
    failures = evaluate_skill_form(session).failures

    assert sorted({(f.check, f.rule) for f in failures}) == [("script_lint", "S13"), ("script_lint", "S4")]

    session.materials = [Material(
        kind=MaterialKind.CODE, content=(fixtures / "storage_cleanup_script.py").read_text(encoding="utf-8"),
    )]
    assert evaluate_skill_form(session).failures == ()


def test_scenario_and_import_sessions_are_inline() -> None:
    assert _checks(_session(skill_kind="scenario"))[0] == "skill_kind"
    assert _checks(_session(mode="import")) == ["mode"]


def test_failures_state_what_failed_and_are_listed_together() -> None:
    session = _session(aca_env_result=FLAGS_OFF, materials=[])
    session.prepare_brief.script_covers_operations = False

    payload = evaluate_skill_form(session).to_dict()

    assert payload["form"] == "inline" and payload["locked"] is False
    assert [f["check"] for f in payload["failures"]] == ["eaa_flags", "code_material", "covers_operations"]
    assert "SKILL_SCRIPTS_ENABLED" in payload["failures"][0]["message"]
    assert "DYNAMIC_SKILLS_ENABLED" not in payload["failures"][0]["message"]


@pytest.mark.parametrize(
    ("result", "off"),
    [
        (FLAGS_ON, []),
        ({"architectural_config": {"SKILL_SCRIPTS_ENABLED": True}}, []),
        ({"architectural_config": {"SKILL_SCRIPTS_ENABLED": "TRUE"}}, []),
        (FLAGS_OFF, ["SKILL_SCRIPTS_ENABLED"]),
        ({"architectural_config": {"SKILL_SCRIPTS_ENABLED": False}}, ["SKILL_SCRIPTS_ENABLED"]),
        ({"architectural_config": {"SKILL_SCRIPTS_ENABLED": "FALSE"}}, ["SKILL_SCRIPTS_ENABLED"]),
        ({"architectural_config": {}}, []),
        ({"architectural_config": None}, []),
        ({"architectural_config": []}, []),
        ({}, []),
        (None, []),
        ({"architectural_config": {"SKILL_SCRIPTS_ENABLED": "1"}}, ["SKILL_SCRIPTS_ENABLED"]),
    ],
)
def test_script_flags_are_read_like_eaa(result, off) -> None:
    assert script_flags_off(result) == off


@pytest.mark.parametrize("dynamic", [None, True, False, "true", "false"])
@pytest.mark.parametrize("enabled", [None, True, False])
def test_only_the_script_flag_controls_form(dynamic, enabled) -> None:
    config = {}
    if dynamic is not None:
        config["DYNAMIC_SKILLS_ENABLED"] = dynamic
    if enabled is not None:
        config["SKILL_SCRIPTS_ENABLED"] = enabled
    session = _session(aca_env_result={"architectural_config": config})

    verdict = evaluate_skill_form(session)

    assert verdict.form == ("inline" if enabled is False else "script")
    assert _checks(session) == (["eaa_flags"] if enabled is False else [])


def test_script_only_errors_needs_no_skill_md() -> None:
    assert script_only_errors(PY) == []
    rules = {i.rule for i in script_only_errors('import sys\nprint("hi")\nsys.exit(2)\n')}
    assert rules == {"S4", "S5"}
    with pytest.raises(SyntaxError):
        script_only_errors("def broken(:\n")


# --- lock -------------------------------------------------------------------


def _to_draft(session: Session, monkeypatch) -> None:
    monkeypatch.setattr(state_machine, "check_quality_gates", lambda _session: [])
    transition(session, Stage.DRAFT)


def test_prepare_to_draft_locks_the_script_form_and_takes_the_code_verbatim(monkeypatch) -> None:
    session = _session()

    _to_draft(session, monkeypatch)

    assert session.skill_form == "script"
    assert session.current_skill.script == PY
    assert [m.content for m in session.conversation if "now locked" in m.content] == [
        state_machine.SCRIPT_LOCKED_SCRIPT_MESSAGE
    ]


def test_the_users_inline_choice_wins_over_a_ready_script(monkeypatch) -> None:
    session = _session()
    _user_chose_inline(session)

    assert state_machine._form_prompt_key(session) is None
    assert state_machine._format_skill_form(session) == (
        "## Skill Form\n\nform: inline\nlocked: false\n"
        "- user_choice: The user chose inline sample code over a bundled script."
    )
    _to_draft(session, monkeypatch)

    assert (session.skill_form, session.current_skill.script) == ("inline", None)
    assert [m for m in session.conversation if "now locked" in m.content] == []


def test_a_library_without_request_inputs_is_not_a_script() -> None:
    # Reproduces a session whose variables were wiped, leaving the `inputs` check nothing to compare.
    session = _session(materials=[Material(kind=MaterialKind.CODE, content=LIBRARY_ONLY)])
    session.prepare_brief.variables = []

    assert evaluate_skill_form(session).form == "inline"
    assert _checks(session) == ["entry_point"]


def test_prepare_to_draft_locks_inline_without_a_script(monkeypatch) -> None:
    session = _session(aca_env_result=FLAGS_OFF)

    _to_draft(session, monkeypatch)

    assert session.skill_form == "inline"
    assert session.current_skill.script is None


def _lock_notices(session: Session) -> list[str]:
    return [m.content for m in session.conversation if "now locked as inline" in m.content]


def test_a_script_candidate_locked_inline_tells_the_agent_why(monkeypatch) -> None:
    session = _session()
    _script_lint_error(session)

    _to_draft(session, monkeypatch)

    [notice] = _lock_notices(session)
    assert session.skill_form == "inline"
    assert "\n- script_lint (S10b): " in notice and "needs a new session" in notice


@pytest.mark.parametrize(
    "overrides",
    [{}, {"aca_env_result": FLAGS_OFF}, {"mode": "import"}],
    ids=["script", "flags-off", "import"],
)
def test_no_inline_lock_notice_when_script_form_was_never_possible_or_was_reached(overrides, monkeypatch) -> None:
    session = _session(**overrides)

    _to_draft(session, monkeypatch)

    assert _lock_notices(session) == []


def test_locked_form_survives_a_return_to_prepare(monkeypatch) -> None:
    session = _session(aca_env_result=FLAGS_OFF)
    _to_draft(session, monkeypatch)
    transition(session, Stage.PREPARE)
    session.aca_env_result = FLAGS_ON

    assert evaluate_skill_form(session).to_dict() == {"form": "inline", "locked": True, "failures": []}
    _to_draft(session, monkeypatch)
    assert (session.skill_form, session.current_skill.script) == ("inline", None)


def test_locked_script_keeps_its_script_when_the_material_changes(monkeypatch) -> None:
    session = _session()
    _to_draft(session, monkeypatch)
    session.materials = []

    lock_skill_form(session)

    assert (session.skill_form, session.current_skill.script) == ("script", PY)


def test_modify_inherits_the_form_of_the_stored_skill() -> None:
    stored = _session(mode="modify")
    stored.current_skill.script = "print('stored')\n"
    inline = _session(mode="modify")

    lock_skill_form(stored)
    lock_skill_form(inline)

    assert (stored.skill_form, stored.current_skill.script) == ("script", "print('stored')\n")
    assert (inline.skill_form, inline.current_skill.script) == ("inline", None)


def test_skill_form_prompt_block_only_appears_with_code() -> None:
    session = _session(aca_env_result=None)
    assert "## Skill Form" in build_system_prompt(session)
    assert "- eaa_flags:" not in build_system_prompt(session)
    assert "form: script" in build_system_prompt(session)

    session.materials = [Material(kind=MaterialKind.TEXT, content="notes")]
    assert "## Skill Form" not in build_system_prompt(session)


def _every_condition_unmet_but_flags(s: Session) -> None:
    _two_code_materials(s)
    s.prepare_brief.script_covers_operations = False


def test_skill_form_block_with_the_flags_off_shows_only_the_flag() -> None:
    session = _session(aca_env_result=FLAGS_OFF)
    _every_condition_unmet_but_flags(session)
    flag = evaluate_skill_form(session).failures[0]

    assert flag.check == "eaa_flags" and len(evaluate_skill_form(session).failures) == 3
    assert state_machine._format_skill_form(session) == (
        f"## Skill Form\n\nform: inline\n- eaa_flags: {flag.message} "
        + state_machine.load_prompt(state_machine.SKILL_FORM_FLAGS_OFF_NOTE)
    )


def test_skill_form_block_with_the_flags_on_lists_every_unmet_condition() -> None:
    session = _session()
    _every_condition_unmet_but_flags(session)

    block = state_machine._format_skill_form(session)

    assert "locked: false" in block and "eaa_flags" not in block
    assert "- code_material:" in block and "- covers_operations:" in block


LOOKUP_ERROR = "HTTP 404 Not Found | <!DOCTYPE html><html><title>Azure Container App - Unavailable</title>"


def test_a_failed_flag_lookup_is_not_reported_as_flags_off() -> None:
    session = _session(aca_env_result=None, aca_env_error=LOOKUP_ERROR)

    assert evaluate_skill_form(session).form == "script"
    assert _checks(session) == []
    prompt = build_system_prompt(session)
    assert "eaa_flags" not in prompt
    assert f"Lookup status: failed\n\nError: {LOOKUP_ERROR}" in prompt
    assert "script form cannot be decided" not in prompt


@pytest.mark.parametrize("error", ["", LOOKUP_ERROR])
def test_locking_after_an_unavailable_lookup_allows_script(monkeypatch, error) -> None:
    session = _session(aca_env_result=None, aca_env_error=error)

    _to_draft(session, monkeypatch)

    assert session.skill_form == "script"
    assert session.current_skill.script == PY
    assert session.aca_env_error == error
    assert _lock_notices(session) == []


def test_the_flag_lookup_is_retried_only_while_a_new_code_skill_awaits_its_form() -> None:
    session = _session(aca_env_result=None)
    assert state_machine.needs_flag_lookup_before_lock(session)
    assert not state_machine.needs_flag_lookup_before_lock(_session())
    assert not state_machine.needs_flag_lookup_before_lock(_session(aca_env_result=None, skill_form="inline"))
    assert not state_machine.needs_flag_lookup_before_lock(_session(aca_env_result=None, mode="modify"))
    session.materials = [Material(kind=MaterialKind.TEXT, content="notes")]
    assert not state_machine.needs_flag_lookup_before_lock(session)


# --- testing: requested_scripts ----------------------------------------------

FLAGS = script_argument_flags(PY)


def test_script_argument_flags_are_the_s3_accepted_set() -> None:
    assert "--building" in FLAGS and "--list-room-lists" in FLAGS


@pytest.mark.parametrize(
    ("args", "problems"),
    [
        (["--building", "B1", "--capacity=8"], []),
        (["--bogus", "x"], ["`--bogus` is not declared by the script"]),
        ("--building B1", ["args is not a list of strings"]),
        (["--capacity", 8], ["args is not a list of strings"]),
        (None, ["args is not a list of strings"]),
    ],
)
def test_requested_script_args_are_checked(args, problems) -> None:
    raw = {"requested_scripts": [{"skill": "ms-graph-room-finder", "script": "x.py", "args": args}]}

    [entry] = _check_requested_scripts(raw, "ms-graph-room-finder", FLAGS)

    assert entry["valid"] is (not problems)
    assert entry["problems"] == problems


def test_requested_scripts_absent_when_the_runtime_flag_is_off() -> None:
    assert _check_requested_scripts({"skills_referenced": []}, "ms-graph-room-finder", FLAGS) == []


def test_another_skills_args_are_only_type_checked() -> None:
    raw = {"requested_scripts": [{"skill": "other", "script": "o.py", "args": ["--anything"]}]}

    assert _check_requested_scripts(raw, "ms-graph-room-finder", FLAGS)[0]["valid"] is True


def test_run_selection_tests_attaches_checked_requested_scripts(monkeypatch) -> None:
    raw = {"requested_scripts": [{"skill": "ms-graph-room-finder", "script": "s.py", "args": ["--nope"]}]}

    def fake_runner(positive, negative, skill_name, token, run_mode="route_only"):
        return [RunResult(query=positive[0], apim_raw_response=raw)], []

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(MD, ["find a room"], [], script=PY)

    assert run.positive_results[0].requested_scripts[0]["problems"] == ["`--nope` is not declared by the script"]


def test_manual_arguments_are_not_reported_as_undeclared_in_routing_tests(monkeypatch) -> None:
    raw = {"requested_scripts": [
        {"skill": "ms-graph-room-finder", "script": "s.py", "args": ["--chat", "room"]}
    ]}

    def fake_runner(positive, negative, skill_name, token, run_mode="route_only"):
        return [RunResult(query=positive[0], apim_raw_response=raw)], []

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(MD, ["send a message"], [], script=MANUAL_ARGS)

    assert run.positive_results[0].requested_scripts[0]["problems"] == []
    assert any("not statically verified" in note for note in run.notes)
    session = _session(skill_form="script", current_stage=Stage.TEST)
    session.test_runs.append(run)
    assert "not statically verified" in build_system_prompt(session)
