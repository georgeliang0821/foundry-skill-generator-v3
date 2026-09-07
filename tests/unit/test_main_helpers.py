from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.blob_store import compute_hash
from backend.models import Session, SkillKind, Stage


def test_current_and_set_current_content(backend_main) -> None:
    session = Session()

    backend_main.set_current_content(session, "SKILL.md", "skill", "hash1")

    assert backend_main.current_content(session, "SKILL.md") == "skill"
    with pytest.raises(HTTPException):
        backend_main.current_content(session, "unknown")
    with pytest.raises(HTTPException):
        backend_main.set_current_content(session, "env.yaml", "env", "hash2")


VALID_MD = '---\nname: demo\ndescription: "d"\nmetadata:\n  author: a\n---\n\n## Overview\n'
# A leading space makes YAML read the key as a continuation of `name`'s value.
UNPARSEABLE_MD = '---\nname: demo\n description: "d"\nmetadata:\n  author: a\n---\n\n## Overview\n'


def test_assert_topology_preserved_rejects_unparseable_frontmatter(backend_main) -> None:
    from backend.models import Mode

    session = Session(mode=Mode.MODIFY)
    session.current_skill.skill_md = VALID_MD

    # MODIFY downgrades T1-T3 at save time; an edit made in-session must not
    # inherit that leniency.
    with pytest.raises(ValueError, match="no longer parses"):
        backend_main._assert_topology_preserved(session, UNPARSEABLE_MD, tool="propose_patch")

    backend_main._assert_topology_preserved(session, VALID_MD, tool="propose_patch")


def test_assert_topology_preserved_allows_already_broken_imports(backend_main) -> None:
    from backend.models import Mode

    session = Session(mode=Mode.IMPORT)
    session.current_skill.skill_md = UNPARSEABLE_MD

    # Regression-only: a file that arrived malformed stays editable.
    backend_main._assert_topology_preserved(session, UNPARSEABLE_MD, tool="propose_patch")


def test_infer_skill_name_requested_wins_then_frontmatter(backend_main) -> None:
    from backend.models import Mode

    # requested_name always wins.
    session = Session(target_skill_id="Target Skill")
    session.current_skill.skill_md = "---\nname: frontmatter-skill\n---\n"
    assert backend_main.infer_skill_name(session, "Requested Skill") == "requested-skill"

    # The live frontmatter name is the source of truth in every mode; MODIFY is
    # no longer pinned to the target id, so rename_skill can actually rename.
    for mode in (Mode.NEW, Mode.IMPORT, Mode.MODIFY):
        session.mode = mode
        assert backend_main.infer_skill_name(session) == "frontmatter-skill"

    # No frontmatter name falls back to the target id.
    session.current_skill.skill_md = ""
    assert backend_main.infer_skill_name(session) == "target-skill"


def test_rename_skill_tool_gate(backend_main) -> None:
    session = Session(current_stage=Stage.REFINE.value)
    session.current_skill.skill_md = VALID_MD
    args = {"new_name": "renamed-demo", "reason": "user asked"}

    backend_main.apply_tool_effect(session, "rename_skill", args)

    with pytest.raises(ValueError, match="only allowed in REFINE/TEST"):
        backend_main.apply_tool_effect(Session(current_stage=Stage.DRAFT.value), "rename_skill", args)
    for bad in ("Renamed Demo", "-leading", "trailing-", "a" * 65, ""):
        with pytest.raises(ValueError, match="not a valid skill name"):
            backend_main.apply_tool_effect(session, "rename_skill", {"new_name": bad, "reason": "r"})
    with pytest.raises(ValueError, match="already named"):
        backend_main.apply_tool_effect(session, "rename_skill", {"new_name": "demo", "reason": "r"})


def test_session_binding_state_cases(backend_main) -> None:
    new_session = Session()
    assert backend_main.session_binding_state(new_session)[1] is False

    unknown_remote = Session(target_skill_id="demo")
    assert backend_main.session_binding_state(unknown_remote)[1] is False

    unsaved = Session(remote_skill_id="demo", remote_version_hash="remote")
    unsaved.current_skill.version_hash = "local"
    remote_name, ok, reason = backend_main.session_binding_state(unsaved)
    assert remote_name == "demo"
    assert ok is False
    assert "UNSAVED" in reason
    assert backend_main.session_binding_state(unsaved, allow_unsaved=True)[1] is True


def test_session_test_samples_uses_checklist_then_fallback(backend_main) -> None:
    session = Session(target_skill_id="demo-skill")
    session.verify_checklist.test_samples.content = {
        "positive": ["p1", "p2", "", "p3", "p4", "p5", "p6"],
        "negative": ["n1", "n2", "n3", "n4", "n5", "n6"],
    }

    positive, negative = backend_main.session_test_samples(session)

    assert positive == ["p1", "p2", "p3", "p4", "p5", "p6"]
    assert negative == ["n1", "n2", "n3", "n4", "n5", "n6"]

    session.verify_checklist.test_samples.content = None
    fallback_positive, fallback_negative = backend_main.session_test_samples(session)
    assert len(fallback_positive) == 5
    assert len(fallback_negative) == 5
    assert "demo-skill" in fallback_positive[0]


