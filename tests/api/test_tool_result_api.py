from __future__ import annotations

import pytest

from backend.models import IterationReflection, PendingToolCall


def test_accept_propose_patch_updates_content_and_patch_history(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "hello"
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_patch",
            tool="propose_patch",
            args={
                "target_file": "SKILL.md",
                "patch": """*** Begin Patch
*** Update File: SKILL.md
@@ hello
-hello
+hi
*** End Patch""",
                "reason": "shorten greeting",
            },
        )
    )
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_patch", "result": {"action": "accept"}},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["current_skill"]["skill_md"] == "hi"
    assert body["patch_history"][0]["content_before"] == "hello"
    assert body["patch_history"][0]["content_after"] == "hi"
    assert body["pending_tool_calls"] == []


def _accept_patch(client, session, *, call_id, old, new, addresses):
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id=call_id,
            tool="propose_patch",
            args={
                "target_file": "SKILL.md",
                "patch": f"""*** Begin Patch
*** Update File: SKILL.md
@@ {old}
-{old}
+{new}
*** End Patch""",
                "reason": "fix",
                "addresses": addresses,
            },
        )
    )
    return client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": call_id, "result": {"action": "accept"}},
    )


def test_accepted_patch_reports_remaining_open_fix_items(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "one"
    session.iteration_reflections.append(
        IterationReflection(
            what_went_wrong=["a", "b", "c"],
            what_to_change=["fix one", "fix two", "fix three"],
            raw="reflection",
        )
    )
    backend_main.sessions[session.id] = session

    body = _accept_patch(
        client, session,
        call_id="p1", old="one", new="two", addresses=["fix one"],
    ).json()

    assert body["patch_history"][0]["addresses"] == ["fix one"]
    notes = [m for m in body["conversation"] if m["role"] == "system" and "fix item(s)" in m["content"]]
    assert notes, body["conversation"]
    assert "2 of 3 fix item(s)" in notes[-1]["content"]
    assert notes[-1]["metadata"]["open_fixes"] == ["fix two", "fix three"]

    _accept_patch(
        client, session,
        call_id="p2", old="two", new="three", addresses=["fix two"],
    )
    body = _accept_patch(
        client, session,
        call_id="p3", old="three", new="four", addresses=["fix three"],
    ).json()

    notes = [m for m in body["conversation"] if m["role"] == "system" and "fix item(s)" in m["content"]]
    assert "All 3 fix item(s) from the latest reflection are addressed" in notes[-1]["content"]


def test_accepted_patch_without_reflection_adds_no_open_fix_note(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "one"
    backend_main.sessions[session.id] = session

    body = _accept_patch(
        client, session,
        call_id="p1", old="one", new="two", addresses=[],
    ).json()

    assert not [m for m in body["conversation"] if "fix item(s)" in m["content"]]


def test_tool_result_missing_call_returns_404(client, backend_main) -> None:
    session = backend_main.Session()
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "missing", "result": {"action": "accept"}},
    )

    assert response.status_code == 404


def test_tool_result_patch_failure_returns_recovery_detail(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "hello"
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_patch",
            tool="propose_patch",
            args={
                "target_file": "SKILL.md",
                "patch": """*** Begin Patch
*** Update File: SKILL.md
@@ missing
-missing
+hi
*** End Patch""",
            },
        )
    )
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_patch", "result": {"action": "accept"}},
    )

    session_after = backend_main.sessions[session.id]
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "anchor_not_found"
    assert session_after.conversation[-1].metadata["patch_error"]["recoverable"] is True


def test_scenario_patch_cannot_remove_children(client, backend_main) -> None:
    session = backend_main.Session(skill_kind="scenario", current_stage="refine")
    session.current_skill.skill_md = (
        "---\nname: parent\nmetadata:\n  skill_type: scenario-orchestration\n"
        "  children: [child]\n---\n\n# Parent\n"
    )
    before = session.current_skill.skill_md
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_topology_patch",
            tool="propose_patch",
            args={
                "target_file": "SKILL.md",
                "patch": """*** Begin Patch
*** Update File: SKILL.md
@@
-  children: [child]
*** End Patch""",
            },
        )
    )
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_topology_patch", "result": {"action": "accept"}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "topology_violation"
    assert backend_main.sessions[session.id].current_skill.skill_md == before


