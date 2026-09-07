from __future__ import annotations

from backend.models import Delegation, SkillFiles


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