from __future__ import annotations

import pytest

from backend import testing
from backend.agent import TOOL_SCHEMAS
from backend.models import Delegation, Mode, Session, SkillDraft, SkillKind, Stage
from backend.orchestration import skill_orchestration_owner
from backend.skill_lint import check_named_skill_references, lint_skill
from backend.state_machine import build_system_prompt, check_quality_gates
from backend.topology import has_errors, validate_topology


HOST = """---
name: parent-skill
description: Coordinate the case.
metadata:
  skill_type: scenario-orchestration
  children: [child-skill]
---

## Instructions
Read the capability contracts, ask for missing inputs and confirm before publishing.
"""
EAA = HOST.replace("  children:", "  orchestration_owner: eaa\n  children:")
CHILD = "---\nname: child-skill\nmetadata: {skill_type: inline}\n---\n\n## Overview\nCapability.\n"


def validate(md=EAA, child=CHILD, **kwargs):
    return validate_topology(md, SkillKind.SCENARIO, mode=Mode.NEW, child_resolver=lambda _: child, **kwargs)


def test_lightweight_eaa_and_legacy_host_are_valid():
    assert not has_errors(validate())
    assert not has_errors(validate(HOST))
    assert skill_orchestration_owner(HOST) == "host"
    assert skill_orchestration_owner(EAA) == "eaa"


@pytest.mark.parametrize("md", [
    EAA.replace("orchestration_owner: eaa", "orchestration_owner: invalid"),
    EAA.replace("orchestration_owner: eaa", "orchestration_owner: null"),
    EAA.replace("  orchestration_owner: eaa", "orchestration_owner: eaa"),
    EAA.split("## Instructions")[0],
    EAA.replace("children: [child-skill]", "children: []"),
    EAA.replace("  children:", "  orchestration: {}\n  children:"),
    EAA.replace("  children:", "  orchestration_ref: workflow.yaml\n  children:"),
    EAA.replace("  children:", "orchestration_ref: workflow.yaml\nmetadata2:\n  children:"),
])
def test_invalid_owner_or_old_flow_contract_is_rejected(md):
    assert has_errors(validate(md))


@pytest.mark.parametrize("skill_type,script", [("inline", None), ("script", "print('ok')")])
def test_capabilities_cannot_declare_owner(skill_type, script):
    md = CHILD.replace(f"skill_type: inline", f"skill_type: {skill_type}, orchestration_owner: host")
    issues = validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW, script=script)
    assert any(issue.rule == "T12" for issue in issues)


@pytest.mark.parametrize("child", [
    CHILD.replace("inline", "instruction-only"),
    CHILD.replace("inline", "scenario-orchestration, children: [grandchild]"),
    "no frontmatter",
    CHILD.replace("metadata: {skill_type: inline}", "metadata: []"),
])
def test_eaa_children_must_be_executable_capabilities(child):
    assert has_errors(validate(child=child))


def test_parent_script_and_public_mcp_calls_are_rejected():
    assert has_errors(validate(script="print('bad')"))
    assert has_errors(validate(EAA + '\nfetch_skill(skill_name="child-skill")\n'))


@pytest.mark.parametrize("owner", ["host", "eaa"])
def test_legacy_session_owner_restored_from_body(owner):
    raw = {"skill_kind": "scenario", "current_skill": {"skill_md": EAA if owner == "eaa" else HOST}}
    session = Session.model_validate(raw)
    assert session.orchestration_owner == owner
    assert Session.model_validate(session.model_dump()).orchestration_owner == owner
    assert Session(skill_kind=SkillKind.SCENARIO, current_skill=SkillDraft(skill_md=EAA)).orchestration_owner == "eaa"