def test_apply_tool_effect_updates_state(backend_main) -> None:
    session = Session()

    # update_verify_checklist (legacy back-compat) still works.
    backend_main.apply_tool_effect(
        session,
        "update_verify_checklist",
        {"item": "identity", "status": "confirmed", "content": {"name": "demo"}},
    )
    assert session.verify_checklist.identity.status == "confirmed"

    # update_prepare_checklist toggles new dict-based checklist.
    backend_main.apply_tool_effect(
        session,
        "update_prepare_checklist",
        {"item": "definition_clear", "confirmed": True},
    )
    assert session.prepare_brief.verify_checklist["definition_clear"] is True

    # propose_skill_draft is only valid in DRAFT and only when skill_md is empty.
    session.current_stage = Stage.DRAFT.value
    backend_main.apply_tool_effect(
        session,
        "propose_skill_draft",
        {"skill_md": "---\nname: demo\n---\n"},
    )
    assert session.current_skill.version_hash == compute_hash(session.current_skill.skill_md)

    # Second propose_skill_draft must be rejected (invariant).
    import pytest
    with pytest.raises(ValueError):
        backend_main.apply_tool_effect(
            session,
            "propose_skill_draft",
            {"skill_md": "---\nname: demo2\n---\n"},
        )

    # request_stage_transition: DRAFT -> REFINE is allowed.
    backend_main.apply_tool_effect(
        session,
        "request_stage_transition",
        {"target_stage": Stage.REFINE.value, "reason": "draft accepted"},
    )
    assert session.current_stage == Stage.REFINE.value


def test_extract_section_pulls_when_to_use(backend_main) -> None:
    md = (
        "---\nname: x\ndescription: d\n---\n\n"
        "## When to Use This Skill\nUse for calendar events.\n\n"
        "## When NOT to Use This Skill\nDo not use for email.\n"
    )
    assert backend_main._extract_section(md, ("when to use",), 500) == "Use for calendar events."
    assert backend_main._extract_section(md, ("when not to use", "do not use"), 400) == "Do not use for email."


def test_peer_skill_digest_uses_frontmatter_description(backend_main) -> None:
    # Schema v2 dropped dbo.skills.description; frontmatter is the only source.
    md = "---\nname: cal\ndescription: frontmatter desc\n---\n\n## When to Use This Skill\nDo X.\n"
    digest = backend_main._peer_skill_digest("cal", md)
    assert digest["name"] == "cal"
    assert digest["description"] == "frontmatter desc"
    assert digest["when_to_use"] == "Do X."
    assert digest["granted"] is True


def test_kind_only_tools_reject_the_wrong_skill_kind(backend_main) -> None:
    capability = Session()
    scenario = Session(skill_kind=SkillKind.SCENARIO)

    with pytest.raises(ValueError, match="record_delegation is only available for a scenario skill"):
        backend_main.apply_tool_effect(capability, "record_delegation", {})
    with pytest.raises(ValueError, match="record_variables is only available for a capability skill"):
        backend_main.apply_tool_effect(scenario, "record_variables", {})


def _confirm_routing(backend_main, session) -> None:
    backend_main.apply_tool_effect(
        session,
        "update_prepare_checklist",
        {"item": "routing_uniqueness_confirmed", "confirmed": True, "evidence": "compared"},
    )


def test_routing_confirmation_is_refused_before_children_are_declared(backend_main) -> None:
    session = Session(skill_kind=SkillKind.SCENARIO)

    with pytest.raises(ValueError, match="declares no children"):
        _confirm_routing(backend_main, session)
    assert session.prepare_brief.verify_checklist["routing_uniqueness_confirmed"] is False


def test_routing_confirmation_passes_once_a_child_is_declared(backend_main) -> None:
    session = Session(skill_kind=SkillKind.SCENARIO, children=["hr-leave-system"])

    _confirm_routing(backend_main, session)

    assert session.prepare_brief.verify_checklist["routing_uniqueness_confirmed"] is True


def test_children_guard_leaves_capability_sessions_alone(backend_main) -> None:
    session = Session()

    _confirm_routing(backend_main, session)

    assert session.prepare_brief.verify_checklist["routing_uniqueness_confirmed"] is True


def test_children_guard_only_covers_the_routing_checkpoint(backend_main) -> None:
    session = Session(skill_kind=SkillKind.SCENARIO)

    backend_main.apply_tool_effect(
        session,
        "update_prepare_checklist",
        {"item": "definition_clear", "confirmed": True, "evidence": "agreed"},
    )

    assert session.prepare_brief.verify_checklist["definition_clear"] is True


def test_children_guard_never_blocks_unconfirming(backend_main) -> None:
    session = Session(skill_kind=SkillKind.SCENARIO)
    session.prepare_brief.verify_checklist["routing_uniqueness_confirmed"] = True

    backend_main.apply_tool_effect(
        session,
        "update_prepare_checklist",
        {"item": "routing_uniqueness_confirmed", "confirmed": False},
    )

    assert session.prepare_brief.verify_checklist["routing_uniqueness_confirmed"] is False


def test_declared_children_are_dropped_from_the_loaded_peer_list(backend_main) -> None:
    # _load_peer_skill_boundaries only runs on PREPARE entry, so children
    # declared afterwards have to be filtered out of the list it left behind.
    session = Session(skill_kind=SkillKind.SCENARIO, children=["hr-leave-system"])
    session.prepare_brief.research.peer_skills = [
        {"name": "hr-leave-system", "description": "child"},
        {"name": "calendar-reader", "description": "peer"},
    ]

    removed = backend_main._drop_declared_children_from_peers(session)

    assert removed == ["hr-leave-system"]
    assert [p["name"] for p in session.prepare_brief.research.peer_skills] == ["calendar-reader"]


def test_dropping_children_from_peers_is_a_noop_when_none_match(backend_main) -> None:
    session = Session(skill_kind=SkillKind.SCENARIO, children=["hr-leave-system"])
    session.prepare_brief.research.peer_skills = [{"name": "calendar-reader"}]

    assert backend_main._drop_declared_children_from_peers(session) == []
    assert [p["name"] for p in session.prepare_brief.research.peer_skills] == ["calendar-reader"]
