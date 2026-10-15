from __future__ import annotations

import pytest

from backend.models import IterationReflection, PendingToolCall


@pytest.mark.parametrize("content,patch,kind", [
    ("hello", "*** Begin Patch\n*** Update File: SKILL.md\n@@\n-missing anchor\n+hi\n*** End Patch", "anchor_not_found"),
    ("hello\nhello", "*** Begin Patch\n*** Update File: SKILL.md\n@@\n-hello\n+hi\n*** End Patch", "ambiguous_anchor"),
    ("hello", "*** Begin Patch\n*** Update File: SKILL.md\n@@\n-hello\n+hi\n*** Update File: SKILL.md\n@@\n-hi\n+bye\n*** End Patch", "patch_apply_failed"),
])
def test_failed_patch_retires_only_its_call(client, backend_main, content, patch, kind) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = content
    before = session.current_skill.model_dump()
    session.pending_tool_calls.extend([
        PendingToolCall(call_id="bad_patch", tool="propose_patch", args={
            "target_file": "SKILL.md",
            "patch": patch,
        }),
        PendingToolCall(call_id="other_patch", tool="propose_patch", args={}),
    ])
    backend_main.sessions[session.id] = session

    response = client.post(f"/api/sessions/{session.id}/tool-result", json={
        "tool_call_id": "bad_patch", "result": {"action": "accept"},
    })

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["kind"] == kind
    assert session.current_skill.model_dump() == before
    assert not session.patch_history
    assert [call.call_id for call in session.pending_tool_calls] == ["other_patch"]
    persisted = backend_main.session_store.load(session.id)
    assert [call.call_id for call in persisted.pending_tool_calls] == ["other_patch"]
    assert persisted.conversation[-1].metadata["patch_error"]["call_id"] == "bad_patch"
    repeated = client.post(f"/api/sessions/{session.id}/tool-result", json={
        "tool_call_id": "bad_patch", "result": {"action": "accept"},
    })
    assert repeated.status_code == 404


def test_named_reference_refine_patch_is_rejected_atomically(client, backend_main, fake_sql, monkeypatch) -> None:
    fake_sql.upsert_skill("hidden-calendar")
    session = backend_main.Session(current_stage="refine", remote_skill_id="demo")
    session.current_skill.skill_md = "---\nname: demo\ndescription: Filter rooms.\n---\n## Ground Rules\nOnly filter rooms."
    before = session.current_skill.model_dump()
    writes = []
    monkeypatch.setattr(backend_main, "save_skill_dual_write", lambda *args, **kwargs: writes.append(args))
    session.pending_tool_calls.append(PendingToolCall(
        call_id="named_patch", tool="propose_patch", args={
            "target_file": "SKILL.md",
            "patch": "*** Begin Patch\n*** Update File: SKILL.md\n@@\n-Only filter rooms.\n+Use hidden-calendar.\n*** End Patch",
        },
    ))
    backend_main.sessions[session.id] = session
    response = client.post(f"/api/sessions/{session.id}/tool-result", json={
        "tool_call_id": "named_patch", "result": {"action": "accept"},
    })
    assert response.status_code == 400, response.text
    assert "N1:" in response.text
    assert "hidden-calendar" not in response.text
    assert session.current_skill.model_dump() == before
    assert not session.patch_history
    assert not session.pending_change_summaries
    assert not writes


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
    assert body["conversation"][-1]["metadata"]["patch_sync"] == {"call_id": "call_patch", "status": "local"}
    assert body["pending_change_summaries"] == ["shorten greeting"]