def test_eaa_quality_gate_does_not_require_host_tools():
    session = Session(skill_kind=SkillKind.SCENARIO, orchestration_owner="eaa", children=["child-skill"])
    session.child_full_md = {"child-skill": CHILD}
    session.prepare_brief.delegation = [Delegation(child_skill="child-skill")]
    assert "delegation.host_capabilities" not in check_quality_gates(session)
    session.prepare_brief.delegation[0].host_capabilities = ["Read local files"]
    assert "delegation.host_capabilities_not_available_to_eaa" in check_quality_gates(session)
    session.orchestration_owner = "host"
    assert "delegation.host_capabilities_not_available_to_eaa" not in check_quality_gates(session)


@pytest.mark.parametrize("stage", list(Stage))
def test_eaa_prompt_has_registered_owner_addendum_at_every_stage(stage):
    session = Session(skill_kind=SkillKind.SCENARIO, orchestration_owner="eaa", current_stage=stage)
    prompt = build_system_prompt(session)
    assert "orchestration_owner: eaa" in prompt
    assert "## EAA owner addendum" in prompt
    assert "Leave `host_capabilities` empty" in prompt
    assert prompt.rfind("## EAA owner addendum") > prompt.find("# Scenario Skill Format Spec")
    host_prompt = build_system_prompt(Session(skill_kind=SkillKind.SCENARIO, current_stage=stage))
    assert "## Scenario ownership contract" in host_prompt
    assert "## EAA owner addendum" not in host_prompt


@pytest.mark.parametrize("owner", ["host", "eaa"])
@pytest.mark.parametrize("mode", list(Mode))
def test_scenario_prepare_discloses_owner_choices_before_dependency_review(owner, mode):
    session = Session(
        skill_kind=SkillKind.SCENARIO, orchestration_owner=owner,
        mode=mode, current_stage=Stage.PREPARE,
    )
    prompt = build_system_prompt(session)
    disclosure = prompt.index("## Proactive owner choice before dependency review")
    dependency_review = prompt.index("## Override for block 1(b)")
    assert disclosure < dependency_review
    assert f"orchestration_owner: {owner}" in prompt
    prompt = " ".join(prompt[disclosure:dependency_review].split())
    assert "The default `host` is not evidence of author consent" in prompt
    assert "Host Agent -- the Host reads child contracts" in prompt
    assert "EAA internal Agent -- EAA reads child contracts" in prompt
    assert "Mode B and interactive delegated/OBO identity" in prompt
    assert "State a recommendation and its concrete reason" in prompt
    for option in ("Keep Host", "Switch to EAA", "Keep EAA", "Switch to Host"):
        assert option in prompt
    assert "Checklist > Orchestration owner" in prompt
    assert "Apply owner" in prompt
    assert "call `set_orchestration_owner`" in prompt
    assert "Selecting the switch option on this informed card is sufficient consent" in prompt
    assert "Do not ask again after an informed choice or require the author to click the UI" in prompt
    assert "confirm_restart=false" in prompt
    assert "only tool call in the turn" in prompt
    assert "Do not repeat an already answered owner question" in prompt


def test_owner_tool_schema_and_shared_prompt_support_single_informed_confirmation():
    schema = next(tool for tool in TOOL_SCHEMAS if tool["name"] == "set_orchestration_owner")
    assert schema["parameters"]["properties"]["owner"]["enum"] == ["host", "eaa"]
    assert schema["parameters"]["properties"]["confirm_restart"]["type"] == "boolean"
    assert set(schema["parameters"]["required"]) == {"owner", "confirm_restart"}
    assert schema["parameters"]["additionalProperties"] is False
    for owner in ("host", "eaa"):
        for stage in Stage:
            prompt = build_system_prompt(Session(
                skill_kind=SkillKind.SCENARIO, orchestration_owner=owner, current_stage=stage,
            ))
            prompt = " ".join(prompt.split())
            assert "a required second action" in prompt
            assert "confirm_restart=true" in prompt
            assert 'or a "continue" message' in prompt
            assert "Do not choose an owner on the author's behalf" in prompt
            assert "direct them to the scenario owner selector" not in prompt


