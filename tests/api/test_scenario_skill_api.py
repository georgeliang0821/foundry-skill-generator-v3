from __future__ import annotations

import pytest

from backend.models import Delegation, InputBinding, Session, SkillFiles


CHILD_MD = "---\nname: child-skill\ndescription: Child capability\n---\n\n# Child\n"
PEER_MD = "---\nname: peer-skill\ndescription: Child capability\n---\n\n# Peer\n"
PARENT_MD = (
    "---\n"
    "name: parent-skill\n"
    "description: Parent scenario\n"
    "metadata:\n"
    "  skill_type: scenario-orchestration\n"
    "  children: [child-skill]\n"
    "---\n\n"
    "# Parent\n"
)
PARENT_WITH_DEPENDENCY_MD = PARENT_MD.replace(
    "children: [child-skill]", "children: [child-skill, peer-skill]"
)


def _seed_skill(backend_main, name: str, skill_md: str) -> None:
    backend_main.store.save_skill(SkillFiles(name=name, skill_md=skill_md))


def _delegate(backend_main, session_id: str, *children: str) -> None:
    session = backend_main.sessions[session_id]
    session.prepare_brief.delegation = [Delegation(child_skill=c) for c in children]


def _scenario_session(client, backend_main) -> str:
    response = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    )
    assert response.status_code == 200
    session_id = response.json()["id"]
    session = backend_main.sessions[session_id]
    session.children = ["child-skill"]
    session.child_full_md = {"child-skill": CHILD_MD}
    session.current_skill.skill_md = PARENT_MD
    return session_id


def test_create_scenario_session_uses_scenario_checklist(client) -> None:
    response = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["skill_kind"] == "scenario"
    assert set(body["prepare_brief"]["verify_checklist"]) == {
        "definition_clear",
        "routing_uniqueness_confirmed",
        "delegation_ok",
    }


def test_declaring_children_drops_them_from_the_overlap_table(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    grant_skill("child-skill")
    grant_skill("peer-skill")
    from backend.skills_index import get_skills_index

    get_skills_index().invalidate()
    session_id = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    ).json()["id"]
    # Let PREPARE-entry finish first, otherwise it overwrites the overlap table
    # that declaring the child is supposed to have just narrowed.
    backend_main.await_prepare_entry()
    backend_main.sessions[session_id].prepare_brief.understanding.skill_goal = "child peer capability"

    declared = client.post(
        f"/api/sessions/{session_id}/children",
        json={"children": ["child-skill"]},
    )

    assert declared.status_code == 200
    overlaps = declared.json()["prepare_brief"]["research"]["existing_skills_overlap"]
    assert {item["name"] for item in overlaps} == {"peer-skill"}


def test_modify_infers_scenario_kind_from_two_matching_signals(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "parent-skill", PARENT_MD)
    grant_skill("parent-skill")

    response = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    )

    assert response.status_code == 200
    assert response.json()["skill_kind"] == "scenario"
    assert response.json()["children"] == ["child-skill"]


def test_modify_rejects_inconsistent_scenario_signals(client, backend_main, grant_skill) -> None:
    invalid = PARENT_MD.replace("  skill_type: scenario-orchestration\n", "")
    _seed_skill(backend_main, "parent-skill", invalid)
    grant_skill("parent-skill")

    response = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    )

    assert response.status_code == 400
    assert "skill_type" in response.json()["detail"]


def test_scenario_save_marks_child_internal(client, backend_main, grant_skill, fake_sql) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    grant_skill("child-skill")
    session_id = _scenario_session(client, backend_main)
    _delegate(backend_main, session_id, "child-skill")

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 200
    assert fake_sql.skills["child-skill"]["is_internal"] is True
    assert fake_sql.skills["parent-skill"]["is_internal"] is False


def test_scenario_save_leaves_a_plain_dependency_visible(
    client, backend_main, grant_skill, fake_sql
) -> None:
    """`children` is a whitelist, so being listed is not a reason to hide a skill."""
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    grant_skill("child-skill")
    grant_skill("peer-skill")
    fake_sql.upsert_skill("peer-skill")
    session_id = _scenario_session(client, backend_main)
    session = backend_main.sessions[session_id]
    session.children = ["child-skill", "peer-skill"]
    session.current_skill.skill_md = PARENT_WITH_DEPENDENCY_MD
    _delegate(backend_main, session_id, "child-skill")

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 200
    assert fake_sql.skills["child-skill"]["is_internal"] is True
    assert fake_sql.skills["peer-skill"]["is_internal"] is False