def test_undo_only_latest_patch(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "second"
    first = backend_main.PatchRecord(
        target_file="SKILL.md",
        v4a_patch="patch 1",
        content_before="first-before",
        content_after="first-after",
        version_hash_before="hash-1",
    )
    second = backend_main.PatchRecord(
        target_file="SKILL.md",
        v4a_patch="patch 2",
        content_before="before-second",
        content_after="second",
        version_hash_before="hash-2",
    )
    session.patch_history.extend([first, second])
    backend_main.sessions[session.id] = session

    old_patch_response = client.post(f"/api/sessions/{session.id}/patches/{first.id}/undo")
    latest_patch_response = client.post(f"/api/sessions/{session.id}/patches/{second.id}/undo")

    assert old_patch_response.status_code == 409
    assert latest_patch_response.status_code == 200
    assert latest_patch_response.json()["current_skill"]["skill_md"] == "before-second"


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Does a thing.\n---\n\n## Overview\n\nBody.\n"


def _persisted_refine_session(backend_main, grant_skill, name: str, *, mode: str = "new"):
    from backend.models import SkillFiles

    files = backend_main.store.save_skill(SkillFiles(name=name, skill_md=_skill_md(name), version_hash=""))
    grant_skill(name)
    session = backend_main.Session(current_stage="refine", mode=mode)
    session.current_skill.skill_md = files.skill_md
    session.current_skill.version_hash = files.version_hash
    session.remote_skill_id = files.name
    session.target_skill_id = files.name
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_rename",
            tool="rename_skill",
            args={"new_name": "new-skill", "reason": "user asked for a rename"},
        )
    )
    backend_main.sessions[session.id] = session
    return session


def test_accept_rename_skill_moves_blob_sql_and_grant(client, backend_main, grant_skill, fake_sql) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    body = response.json()
    assert response.status_code == 200
    assert "name: new-skill" in body["current_skill"]["skill_md"]
    assert body["remote_skill_id"] == "new-skill"
    assert body["patch_history"][0]["content_after"] == body["current_skill"]["skill_md"]

    assert backend_main.store.load_skill("new-skill").skill_md == body["current_skill"]["skill_md"]
    with pytest.raises(Exception):
        backend_main.store.load_skill("old-skill")
    assert "new-skill" in fake_sql.skills and "old-skill" not in fake_sql.skills
    assert ("test@example.com", "new-skill") in fake_sql.grants


def test_rename_skill_works_in_modify_mode(client, backend_main, grant_skill, fake_sql) -> None:
    # Regression: MODIFY used to pin the name to target_skill_id, so the rename
    # applied locally and then failed every sync forever.
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill", mode="modify")

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    assert response.json()["remote_skill_id"] == "new-skill"
    assert "old-skill" not in fake_sql.skills


def test_rename_skill_preserves_is_internal(client, backend_main, grant_skill, fake_sql) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")
    fake_sql.skills["old-skill"]["is_internal"] = True

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    assert fake_sql.skills["new-skill"]["is_internal"] is True


def test_rename_skill_preserves_is_public(client, backend_main, grant_skill, fake_sql) -> None:
    # The new name has no row yet, so visibility must be read off the row being
    # renamed -- otherwise a public skill silently drops back to private.
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")
    fake_sql.skills["old-skill"]["is_public"] = True

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    assert fake_sql.skills["new-skill"]["is_public"] is True


def test_rename_skill_migrates_every_grant(client, backend_main, grant_skill, fake_sql) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")
    fake_sql.add_grant("old-skill", "colleague@example.com", granted_by="test@example.com")

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    assert ("colleague@example.com", "new-skill") in fake_sql.grants
    assert ("test@example.com", "new-skill") in fake_sql.grants
    assert [key for key in fake_sql.grants if key[1] == "old-skill"] == []


