from __future__ import annotations

import json

import pytest

from backend import agent as agent_module
from backend.models import PendingToolCall, SkillFiles, Stage, TestRun as SkillTestRun


HOST = """---
name: parent-skill
description: Coordinate the case.
metadata:
  skill_type: scenario-orchestration
  children: [child-skill]
---

## Instructions
Ask for missing inputs and confirm before publishing.
"""
EAA = HOST.replace("  children:", "  orchestration_owner: eaa\n  children:")
CHILD = "---\nname: child-skill\nmetadata: {skill_type: inline}\n---\n\n## Overview\nCapability.\n"


def seed(backend_main, grant_skill, owner="host"):
    parent = EAA if owner == "eaa" else HOST
    for name, md in (("parent-skill", parent), ("child-skill", CHILD)):
        backend_main.store.save_skill(SkillFiles(name=name, skill_md=md))
        grant_skill(name)
    return parent


def modify(client, backend_main, grant_skill, owner="host"):
    original = seed(backend_main, grant_skill, owner)
    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "parent-skill", "start_stage": "refine"})
    assert response.status_code == 200
    sid = response.json()["id"]
    backend_main.await_prepare_entry()
    return sid, original


@pytest.mark.parametrize("owner", ["host", "eaa"])
def test_modify_and_save_preserve_owner_and_legacy_omission(client, backend_main, grant_skill, owner):
    sid, original = modify(client, backend_main, grant_skill, owner)
    assert client.get(f"/api/sessions/{sid}").json()["orchestration_owner"] == owner
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 200
    saved = backend_main.store.load_skill("parent-skill").skill_md
    assert ("orchestration_owner: eaa" in saved) is (owner == "eaa")
    assert ("orchestration_owner" in saved) is ("orchestration_owner" in original)