@pytest.mark.parametrize("stage", list(Stage))
def test_proactive_owner_choice_is_scoped_to_scenario_prepare(stage):
    scenario = build_system_prompt(Session(skill_kind=SkillKind.SCENARIO, current_stage=stage))
    capability = build_system_prompt(Session(current_stage=stage))
    heading = "## Proactive owner choice before dependency review"
    assert (heading in scenario) is (stage is Stage.PREPARE)
    assert heading not in capability


def test_eaa_named_children_are_allowed_only_in_execution_body():
    catalog = {"child-skill", "unlisted-skill"}
    assert not check_named_skill_references(EAA + "\nUse `child-skill` to do the work.\n", SkillKind.SCENARIO, catalog)
    for md in (
        EAA.replace("Coordinate the case.", "Use child-skill."),
        EAA + "\n## When NOT to Use\nUse child-skill instead.\n",
        EAA + "\nUse unlisted-skill.\n",
    ):
        assert check_named_skill_references(md, SkillKind.SCENARIO, catalog)
    assert check_named_skill_references(HOST + "\nUse child-skill.\n", SkillKind.SCENARIO, catalog)


@pytest.mark.parametrize("output,passed", [
    ({"skills_referenced": ["parent-skill"], "raw_response": {"success": True}}, True),
    ({"skills_referenced": ["child-skill"]}, False),
    ({"skills_referenced": ["parent-skill"], "raw_response": {"success": False}}, False),
    ({"error": "unavailable"}, None),
])
def test_eaa_route_only_resolves_parent_without_claiming_child_execution(monkeypatch, output, passed):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return output

    monkeypatch.setattr(testing, "_post_apim_run", post)
    run = testing.run_selection_tests(EAA, ["analyze"], [], kind=SkillKind.SCENARIO, child_resolver=lambda _: CHILD)
    assert len(calls) == 1
    assert calls[0]["mode"] == "route_only"
    assert calls[0]["scenario"] == "parent-skill"
    assert run.scenario_layers[1].title == "EAA scenario resolution"
    assert run.scenario_layers[1].passed is passed
    assert run.scenario_layers[2].passed is None
    assert testing.EAA_SCENARIO_EXECUTION_NOTE in run.notes


def test_eaa_tests_cannot_execute_or_ignore_platform_errors(monkeypatch):
    monkeypatch.setattr(testing, "_post_apim_run", lambda *a, **k: pytest.fail("No runtime call expected"))
    with pytest.raises(ValueError, match="route_only"):
        testing.run_selection_tests(EAA, ["analyze"], [], kind=SkillKind.SCENARIO, child_resolver=lambda _: CHILD, run_mode="execute")
    md = EAA + '\nRead os.environ["OBO_CLIENT_SECRET"].\n'
    assert any(issue.severity == "error" for issue in lint_skill(md, SkillKind.SCENARIO))
    run = testing.run_selection_tests(md, ["analyze"], [], kind=SkillKind.SCENARIO, child_resolver=lambda _: CHILD)
    assert len(run.scenario_layers) == 1
    assert run.scenario_layers[0].passed is False


def test_fake_eaa_runner_preserves_mode_echo_verification(monkeypatch):
    from backend import e2e

    monkeypatch.setattr(e2e, "get_scenario", lambda: "scenario_skill_happy_path")
    run = e2e.fake_run_selection_tests(EAA, ["analyze"], [], kind=SkillKind.SCENARIO)
    result = run.scenario_layers[1].results[0]
    assert result.skills_referenced == ["parent-skill"]
    assert result.apim_raw_response["mode"] == "route_only"
    assert run.scenario_layers[2].passed is None
    with pytest.raises(ValueError, match="route_only"):
        e2e.fake_run_selection_tests(EAA, ["analyze"], [], kind=SkillKind.SCENARIO, run_mode="execute")
    monkeypatch.setattr(e2e, "get_scenario", lambda: "mode_echo_missing")
    with pytest.raises(testing.ModeEchoError, match="did not echo"):
        e2e.fake_run_selection_tests(EAA, ["analyze"], [], kind=SkillKind.SCENARIO)