def test_scenario_save_rejects_a_pointer_at_an_undeclared_skill(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    grant_skill("child-skill")
    session_id = _scenario_session(client, backend_main)
    backend_main.sessions[session_id].current_skill.skill_md = PARENT_MD + (
        "\n## Step 1\n"
        "These skills are absent from `list_skills` and may still be fetched.\n"
        'fetch_skill(skill_name="peer-skill", sections="Overview")\n'
    )

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 400
    assert "P1" in response.json()["detail"]


def test_removing_a_dependency_that_was_never_internal_is_silent(
    client, backend_main, grant_skill, fake_sql
) -> None:
    """No state changed, so there is nothing to ask the user to resolve."""
    old_parent = PARENT_WITH_DEPENDENCY_MD
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    _seed_skill(backend_main, "parent-skill", old_parent)
    for name in ("child-skill", "peer-skill", "parent-skill"):
        grant_skill(name)
    fake_sql.upsert_skill("child-skill", is_internal=True)
    fake_sql.upsert_skill("peer-skill")

    session_id = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    ).json()["id"]
    session = backend_main.sessions[session_id]
    session.current_skill.skill_md = PARENT_MD
    session.children = ["child-skill"]
    session.child_full_md = {"child-skill": CHILD_MD}
    _delegate(backend_main, session_id, "child-skill")

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 200
    assert fake_sql.skills["peer-skill"]["is_internal"] is False


@pytest.mark.parametrize("action", [None, "make_capability"])
def test_removing_shared_internal_child_preserves_internal(
    client, backend_main, grant_skill, fake_sql, action
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    _seed_skill(backend_main, "parent-skill", PARENT_WITH_DEPENDENCY_MD)
    _seed_skill(
        backend_main, "other-parent", PARENT_MD.replace("parent-skill", "other-parent")
    )
    for name in ("child-skill", "peer-skill", "parent-skill"):
        grant_skill(name)
    fake_sql.upsert_skill("child-skill", is_internal=True)
    fake_sql.upsert_skill("other-parent", created_by="other@example.com")
    session_id = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    ).json()["id"]
    session = backend_main.sessions[session_id]
    session.current_skill.skill_md = PARENT_MD.replace("child-skill", "peer-skill")
    session.children = ["peer-skill"]
    session.child_full_md = {"peer-skill": PEER_MD}

    response = client.post(
        f"/api/sessions/{session_id}/save", json={"orphan_action": action}
    )

    assert response.status_code == 200
    assert fake_sql.skills["child-skill"]["is_internal"] is True


def test_removing_internal_child_fails_closed_on_incomplete_scan(
    client, backend_main, grant_skill, fake_sql
) -> None:
    for name, content in (
        ("child-skill", CHILD_MD), ("peer-skill", PEER_MD),
        ("parent-skill", PARENT_WITH_DEPENDENCY_MD),
    ):
        _seed_skill(backend_main, name, content)
        grant_skill(name)
    fake_sql.upsert_skill("child-skill", is_internal=True)
    fake_sql.upsert_skill("missing-parent", created_by="other@example.com")
    session_id = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    ).json()["id"]
    session = backend_main.sessions[session_id]
    session.current_skill.skill_md = PARENT_MD.replace("child-skill", "peer-skill")
    session.children = ["peer-skill"]
    session.child_full_md = {"peer-skill": PEER_MD}

    response = client.post(
        f"/api/sessions/{session_id}/save", json={"orphan_action": "make_capability"}
    )

    assert response.status_code == 503
    assert backend_main.store.load_skill("parent-skill").skill_md == PARENT_WITH_DEPENDENCY_MD
    assert fake_sql.skills["child-skill"]["is_internal"] is True


def test_scenario_save_denied_child_writes_nothing(client, backend_main, fake_sql) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    fake_sql.upsert_skill("child-skill")
    session_id = _scenario_session(client, backend_main)

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 403
    assert "parent-skill" not in fake_sql.skills
    assert all(entry.name != "parent-skill" for entry in backend_main.store.list_skills())