def test_owner_selection_is_scenario_only_and_requires_session_access(client, anon_client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    path = f"/api/sessions/{sid}/orchestration-owner"
    assert client.post(path, json={"owner": "eaa"}).status_code == 400
    assert anon_client.post(path, json={"owner": "eaa"}).status_code == 401
    assert client.post(path, headers={"X-Test-UPN": "other@example.com"}, json={"owner": "eaa"}).status_code == 404
    assert client.post("/api/sessions", json={"orchestration_owner": "eaa"}).status_code == 400
    sid = client.post("/api/sessions", json={"skill_kind": "scenario"}).json()["id"]
    assert client.post(f"/api/sessions/{sid}/orchestration-owner", json={"owner": "invalid"}).status_code == 422


def test_new_owner_selection_and_import_preserve_source(client):
    created = client.post("/api/sessions", json={"skill_kind": "scenario", "orchestration_owner": "eaa"})
    assert created.status_code == 200
    assert created.json()["orchestration_owner"] == "eaa"
    assert not created.json()["owner_regeneration_pending"]
    imported = client.post("/api/sessions", json={"mode": "import", "materials": [{"kind": "text", "content": EAA}]})
    assert imported.status_code == 200
    assert imported.json()["skill_kind"] == "scenario"
    assert imported.json()["orchestration_owner"] == "eaa"
    assert imported.json()["children"] == ["child-skill"]


def owner_chat(client, backend_main, monkeypatch, sid, args, *, trailing_calls=()):
    monkeypatch.setattr(agent_module, "_run_foundry_turn_sync", lambda session, message: json.dumps({
        "text": "",
        "tool_calls": [{"tool": "set_orchestration_owner", "args": args}, *trailing_calls],
    }))
    monkeypatch.setattr(backend_main, "agent", agent_module.OrchestratorAgent())
    response = client.post(f"/api/sessions/{sid}/chat", json={"message": "Switch the owner as confirmed."})
    assert response.status_code == 200
    return response.json()["events"]


@pytest.mark.parametrize("initial,target", [("host", "eaa"), ("eaa", "host")])
def test_chat_owner_switch_persists_without_another_ui_confirmation(
    client, backend_main, grant_skill, monkeypatch, initial, target,
):
    sid, original = modify(client, backend_main, grant_skill, initial)
    session = backend_main.sessions[sid]
    session.pending_tool_calls = [PendingToolCall(tool="propose_patch", args={})]
    session.test_runs = [SkillTestRun()]
    session.prepare_brief.verify_checklist = {key: True for key in session.prepare_brief.verify_checklist}
    events = owner_chat(client, backend_main, monkeypatch, sid, {"owner": target, "confirm_restart": True},
                        trailing_calls=[{"tool": "record_understanding", "args": {"skill_goal": "STALE OWNER CONTEXT"}}])
    assert not any(event["event"] in {"error", "tool_effect_rejected"} for event in events), events
    calls = [event["data"]["tool"] for event in events if event["event"] == "tool_call"]
    assert calls == ["set_orchestration_owner"]
    state = next(event["data"] for event in events if event["event"] == "state_update")
    assert state["orchestration_owner"] == target
    assert state["current_stage"] == "prepare"
    assert state["current_skill"]["skill_md"] == ""
    assert state["owner_source_skill_md"] == original
    assert state["owner_regeneration_pending"]
    assert state["prepare_brief"]["understanding"]["skill_goal"] != "STALE OWNER CONTEXT"
    assert not state["pending_tool_calls"] and not state["test_runs"] and not state["patch_history"]
    assert not any(state["prepare_brief"]["verify_checklist"].values())
    assert any(event["kind"] == "tool" and event["data"]["tool"] == "set_orchestration_owner"
               for event in state["ui_history"])
    assert backend_main.store.load_skill("parent-skill").skill_md == original
    backend_main.await_prepare_entry()
    backend_main.sessions.pop(sid)
    restored = client.get(f"/api/sessions/{sid}").json()
    assert restored["orchestration_owner"] == target
    assert restored["owner_source_skill_md"] == original
    assert restored["current_skill"]["skill_md"] == ""


def test_chat_owner_switch_needs_restart_consent_before_clearing_existing_text(
    client, backend_main, grant_skill, monkeypatch,
):
    sid, original = modify(client, backend_main, grant_skill)
    events = owner_chat(client, backend_main, monkeypatch, sid, {"owner": "eaa", "confirm_restart": False},
                        trailing_calls=[{"tool": "record_understanding", "args": {"skill_goal": "MUST NOT APPLY"}}])
    rejection = next(event["data"] for event in events if event["event"] == "tool_effect_rejected")
    assert "confirmation" in rejection["message"].lower()
    assert "ask_user_input" in rejection["guidance"]
    assert "set_orchestration_owner" in rejection["guidance"]
    state = next(event["data"] for event in events if event["event"] == "state_update")
    assert state["orchestration_owner"] == "host"
    assert state["current_stage"] == "refine"
    assert state["current_skill"]["skill_md"] == original
    assert state["prepare_brief"]["understanding"]["skill_goal"] != "MUST NOT APPLY"
    assert not state["pending_tool_calls"]
    assert backend_main.store.load_skill("parent-skill").skill_md == original


def test_chat_owner_switch_on_empty_scenario_needs_no_restart_confirmation(
    client, backend_main, monkeypatch,
):
    sid = client.post("/api/sessions", json={"skill_kind": "scenario"}).json()["id"]
    events = owner_chat(client, backend_main, monkeypatch, sid, {"owner": "eaa", "confirm_restart": False})
    assert not any(event["event"] in {"error", "tool_effect_rejected"} for event in events), events
    state = next(event["data"] for event in events if event["event"] == "state_update")
    assert state["orchestration_owner"] == "eaa"
    assert not state["owner_regeneration_pending"]
    assert not state["pending_tool_calls"]


def test_chat_same_owner_does_not_clear_existing_text(client, backend_main, grant_skill, monkeypatch):
    sid, original = modify(client, backend_main, grant_skill)
    events = owner_chat(client, backend_main, monkeypatch, sid, {"owner": "host", "confirm_restart": False})
    assert not any(event["event"] in {"error", "tool_effect_rejected"} for event in events), events
    state = next(event["data"] for event in events if event["event"] == "state_update")
    assert state["current_skill"]["skill_md"] == original
    assert state["current_stage"] == "refine"
    assert not state["owner_regeneration_pending"]
    assert not state["pending_tool_calls"]


@pytest.mark.parametrize("kind,owner", [("capability", "eaa"), ("scenario", "invalid")])
def test_chat_owner_tool_rejects_wrong_kind_or_invalid_owner(client, backend_main, monkeypatch, kind, owner):
    sid = client.post("/api/sessions", json={"skill_kind": kind}).json()["id"]
    events = owner_chat(client, backend_main, monkeypatch, sid, {"owner": owner, "confirm_restart": True})
    assert any(event["event"] == "tool_effect_rejected" for event in events), events
    assert not any(event["event"] == "error" for event in events), events
    state = next(event["data"] for event in events if event["event"] == "state_update")
    assert state["orchestration_owner"] == "host"
    assert not state["pending_tool_calls"]


@pytest.mark.parametrize("initial,target", [("host", "eaa"), ("eaa", "host")])
def test_switch_requires_confirmation_and_persists_clean_regeneration(
    client, backend_main, grant_skill, initial, target,
):
    sid, original = modify(client, backend_main, grant_skill, initial)
    session = backend_main.sessions[sid]
    session.pending_tool_calls = [PendingToolCall(tool="propose_patch", args={})]
    session.test_runs = [SkillTestRun()]
    session.prepare_brief.verify_checklist = {key: True for key in session.prepare_brief.verify_checklist}
    path = f"/api/sessions/{sid}/orchestration-owner"
    refused = client.post(path, json={"owner": target})
    assert refused.status_code == 409
    assert session.current_skill.skill_md == original
    response = client.post(path, json={"owner": target, "confirm_restart": True})
    assert response.status_code == 200
    body = response.json()
    assert body["orchestration_owner"] == target
    assert body["current_stage"] == "prepare"
    assert body["current_skill"]["skill_md"] == ""
    assert body["owner_regeneration_pending"]
    assert body["owner_source_skill_md"] == original
    assert not body["pending_tool_calls"] and not body["patch_history"] and not body["test_runs"]
    assert not any(body["prepare_brief"]["verify_checklist"].values())
    assert backend_main.store.load_skill("parent-skill").skill_md == original
    remote_hash = body["remote_version_hash"]
    backend_main.await_prepare_entry()
    backend_main.sessions.pop(sid)
    restored = backend_main.get_session_for_user(sid, "test@example.com")
    restored.current_stage = Stage.DRAFT
    assert not backend_main._hydrate_current_skill_from_remote(restored)
    assert not backend_main.session_binding_state(restored)[1]
    assert restored.remote_version_hash == remote_hash
    assert restored.current_skill.skill_md == ""

    replacement = EAA if target == "eaa" else HOST
    call = PendingToolCall(tool="propose_skill_draft", args={"skill_md": replacement})
    restored.pending_tool_calls.append(call)
    backend_main.apply_tool_effect(restored, call.tool, call.args)
    response = client.post(f"/api/sessions/{sid}/tool-result", json={"tool_call_id": call.call_id, "result": {"action": "accept"}})
    assert response.status_code == 200, response.text
    assert not response.json()["owner_regeneration_pending"]
    assert response.json()["owner_source_skill_md"] == ""
    assert response.json()["current_stage"] == "refine"
    assert ("orchestration_owner: eaa" in backend_main.store.load_skill("parent-skill").skill_md) is (target == "eaa")


def test_editor_patch_and_save_cannot_change_owner_without_confirmation(client, backend_main, grant_skill):
    sid, original = modify(client, backend_main, grant_skill)
    assert client.put(f"/api/sessions/{sid}/draft", json={"skill_md": EAA}).status_code == 409
    session = backend_main.sessions[sid]
    with pytest.raises(ValueError, match="owner selector"):
        backend_main._assert_topology_preserved(session, EAA, tool="propose_patch")
    session.current_skill.skill_md = EAA
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 400
    assert backend_main.store.load_skill("parent-skill").skill_md == original


def test_switching_a_new_draft_preserves_business_rules_before_prepare_reset(client, backend_main):
    sid = client.post("/api/sessions", json={"skill_kind": "scenario"}).json()["id"]
    session = backend_main.sessions[sid]
    session.current_stage = Stage.DRAFT
    session.current_skill.skill_md = HOST
    response = client.post(
        f"/api/sessions/{sid}/orchestration-owner",
        json={"owner": "eaa", "confirm_restart": True},
    )
    assert response.status_code == 200
    assert response.json()["owner_source_skill_md"] == HOST
    assert response.json()["current_skill"]["skill_md"] == ""
    backend_main.await_prepare_entry()
    assert backend_main.session_store.load(sid).owner_source_skill_md == HOST


@pytest.mark.parametrize("failure", ["invalid", "failed", "unavailable"])
def test_failed_eaa_save_does_not_overwrite_original_or_run_probe(
    client, backend_main, grant_skill, monkeypatch, failure,
):
    sid, original = modify(client, backend_main, grant_skill)
    path = f"/api/sessions/{sid}/orchestration-owner"
    assert client.post(path, json={"owner": "eaa", "confirm_restart": True}).status_code == 200
    session = backend_main.sessions[sid]
    session.current_stage = Stage.DRAFT
    session.current_skill.skill_md = EAA
    calls = []

    def lint(name, files):
        calls.append((name, files))
        if failure == "failed":
            raise backend_main.EaaLintRejected("Rejected")
        if failure == "unavailable":
            raise backend_main.EaaLintUnavailable("Offline")
        return backend_main.EaaLintResult(valid=False, errors=["bad"], warnings=[], ruleset_version="1.3")

    monkeypatch.setattr(backend_main, "lint_skill_package", lint)
    response = client.post(f"/api/sessions/{sid}/save", json={})
    assert response.status_code == (503 if failure == "unavailable" else 400)
    assert len(calls) == 1
    assert calls[0][0] == "parent-skill"
    assert set(calls[0][1]) == {"SKILL.md"}
    assert "orchestration_owner: eaa" in calls[0][1]["SKILL.md"]
    assert backend_main.store.load_skill("parent-skill").skill_md == original
    assert session.owner_regeneration_pending


def test_same_owner_is_a_noop_and_draftless_switch_does_not_write(client, backend_main):
    sid = client.post("/api/sessions", json={"skill_kind": "scenario"}).json()["id"]
    session = backend_main.sessions[sid]
    session.test_runs = [SkillTestRun()]
    path = f"/api/sessions/{sid}/orchestration-owner"
    assert client.post(path, json={"owner": "host"}).status_code == 200
    assert len(session.test_runs) == 1
    assert client.post(path, json={"owner": "eaa"}).status_code == 200
    assert session.orchestration_owner == "eaa"
    assert not session.owner_regeneration_pending
    assert not session.test_runs


def test_regeneration_keeps_concurrent_remote_update_protected(client, backend_main, grant_skill):
    sid, original = modify(client, backend_main, grant_skill)
    original_hash = backend_main.sessions[sid].remote_version_hash
    response = client.post(
        f"/api/sessions/{sid}/orchestration-owner",
        json={"owner": "eaa", "confirm_restart": True},
    )
    assert response.status_code == 200
    backend_main.await_prepare_entry()
    concurrent = original + "\n## Updated\nPreserve this concurrent change.\n"
    backend_main.store.save_skill(SkillFiles(name="parent-skill", skill_md=concurrent))
    session = backend_main.sessions[sid]
    session.current_stage = Stage.DRAFT
    session.current_skill.skill_md = EAA
    response = client.post(f"/api/sessions/{sid}/save", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "version_conflict"
    assert backend_main.store.load_skill("parent-skill").skill_md == concurrent
    assert session.remote_version_hash == original_hash
    assert session.owner_source_skill_md == original
    assert session.owner_regeneration_pending


def test_switching_back_before_regeneration_does_not_restore_old_remote(client, backend_main, grant_skill):
    sid, _ = modify(client, backend_main, grant_skill)
    path = f"/api/sessions/{sid}/orchestration-owner"
    client.post(path, json={"owner": "eaa", "confirm_restart": True})
    response = client.post(path, json={"owner": "host"})
    assert response.status_code == 200
    assert response.json()["owner_regeneration_pending"]
    assert response.json()["current_skill"]["skill_md"] == ""
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 400