def test_rename_skill_keeps_old_skill_when_grants_cannot_move(
    client, backend_main, grant_skill, fake_sql, monkeypatch
) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")

    def _unavailable(*args, **kwargs):
        raise RuntimeError("grant store offline")

    monkeypatch.setattr(backend_main.skills_repo, "list_grants", _unavailable)

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    body = response.json()
    assert response.status_code == 200
    assert "old-skill" in fake_sql.skills
    assert ("test@example.com", "old-skill") in fake_sql.grants
    assert backend_main.store.load_skill("old-skill") is not None
    assert any("grants could not be moved" in m["content"] for m in body["conversation"])


def test_rename_skill_onto_existing_name_is_refused_and_rolled_back(
    client, backend_main, grant_skill, fake_sql
) -> None:
    from backend.models import SkillFiles

    taken = backend_main.store.save_skill(
        SkillFiles(name="new-skill", skill_md=_skill_md("new-skill"), version_hash="")
    )
    grant_skill("new-skill")
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill")
    before = session.current_skill.skill_md

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "rename_conflict"
    session_after = backend_main.sessions[session.id]
    assert session_after.current_skill.skill_md == before
    assert session_after.patch_history == []
    assert backend_main.store.load_skill("new-skill").skill_md == taken.skill_md
    assert backend_main.store.load_skill("old-skill").skill_md == before


def _draft_session(backend_main, name: str):
    session = backend_main.Session(current_stage="draft", mode="new")
    session.current_skill.skill_md = _skill_md(name)
    session.current_skill.version_hash = "draft-hash"
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_draft",
            tool="propose_skill_draft",
            args={"skill_md": session.current_skill.skill_md},
        )
    )
    backend_main.sessions[session.id] = session
    return session


def _accept_draft(client, session, name):
    return client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_draft", "result": {"action": "accept", "name": name}},
    )


def test_accept_draft_with_unchanged_name_saves_as_proposed(client, backend_main, fake_sql) -> None:
    session = _draft_session(backend_main, "model-chosen-skill")

    response = _accept_draft(client, session, "model-chosen-skill")

    assert response.status_code == 200
    assert response.json()["remote_skill_id"] == "model-chosen-skill"
    assert "model-chosen-skill" in fake_sql.skills


def test_accept_draft_with_edited_name_rewrites_frontmatter(client, backend_main, fake_sql) -> None:
    session = _draft_session(backend_main, "model-chosen-skill")

    response = _accept_draft(client, session, "user-chosen-skill")

    body = response.json()
    assert response.status_code == 200
    assert "name: user-chosen-skill" in body["current_skill"]["skill_md"]
    assert body["remote_skill_id"] == "user-chosen-skill"
    assert backend_main.store.load_skill("user-chosen-skill").skill_md == body["current_skill"]["skill_md"]
    assert "user-chosen-skill" in fake_sql.skills and "model-chosen-skill" not in fake_sql.skills


@pytest.mark.parametrize("bad", ["Bad Name", "-leading", "trailing-", "under_score", "x" * 65])
def test_accept_draft_rejects_invalid_edited_name(client, backend_main, fake_sql, bad) -> None:
    session = _draft_session(backend_main, "model-chosen-skill")
    before = session.current_skill.skill_md

    response = _accept_draft(client, session, bad)

    assert response.status_code == 400
    assert response.json()["detail"]["kind"] == "invalid_skill_name"
    assert backend_main.sessions[session.id].current_skill.skill_md == before
    assert fake_sql.skills == {}


def test_accept_draft_rejects_edited_name_that_already_exists(
    client, backend_main, grant_skill, fake_sql
) -> None:
    from backend.models import SkillFiles

    backend_main.store.save_skill(SkillFiles(name="taken-skill", skill_md=_skill_md("taken-skill"), version_hash=""))
    grant_skill("taken-skill")
    session = _draft_session(backend_main, "model-chosen-skill")
    before = session.current_skill.skill_md

    response = _accept_draft(client, session, "taken-skill")

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "skill_name_conflict"
    assert backend_main.sessions[session.id].current_skill.skill_md == before
    assert backend_main.store.load_skill("taken-skill").skill_md == _skill_md("taken-skill")