def test_scenario_save_rejects_scalar_children(client, backend_main, grant_skill) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    grant_skill("child-skill")
    session_id = _scenario_session(client, backend_main)
    backend_main.sessions[session_id].current_skill.skill_md = PARENT_MD.replace(
        "children: [child-skill]", "children: child-skill"
    )

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 400
    assert "T4" in response.json()["detail"]


def test_load_session_children_populates_transient_content(client, backend_main) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    session_id = _scenario_session(client, backend_main)
    backend_main.sessions[session_id].child_full_md = {}

    response = client.post(f"/api/sessions/{session_id}/children")

    assert response.status_code == 200
    assert backend_main.sessions[session_id].child_full_md["child-skill"] == CHILD_MD


def _scenario_with_many_children(client, backend_main, grant_skill, count: int, owner: str = "host") -> Session:
    session_id = _scenario_session(client, backend_main)
    backend_main.await_prepare_entry()
    session = backend_main.sessions[session_id]
    session.orchestration_owner = owner
    session.children = [f"child-{index:02d}" for index in range(count)]
    for name in session.children:
        md = CHILD_MD.replace("child-skill", name) + (
            "\n## Required Inputs\n```input-bindings\n"
            "- name: description\n  source: request\n```\n"
            f"\n## Error Handling\nContract details for {name}.\n"
        )
        _seed_skill(backend_main, name, md)
        grant_skill(name)
    session.child_full_md = {}
    session.prepare_brief.delegation = [
        Delegation(
            child_skill=name,
            input_bindings=[InputBinding(name="description", source="request")],
            host_capabilities=["Collect the user's request"] if owner == "host" else [],
        )
        for name in session.children
    ]
    session.current_skill.skill_md = PARENT_MD.replace(
        "children: [child-skill]", f"children: [{', '.join(session.children)}]"
    )
    if owner == "eaa":
        session.current_skill.skill_md = session.current_skill.skill_md.replace(
            "metadata:\n", "metadata:\n  orchestration_owner: eaa\n"
        )
    brief = session.prepare_brief
    brief.understanding.skill_goal = "Coordinate the declared capabilities."
    brief.understanding.key_capabilities = ["Coordinate work"]
    brief.understanding.differentiation = "Handle the combined workflow."
    brief.research.summary = "No competing scenario."
    brief.research.web_status = "ok"
    for key in brief.verify_checklist:
        brief.verify_checklist[key] = True
    return session


@pytest.mark.parametrize("count", [1, 5, 6, 8, 11])
@pytest.mark.parametrize("owner", ["host", "eaa"])
def test_all_children_are_loaded_before_prepare_to_draft(
    client, backend_main, grant_skill, count, owner
) -> None:
    from backend.state_machine import build_system_prompt, check_quality_gates

    session = _scenario_with_many_children(client, backend_main, grant_skill, count, owner)
    session.prepare_brief.delegation.reverse()
    response = client.post(f"/api/sessions/{session.id}/children")

    assert response.status_code == 200
    assert list(session.child_full_md) == list(reversed(session.children))
    assert set(session.child_sections) == set(session.children)
    assert "child_full_md" not in response.json()
    assert check_quality_gates(session) == []
    backend_main.apply_tool_effect(session, "update_prepare_checklist", {
        "item": "delegation_ok", "confirmed": True,
    })
    backend_main.apply_tool_effect(session, "stage_transition", {"target": "draft"})
    assert session.current_stage == "draft"

    prompt = build_system_prompt(session)
    assert "\n<<<BEGIN SKILL.md: " not in prompt
    for name in session.children:
        assert f"<<<BEGIN CHILD SECTION: {name} / Required Inputs>>>" in prompt
        assert f"- **{name}**:" in prompt


@pytest.mark.parametrize("count", [6, 8, 11])
@pytest.mark.parametrize("owner", ["host", "eaa"])
def test_save_validates_input_contracts_for_every_child(
    client, backend_main, grant_skill, count, owner
) -> None:
    session = _scenario_with_many_children(client, backend_main, grant_skill, count, owner)

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 200, response.json()
    assert set(session.child_full_md) == set(session.children)