def test_patch_sync_failure_retains_local_patch_and_reports_outcome(client, backend_main, monkeypatch) -> None:
    session = backend_main.Session(remote_skill_id="demo")
    session.current_skill.skill_md = "one"
    backend_main.sessions[session.id] = session
    detail = {
        "kind": "skill_lint_failed",
        "recoverable": True,
        "message": "Skill lint failed",
        "issues": ["D4: reserved secret"],
    }

    def reject_save(*args, **kwargs):
        raise backend_main.HTTPException(status_code=400, detail=detail)

    monkeypatch.setattr(backend_main, "save_skill_dual_write", reject_save)
    response = _accept_patch(
        client, session,
        call_id="blocked_patch", old="one", new="two", addresses=[],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_skill"]["skill_md"] == "two"
    assert len(body["patch_history"]) == 1
    outcome = body["conversation"][-1]["metadata"]["patch_sync"]
    assert outcome == {"call_id": "blocked_patch", "status": "failed", "detail": detail}
    assert body["pending_tool_calls"] == []

    assert body["pending_change_summaries"] == ["Update the skill instructions."]


def test_patch_sync_success_reports_saved(client, backend_main, monkeypatch) -> None:
    session = backend_main.Session(remote_skill_id="demo")
    session.current_skill.skill_md = "one"
    backend_main.sessions[session.id] = session
    monkeypatch.setattr(backend_main, "save_skill_dual_write", lambda *args, **kwargs: None)

    body = _accept_patch(
        client, session,
        call_id="saved_patch", old="one", new="two", addresses=[],
    ).json()

    assert body["conversation"][-1]["metadata"]["patch_sync"] == {"call_id": "saved_patch", "status": "saved"}


def test_patch_version_tracks_saved_content_and_undo_remains_local(client, backend_main, grant_skill) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    original = '---\nname: demo\ndescription: Demo\nmetadata:\n  version: "1.6"\n---\nOriginal'
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo", skill_md=original))
    grant_skill("demo")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo"}).json()["id"]
    session = backend_main.sessions[sid]
    response = _accept_patch(client, session, call_id="version_patch", old="Original", new="Changed", addresses=[])
    assert response.status_code == 200, response.text
    body = response.json()
    saved = original.replace("Original", "Changed").replace('"1.6"', '"1.6.1"').replace("metadata:\n", "metadata:\n  skill_type: inline\n")
    record = body["patch_history"][-1]
    assert record["content_after"] == saved
    assert record["version_hash_after"] == backend_main.version_hash(saved)
    assert body["current_skill"]["skill_md"] == saved
    assert body["conversation"][-1]["metadata"]["patch_sync"]["status"] == "saved"
    assert body["pending_change_summaries"] == []
    assert next(iter(local.history.values()))["manifest"]["change_summary"] == "Update the skill instructions."
    undone = client.post(f"/api/sessions/{sid}/patches/{record['id']}/undo")
    assert undone.status_code == 200
    assert undone.json()["current_skill"]["skill_md"] == original
    assert backend_main.store.load_skill("demo").skill_md == saved
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 200
    assert backend_main.store.load_skill("demo").skill_md == original.replace('"1.6"', '"1.6.2"').replace("metadata:\n", "metadata:\n  skill_type: inline\n")


def test_lint_rejected_patch_never_writes_a_snapshot(client, backend_main, grant_skill) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    original = '---\nname: demo\ndescription: Demo\nmetadata:\n  version: "1.6"\n---\nOriginal'
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo", skill_md=original))
    grant_skill("demo")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo"}).json()["id"]
    session = backend_main.sessions[sid]
    response = _accept_patch(
        client, session, call_id="lint_patch", old="Original", new='os.environ["OBO_CLIENT_SECRET"]', addresses=[],
    )
    assert response.status_code == 200, response.text
    outcome = response.json()["conversation"][-1]["metadata"]["patch_sync"]
    assert outcome["status"] == "failed"
    assert outcome["detail"]["kind"] == "skill_lint_failed"
    assert not local.history
    assert backend_main.store.load_skill("demo").skill_md == original
    assert 'version: "1.6"' in session.current_skill.skill_md


def test_pending_summaries_survive_retry_and_clear_after_save(client, backend_main, grant_skill, monkeypatch) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    original = "---\nname: demo\ndescription: Demo\n---\nOriginal"
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo", skill_md=original))
    grant_skill("demo")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo"}).json()["id"]
    session = backend_main.sessions[sid]

    def fail_save(*args, **kwargs):
        raise backend_main.HTTPException(status_code=503, detail="Unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(backend_main, "save_skill_dual_write", fail_save)
        for call_id, old, new in [("first", "Original", "Changed"), ("second", "Changed", "Final")]:
            response = _accept_patch(client, session, call_id=call_id, old=old, new=new, addresses=[])
            assert response.status_code == 200
    assert not local.history
    restored = backend_main.Session.model_validate(session.model_dump())
    assert restored.pending_change_summaries == ["Update the skill instructions."] * 2
    response = client.post(f"/api/sessions/{sid}/save", json={})
    assert response.status_code == 200, response.text
    assert next(iter(local.history.values()))["manifest"]["change_summary"] == "\n".join(restored.pending_change_summaries)
    assert session.pending_change_summaries == []
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 200
    assert list(local.history.values())[-1]["manifest"]["change_summary"] == ""


def test_manual_draft_replacement_clears_pending_summary(client, backend_main) -> None:
    session = backend_main.Session(pending_change_summaries=["A previous proposal."])
    session.current_skill.skill_md = "Original"
    backend_main.sessions[session.id] = session
    assert client.put(f"/api/sessions/{session.id}/draft", json={"skill_md": "Original"}).status_code == 200
    assert session.pending_change_summaries == ["A previous proposal."]
    assert client.put(f"/api/sessions/{session.id}/draft", json={"skill_md": "Manually replaced"}).status_code == 200
    assert session.pending_change_summaries == []


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
                "change_summary": "Update the skill instructions.",
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


def test_skip_open_fixes_closes_the_rest_without_a_patch(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = "one"
    session.iteration_reflections.append(
        IterationReflection(what_to_change=["fix one", "fix two", "fix three"], raw="reflection")
    )
    backend_main.sessions[session.id] = session
    _accept_patch(client, session, call_id="p1", old="one", new="two", addresses=["fix one"])

    response = client.post(f"/api/sessions/{session.id}/fix-list/skip")

    body = response.json()
    assert response.status_code == 200
    assert body["iteration_reflections"][-1]["skipped"] == ["fix two", "fix three"]
    note = body["conversation"][-1]
    assert note["role"] == "system"
    assert note["metadata"]["skipped_fixes"] == ["fix two", "fix three"]

    again = client.post(f"/api/sessions/{session.id}/fix-list/skip").json()
    assert again["iteration_reflections"][-1]["skipped"] == ["fix two", "fix three"]
    assert len(again["conversation"]) == len(body["conversation"])


def test_skip_open_fixes_rejects_another_users_session(client, backend_main) -> None:
    session = backend_main.Session(owner_upn="someone-else@example.com")
    session.iteration_reflections.append(IterationReflection(what_to_change=["fix one"], raw="r"))
    backend_main.sessions[session.id] = session

    response = client.post(f"/api/sessions/{session.id}/fix-list/skip")

    assert response.status_code in {403, 404}
    assert session.iteration_reflections[-1].skipped == []


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
    assert fake_sql.skills["new-skill"]["created_by"] == "test@example.com"
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


def test_rename_creator_without_grants_keeps_creator(client, backend_main, grant_skill, fake_sql) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill", mode="modify")
    fake_sql.grants.clear()
    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )
    assert response.status_code == 200
    assert "old-skill" not in fake_sql.skills
    assert fake_sql.skills["new-skill"]["created_by"] == "test@example.com"


def test_rename_rechecks_source_creator_before_any_destination_write(client, backend_main, grant_skill, fake_sql) -> None:
    session = _persisted_refine_session(backend_main, grant_skill, "old-skill", mode="modify")
    original = backend_main.store.load_skill("old-skill")
    fake_sql.skills["old-skill"]["created_by"] = "other@x"
    client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )
    assert "new-skill" not in fake_sql.skills
    assert backend_main.store.load_skill("old-skill").skill_md == original.skill_md
    with pytest.raises(FileNotFoundError):
        backend_main.store.load_skill("new-skill")


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
