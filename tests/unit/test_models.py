from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.models import ChecklistItem, Material, VerifyChecklist


def test_verify_checklist_all_confirmed() -> None:
    checklist = VerifyChecklist()
    assert checklist.all_confirmed() is False

    for item in (
        checklist.understanding,
        checklist.differentiation,
        checklist.identity,
        checklist.metadata,
        checklist.io_spec,
        checklist.env_vars,
        checklist.test_samples,
    ):
        item.status = "confirmed"

    assert checklist.all_confirmed() is True


def test_checklist_status_validation_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ChecklistItem(status="done")


def test_material_defaults_to_text_kind_and_timestamp() -> None:
    material = Material(content="hello")

    assert material.kind == "text"
    assert material.created_at

def test_prepare_checklist_default_order_and_keys() -> None:
    from backend.models import _DEFAULT_PREPARE_CHECKLIST

    assert list(_DEFAULT_PREPARE_CHECKLIST) == [
        "definition_clear",
        "routing_uniqueness_confirmed",
        "variables_ok",
    ]


def test_prepare_checklist_migrates_legacy_keys_to_three_blocks() -> None:
    from backend.models import PrepareBrief

    # Old env_vars_ok -> variables_ok; uniqueness + samples -> routing_uniqueness
    # (confirmed only when BOTH legacy keys were confirmed).
    pb = PrepareBrief.model_validate(
        {
            "verify_checklist": {
                "definition_clear": True,
                "samples_confirmed": True,
                "uniqueness_confirmed": False,
                "env_vars_ok": True,
            }
        }
    )
    assert pb.verify_checklist == {
        "definition_clear": True,
        "routing_uniqueness_confirmed": False,
        "variables_ok": True,
    }

    pb2 = PrepareBrief.model_validate(
        {
            "verify_checklist": {
                "definition_clear": True,
                "samples_confirmed": True,
                "uniqueness_confirmed": True,
                "env_vars_ok": True,
            }
        }
    )
    assert pb2.verify_checklist["routing_uniqueness_confirmed"] is True


def test_session_preserves_current_capability_and_scenario_checklists() -> None:
    from backend.models import Session, SkillKind

    capability = Session.model_validate({
        "prepare_brief": {
            "verify_checklist": {
                "definition_clear": True,
                "routing_uniqueness_confirmed": False,
                "variables_ok": True,
            }
        }
    })
    assert capability.prepare_brief.verify_checklist == {
        "definition_clear": True,
        "routing_uniqueness_confirmed": False,
        "variables_ok": True,
    }

    scenario = Session.model_validate({
        "skill_kind": SkillKind.SCENARIO,
        "prepare_brief": {
            "verify_checklist": {
                "definition_clear": True,
                "routing_uniqueness_confirmed": True,
                "delegation_ok": False,
            }
        },
    })
    assert scenario.prepare_brief.verify_checklist == {
        "definition_clear": True,
        "routing_uniqueness_confirmed": True,
        "delegation_ok": False,
    }


def test_session_aligns_checklist_when_kind_changes() -> None:
    from backend.models import Session, SkillKind

    session = Session.model_validate({
        "skill_kind": SkillKind.SCENARIO,
        "prepare_brief": {
            "verify_checklist": {
                "definition_clear": True,
                "routing_uniqueness_confirmed": False,
                "variables_ok": True,
            }
        },
    })

    assert session.prepare_brief.verify_checklist == {
        "definition_clear": True,
        "routing_uniqueness_confirmed": False,
        "delegation_ok": False,
    }


def test_delegation_recorded_before_operations_existed_still_loads() -> None:
    from backend.models import Delegation

    delegation = Delegation.model_validate(
        {"child_skill": "hr-leave-system", "credentials_key": "hr_leave_json"}
    )

    assert delegation.operations == []


def test_prepare_brief_holds_samples_and_variables() -> None:
    from backend.models import NegativeSample, PrepareBrief, SkillVariable

    pb = PrepareBrief()
    pb.positive_samples = ["q1"]
    pb.negative_samples = [
        NegativeSample(query="n1", route_to_peer="peer-x", why_not_this="different goal")
    ]
    pb.variables = [SkillVariable(name="API_KEY", kind="aca_env", in_aca=True, description="auth key")]

    dumped = pb.model_dump()
    assert dumped["positive_samples"] == ["q1"]
    assert dumped["negative_samples"][0]["route_to_peer"] == "peer-x"
    assert dumped["variables"][0]["kind"] == "aca_env"
    assert dumped["variables"][0]["in_aca"] is True


def test_skill_variable_defaults_runtime_and_required() -> None:
    from backend.models import SkillVariable

    v = SkillVariable(name="RESOURCE_ID")
    assert v.kind == "runtime"
    assert v.in_aca is False
    assert v.required is True


def test_skill_variable_migrates_legacy_type_to_kind_and_in_aca() -> None:
    from backend.models import SkillVariable

    reuse = SkillVariable.model_validate({"name": "API_KEY", "type": "env_reuse", "source": "ACA", "reason": "auth"})
    assert reuse.kind == "aca_env" and reuse.in_aca is True
    assert reuse.description == "ACA -- auth"

    add = SkillVariable.model_validate({"name": "DB_URL", "type": "env_add"})
    assert add.kind == "aca_env" and add.in_aca is False

    rt = SkillVariable.model_validate({"name": "file", "type": "runtime", "reason": "the file to process"})
    assert rt.kind == "runtime" and rt.in_aca is False
    assert rt.description == "the file to process"

    # Phase-2 kind='env' migrates to aca_env.
    p2 = SkillVariable.model_validate({"name": "X", "kind": "env", "in_aca": True})
    assert p2.kind == "aca_env" and p2.in_aca is True

def test_neighbor_skill_and_understanding_drop_target_users() -> None:
    from backend.models import NeighborSkill, UnderstandingBrief

    ub = UnderstandingBrief(neighbor_skills=[NeighborSkill(skill="peer-a", axis="x", scenario="y")])
    assert ub.neighbor_skills[0].skill == "peer-a"
    assert "target_users" not in ub.model_dump()


def test_neighbor_edit_version_defaults() -> None:
    from backend.models import NeighborEdit, NeighborVersion

    ne = NeighborEdit(skill_name="peer-a", versions=[NeighborVersion(origin="original", skill_md="x")])
    assert ne.status == "draft"
    assert ne.versions[0].origin == "original"
    assert ne.versions[0].version_id



def test_understanding_brief_migrates_user_goal_to_skill_goal() -> None:
    from backend.models import UnderstandingBrief

    legacy = UnderstandingBrief.model_validate({"user_goal": "do the thing"})
    assert legacy.skill_goal == "do the thing"
    assert "user_goal" not in legacy.model_dump()

    # New key is used as-is.
    fresh = UnderstandingBrief(skill_goal="answer RAG queries")
    assert fresh.skill_goal == "answer RAG queries"