@pytest.mark.parametrize(("problem", "expected"), [
    ("missing", "children.unresolved:child-07"),
    ("name", "children.name_mismatch:child-07"),
    ("nested", "children.nested:child-07"),
    ("bindings", "child-07: delegation input sources must match the child's Required Inputs."),
])
def test_invalid_child_beyond_prompt_limit_still_blocks_prepare(
    client, backend_main, grant_skill, problem, expected
) -> None:
    from backend.state_machine import QualityGateError, check_quality_gates

    session = _scenario_with_many_children(client, backend_main, grant_skill, 8)
    child = session.children[-1]
    if problem == "missing":
        backend_main.store.delete_skill(child)
    elif problem == "bindings":
        session.prepare_brief.delegation[-1].input_bindings = [
            InputBinding(name="other", source="request")
        ]
    else:
        md = backend_main.store.load_skill(child).skill_md
        if problem == "name":
            md = md.replace(f"name: {child}", "name: wrong-child")
        else:
            md = md.replace("description:", "metadata:\n  children: [grandchild]\ndescription:")
        _seed_skill(backend_main, child, md)

    assert client.post(f"/api/sessions/{session.id}/children").status_code == 200
    assert expected in check_quality_gates(session)
    with pytest.raises(QualityGateError) as exc:
        backend_main.apply_tool_effect(session, "stage_transition", {"target": "draft"})
    assert expected in exc.value.missing
    assert session.current_stage == "prepare"
    if problem in {"missing", "nested", "bindings"}:
        response = client.post(f"/api/sessions/{session.id}/save", json={})
        assert response.status_code == 400
        assert not any(skill.name == "parent-skill" for skill in backend_main.store.list_skills())


def test_save_refreshes_selected_child_before_topology_validation(
    client, backend_main, grant_skill
) -> None:
    session = _scenario_with_many_children(client, backend_main, grant_skill, 8)
    assert client.post(f"/api/sessions/{session.id}/children").status_code == 200
    child = session.children[-1]
    selected_md = backend_main.store.load_skill(child).skill_md.replace(
        "description:", "metadata:\n  children: [grandchild]\ndescription:"
    ).replace("## Error Handling", "## Selected Version Errors")
    proposed = client.post(
        f"/api/sessions/{session.id}/neighbor-edits/{child}/propose",
        json={"skill_md": selected_md, "label": "Selected child version"},
    )
    assert proposed.status_code == 200

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 400
    assert "T7" in response.json()["detail"]
    assert session.child_full_md[child] == selected_md
    assert "Selected Version Errors" in session.child_sections[child]


def test_update_session_children_deduplicates_and_loads(client, backend_main, grant_skill) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    grant_skill("child-skill")
    session_id = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    ).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/children",
        json={"children": ["child-skill", "child-skill"]},
    )

    assert response.status_code == 200
    assert response.json()["children"] == ["child-skill"]
    assert backend_main.sessions[session_id].child_full_md["child-skill"] == CHILD_MD


@pytest.mark.parametrize("owner", ["host", "eaa"])
def test_record_delegation_preserves_whitelist_and_other_child_contracts(
    client, backend_main, grant_skill, owner
) -> None:
    from backend.state_machine import build_system_prompt

    skills = {
        "child-skill": CHILD_MD,
        "peer-skill": PEER_MD,
        "publisher-skill": CHILD_MD.replace("child-skill", "publisher-skill"),
    }
    for name, md in skills.items():
        _seed_skill(backend_main, name, md)
        grant_skill(name)
    session_id = _scenario_session(client, backend_main)
    backend_main.await_prepare_entry()
    session = backend_main.sessions[session_id]
    session.orchestration_owner = owner
    response = client.post(
        f"/api/sessions/{session_id}/children",
        json={"children": list(skills)},
    )
    assert response.status_code == 200

    for name in skills:
        backend_main.apply_tool_effect(session, "record_delegation", {
            "delegation": [{"child_skill": name, "operations": [f"run-{name}"]}],
        })
        assert session.children == list(skills)
        assert session.child_full_md == skills
        prompt = build_system_prompt(session)
        for child, md in skills.items():
            assert f"### {child}\nversion_hash:" in prompt
        assert "Never request their SKILL.md through `request_materials`." in prompt

    session.prepare_brief.verify_checklist["delegation_ok"] = True
    session.prepare_brief.verify_evidence["delegation_ok"] = "confirmed"
    backend_main.apply_tool_effect(session, "record_delegation", {
        "delegation": [{"child_skill": "child-skill", "handshakes": ["stop on failure"]}],
    })
    contracts = {d.child_skill: d for d in session.prepare_brief.delegation}
    assert list(contracts) == list(skills)
    assert contracts["child-skill"].operations == ["run-child-skill"]
    assert contracts["child-skill"].handshakes == ["stop on failure"]
    assert contracts["peer-skill"].operations == ["run-peer-skill"]
    assert contracts["publisher-skill"].operations == ["run-publisher-skill"]
    assert session.prepare_brief.verify_checklist["delegation_ok"] is False
    assert "delegation_ok" not in session.prepare_brief.verify_evidence

    backend_main.persist_session(session)
    restored = backend_main.session_store.load_all()[session_id]
    assert restored.children == list(skills)
    assert restored.prepare_brief.delegation == session.prepare_brief.delegation

    backend_main.apply_tool_effect(session, "record_delegation", {
        "delegation": [{"child_skill": "child-skill", "operations": []}],
    })
    contracts = {d.child_skill: d for d in session.prepare_brief.delegation}
    assert contracts["child-skill"].operations == []
    assert contracts["child-skill"].handshakes == ["stop on failure"]
    assert contracts["peer-skill"].operations == ["run-peer-skill"]
    assert session.children == list(skills)


def test_record_delegation_adds_dependencies_without_clearing_existing_state(
    client, backend_main
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    session_id = _scenario_session(client, backend_main)
    backend_main.await_prepare_entry()
    session = backend_main.sessions[session_id]
    _delegate(backend_main, session_id, "child-skill")
    session.prepare_brief.verify_checklist["delegation_ok"] = True
    session.prepare_brief.verify_evidence["delegation_ok"] = "confirmed"

    backend_main.apply_tool_effect(session, "record_delegation", {
        "delegation": [], "dependency_skills": ["peer-skill", "peer-skill"],
    })
    assert session.children == ["child-skill", "peer-skill"]
    assert [d.child_skill for d in session.prepare_brief.delegation] == ["child-skill"]
    assert set(session.child_full_md) == {"child-skill", "peer-skill"}
    assert session.prepare_brief.verify_checklist["delegation_ok"] is False
    assert "delegation_ok" not in session.prepare_brief.verify_evidence

    session.prepare_brief.verify_checklist["delegation_ok"] = True
    backend_main.apply_tool_effect(session, "record_delegation", {"delegation": []})
    assert session.children == ["child-skill", "peer-skill"]
    assert session.prepare_brief.verify_checklist["delegation_ok"] is True


def test_children_update_removes_contracts_without_later_resurrection(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "peer-skill", PEER_MD)
    grant_skill("child-skill")
    grant_skill("peer-skill")
    session_id = _scenario_session(client, backend_main)
    backend_main.await_prepare_entry()
    session = backend_main.sessions[session_id]
    session.children = ["child-skill", "peer-skill"]
    _delegate(backend_main, session_id, "child-skill", "peer-skill")
    session.prepare_brief.verify_checklist["delegation_ok"] = True
    session.prepare_brief.verify_evidence["delegation_ok"] = "confirmed"

    response = client.post(
        f"/api/sessions/{session_id}/children", json={"children": ["child-skill"]},
    )
    assert response.status_code == 200
    assert [d.child_skill for d in session.prepare_brief.delegation] == ["child-skill"]
    assert set(session.child_full_md) == {"child-skill"}
    assert session.prepare_brief.verify_checklist["delegation_ok"] is False
    assert "delegation_ok" not in session.prepare_brief.verify_evidence
    backend_main.apply_tool_effect(session, "record_delegation", {
        "delegation": [{"child_skill": "child-skill", "operations": ["read"]}],
    })
    assert session.children == ["child-skill"]

    session.prepare_brief.verify_checklist["delegation_ok"] = True
    session.prepare_brief.verify_evidence["delegation_ok"] = "still confirmed"
    response = client.post(
        f"/api/sessions/{session_id}/children", json={"children": ["child-skill"]},
    )
    assert response.status_code == 200
    assert session.prepare_brief.verify_checklist["delegation_ok"] is True
    assert session.prepare_brief.verify_evidence["delegation_ok"] == "still confirmed"

    response = client.post(
        f"/api/sessions/{session_id}/children", json={"children": []},
    )
    assert response.status_code == 200
    assert session.children == []
    assert session.prepare_brief.delegation == []
    assert session.child_full_md == {}


def test_routing_checkpoint_is_refused_before_children_are_declared(client, backend_main) -> None:
    session_id = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    ).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/checklist",
        json={"item": "routing_uniqueness_confirmed", "status": "confirmed"},
    )

    assert response.status_code == 400
    assert "declares no children" in response.json()["detail"]
    checks = backend_main.sessions[session_id].prepare_brief.verify_checklist
    assert checks["routing_uniqueness_confirmed"] is False


def test_routing_checkpoint_passes_once_children_are_declared(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    grant_skill("child-skill")
    session_id = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    ).json()["id"]
    client.post(f"/api/sessions/{session_id}/children", json={"children": ["child-skill"]})

    response = client.post(
        f"/api/sessions/{session_id}/checklist",
        json={"item": "routing_uniqueness_confirmed", "status": "confirmed"},
    )

    assert response.status_code == 200
    checks = response.json()["prepare_brief"]["verify_checklist"]
    assert checks["routing_uniqueness_confirmed"] is True


def test_declaring_children_removes_them_from_the_loaded_peer_list(
    client, backend_main, grant_skill
) -> None:
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    grant_skill("child-skill")
    session_id = client.post(
        "/api/sessions",
        json={"mode": "new", "skill_kind": "scenario", "materials": []},
    ).json()["id"]
    backend_main.await_prepare_entry()
    research = backend_main.sessions[session_id].prepare_brief.research
    research.peer_skills = [{"name": "child-skill"}, {"name": "peer-skill"}]

    response = client.post(
        f"/api/sessions/{session_id}/children",
        json={"children": ["child-skill"]},
    )

    assert response.status_code == 200
    peers = response.json()["prepare_brief"]["research"]["peer_skills"]
    assert [p["name"] for p in peers] == ["peer-skill"]


def test_topology_endpoint_returns_per_rule_failure(client, backend_main) -> None:
    session_id = _scenario_session(client, backend_main)
    backend_main.sessions[session_id].current_skill.skill_md = PARENT_MD.replace(
        "children: [child-skill]", "children: child-skill"
    )

    response = client.get(f"/api/sessions/{session_id}/topology")

    assert response.status_code == 200
    assert response.json()["valid"] is False
    results = {item["rule"]: item for item in response.json()["results"]}
    assert results["T4"]["passed"] is False
    assert results["T4"]["issues"]


def test_removed_child_requires_resolution_and_can_become_capability(
    client, backend_main, grant_skill, fake_sql
) -> None:
    removed_md = CHILD_MD.replace("child-skill", "removed-child")
    old_parent = PARENT_MD.replace(
        "children: [child-skill]", "children: [child-skill, removed-child]"
    )
    _seed_skill(backend_main, "child-skill", CHILD_MD)
    _seed_skill(backend_main, "removed-child", removed_md)
    _seed_skill(backend_main, "parent-skill", old_parent)
    for name in ("child-skill", "removed-child", "parent-skill"):
        grant_skill(name)
    fake_sql.upsert_skill("child-skill", is_internal=True)
    fake_sql.upsert_skill("removed-child", is_internal=True)

    created = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "parent-skill", "materials": []},
    )
    session_id = created.json()["id"]
    session = backend_main.sessions[session_id]
    session.current_skill.skill_md = PARENT_MD
    session.children = ["child-skill"]
    session.child_full_md = {"child-skill": CHILD_MD}
    _delegate(backend_main, session_id, "child-skill")

    warning = client.post(f"/api/sessions/{session_id}/save", json={})

    assert warning.status_code == 409
    assert warning.json()["detail"]["kind"] == "orphaned_children"
    assert warning.json()["detail"]["children"] == ["removed-child"]
    assert "removed-child" in backend_main.store.load_skill("parent-skill").skill_md

    saved = client.post(
        f"/api/sessions/{session_id}/save",
        json={"orphan_action": "make_capability"},
    )

    assert saved.status_code == 200
    assert fake_sql.skills["removed-child"]["is_internal"] is False
    assert "removed-child" not in backend_main.store.load_skill("parent-skill").skill_md