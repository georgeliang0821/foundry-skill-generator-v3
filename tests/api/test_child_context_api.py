from __future__ import annotations

import json

import pytest

from backend import agent as agent_module, state_machine
from backend.blob_store import compute_hash
from backend.models import ChildContextSelection, PendingToolCall, Session, SkillFiles
from backend.state_machine import build_system_prompt, child_context_for_prompt


@pytest.fixture()
def context_session(client, backend_main, grant_skill):
    response = client.post("/api/sessions", json={"mode": "new", "skill_kind": "scenario"})
    backend_main.await_prepare_entry()
    session = backend_main.sessions[response.json()["id"]]
    session.children = [f"child-{index:02d}" for index in range(8)]
    for name in session.children:
        backend_main.store.save_skill(SkillFiles(name=name, skill_md=(
            f"---\nname: {name}\ndescription: Test capability\n---\n"
            "## Required Inputs\n```input-bindings\n- name: description\n  source: request\n```\n"
            f"## Execution\nEXECUTION_ONLY_{name}\n"
            f"## Error Handling\nERROR_ONLY_{name}\n"
        )))
        grant_skill(name)
    return session


def _mock_model(backend_main, monkeypatch, payloads):
    prompts = []

    def run(session, message):
        prompts.append(build_system_prompt(session))
        payload = payloads[min(len(prompts) - 1, len(payloads) - 1)]
        return json.dumps(payload)

    monkeypatch.setattr(agent_module, "_run_foundry_turn_sync", run)
    monkeypatch.setattr(backend_main, "agent", agent_module.OrchestratorAgent())
    return prompts


def _read(*reads):
    return {"text": "", "tool_calls": [{"tool": "read_child_context", "args": {"reads": list(reads)}}]}


def _errors(response):
    assert response.status_code == 200
    return [event["data"]["message"] for event in response.json()["events"] if event["event"] == "error"]


def test_batch_read_resumes_model_with_original_sections_in_the_same_chat(
    client, backend_main, monkeypatch, context_session
) -> None:
    session = context_session
    prompts = _mock_model(backend_main, monkeypatch, [
        _read(
            {"skill_name": "child-06", "sections": ["execution"]},
            {"skill_name": "child-07", "sections": ["Error Handling", "Exec"]},
        ),
        {"text": "Contracts read.", "tool_calls": []},
    ])

    response = client.post(f"/api/sessions/{session.id}/chat", json={"message": "Review the last children."})

    assert not _errors(response)
    assert len(prompts) == 2
    for name in session.children:
        assert f"<<<BEGIN CHILD SECTION: {name} / Required Inputs>>>" in prompts[0]
        assert f"- **{name}**:" in prompts[0]
        assert f"EXECUTION_ONLY_{name}" not in prompts[0]
    assert "EXECUTION_ONLY_child-06" in prompts[1]
    assert "ERROR_ONLY_child-06" not in prompts[1]
    assert "EXECUTION_ONLY_child-07" in prompts[1]
    assert "ERROR_ONLY_child-07" in prompts[1]
    assert prompts[1].index("EXECUTION_ONLY_child-07") < prompts[1].index("ERROR_ONLY_child-07")
    assert session.current_stage == "prepare"
    assert not session.pending_tool_calls
    assert not session.neighbor_edits


def test_read_discards_remaining_stale_tools_and_stops_at_confirmation(
    client, backend_main, monkeypatch, context_session
) -> None:
    session = context_session
    first = _read({"skill_name": "child-07", "full": True})
    first["tool_calls"].append({"tool": "stage_transition", "args": {"target": "draft"}})
    prompts = _mock_model(backend_main, monkeypatch, [
        first,
        {"text": "", "tool_calls": [{"tool": "ask_user_input", "args": {
            "question": "Proceed?", "options": ["Yes", "No"],
        }}]},
    ])

    response = client.post(f"/api/sessions/{session.id}/chat", json={"message": "Inspect the child."})

    assert not _errors(response)
    assert len(prompts) == 2
    md = session.child_full_md["child-07"]
    assert f"<<<BEGIN SKILL.md: child-07>>>\n{md}" in prompts[1]
    assert session.current_stage == "prepare"
    assert [call.tool for call in session.pending_tool_calls] == ["ask_user_input"]


@pytest.mark.parametrize("reads,reason", [
    ([], "at least 1"),
    ([{"skill_name": "outside", "full": True}], "not an exact declared"),
    ([{"skill_name": " child-07", "full": True}], "not an exact declared"),
    ([{"skill_name": "child-07", "sections": ["Invented"]}], "Available sections:"),
    ([{"skill_name": "child-07"}], "Choose either"),
    ([{"skill_name": "child-07", "full": True, "sections": ["Execution"]}], "Choose either"),
    ([{"skill_name": "child-07", "sections": [" "]}], "must not be blank"),
    ([{"skill_name": "child-07", "full": True}] * 2, "one read entry"),
])
def test_invalid_read_is_explicit_and_does_not_change_the_window(
    backend_main, context_session, reads, reason
) -> None:
    with pytest.raises(ValueError, match=reason):
        backend_main.apply_tool_effect(context_session, "read_child_context", {"reads": reads})
    assert context_session.child_context_reads == []


def test_read_requires_access_and_cannot_use_an_old_loaded_copy(
    backend_main, fake_sql, context_session
) -> None:
    session = context_session
    backend_main._load_child_full_md(session)
    fake_sql.remove_grant("child-07", session.owner_upn)
    backend_main.acl_mod.get_cache().invalidate()

    with pytest.raises(ValueError, match="not accessible"):
        backend_main.apply_tool_effect(session, "read_child_context", {
            "reads": [{"skill_name": "child-07", "full": True}],
        })
    assert session.child_context_reads == []


def test_invalid_read_in_chat_stops_explicitly_without_a_pending_card(
    client, backend_main, monkeypatch, context_session
) -> None:
    prompts = _mock_model(backend_main, monkeypatch, [
        _read({"skill_name": "child-07", "sections": ["Invented"]}),
    ])

    response = client.post(f"/api/sessions/{context_session.id}/chat", json={"message": "Read."})

    assert "Available sections:" in _errors(response)[0]
    assert len(prompts) == 1
    assert context_session.child_context_reads == []
    assert not context_session.pending_tool_calls


def test_short_full_reads_are_limited_by_characters_not_child_count(
    backend_main, context_session
) -> None:
    session = context_session
    backend_main.apply_tool_effect(session, "read_child_context", {
        "reads": [{"skill_name": name, "full": True} for name in session.children],
    })

    rendered = child_context_for_prompt(session)
    assert len(session.child_context_reads) == 8
    assert len(rendered) <= state_machine.CHILD_CONTEXT_MAX_CHARS
    for name in session.children:
        assert f"<<<BEGIN SKILL.md: {name}>>>\n{session.child_full_md[name]}" in rendered


def test_declared_child_cannot_bypass_the_read_window_as_a_neighbor(
    backend_main, context_session
) -> None:
    session = context_session
    backend_main._load_child_full_md(session)
    session.neighbor_full_md = {
        "child-07": session.child_full_md["child-07"],
        "unrelated-neighbor": "UNRELATED_NEIGHBOR_BODY",
    }

    prompt = build_system_prompt(session)

    assert "EXECUTION_ONLY_child-07" not in prompt
    assert "ERROR_ONLY_child-07" not in prompt
    assert "UNRELATED_NEIGHBOR_BODY" in prompt
    assert "<<<BEGIN CHILD SECTION: child-07 / Required Inputs>>>" in prompt


def test_missing_child_is_not_treated_as_an_empty_contract(backend_main, context_session) -> None:
    backend_main.store.delete_skill("child-07")
    with pytest.raises(ValueError, match="could not be loaded"):
        backend_main.apply_tool_effect(context_session, "read_child_context", {
            "reads": [{"skill_name": "child-07", "full": True}],
        })


def test_context_budget_checks_exact_rendered_size_and_replaces_optional_reads(
    backend_main, monkeypatch, context_session
) -> None:
    session = context_session
    backend_main.apply_tool_effect(session, "read_child_context", {
        "reads": [{"skill_name": "child-06", "sections": ["Execution"]}],
    })
    previous = list(session.child_context_reads)
    read = ChildContextSelection(
        skill_name="child-07", full=True, version_hash=compute_hash(session.child_full_md["child-07"]),
    )
    size = len(child_context_for_prompt(session.model_copy(update={"child_context_reads": [read]})))
    monkeypatch.setattr(state_machine, "CHILD_CONTEXT_MAX_CHARS", size - 1)
    with pytest.raises(ValueError, match="characters"):
        backend_main.apply_tool_effect(session, "read_child_context", {
            "reads": [{"skill_name": "child-07", "full": True}],
        })
    assert session.child_context_reads == previous

    monkeypatch.setattr(state_machine, "CHILD_CONTEXT_MAX_CHARS", size)
    backend_main.apply_tool_effect(session, "read_child_context", {
        "reads": [{"skill_name": "child-07", "full": True}],
    })
    rendered = child_context_for_prompt(session)
    assert len(rendered) == size
    assert "EXECUTION_ONLY_child-07" in rendered
    assert "EXECUTION_ONLY_child-06" not in rendered
    assert "<<<BEGIN CHILD SECTION: child-06 / Required Inputs>>>" in rendered


def test_mandatory_context_over_budget_fails_before_any_model_call(
    client, backend_main, monkeypatch, context_session
) -> None:
    prompts = _mock_model(backend_main, monkeypatch, [{"text": "Must not run."}])
    monkeypatch.setattr(state_machine, "CHILD_CONTEXT_MAX_CHARS", 1)
    response = client.post(f"/api/sessions/{context_session.id}/chat", json={"message": "Continue."})
    assert "reduce the scenario scope" in _errors(response)[0]
    assert prompts == []


def test_repeated_reads_stop_without_leaving_a_pending_card(
    client, backend_main, monkeypatch, context_session
) -> None:
    prompts = _mock_model(backend_main, monkeypatch, [_read({"skill_name": "child-07", "full": True})])
    response = client.post(f"/api/sessions/{context_session.id}/chat", json={"message": "Read."})
    assert "Repeated child read" in _errors(response)[0]
    assert len(prompts) == 2
    assert not context_session.pending_tool_calls


def test_read_round_limit_cannot_be_bypassed_by_changing_requests(
    client, backend_main, monkeypatch, context_session
) -> None:
    payloads = [_read({"skill_name": name, "sections": ["Execution"]}) for name in context_session.children]
    prompts = _mock_model(backend_main, monkeypatch, payloads)
    response = client.post(f"/api/sessions/{context_session.id}/chat", json={"message": "Read."})
    assert "continuation limit" in _errors(response)[0]
    assert len(prompts) == backend_main.MAX_CHILD_READ_ROUNDS + 1
    assert not context_session.pending_tool_calls


def test_read_after_a_question_does_not_auto_continue_past_confirmation(
    client, backend_main, monkeypatch, context_session
) -> None:
    payload = _read({"skill_name": "child-07", "full": True})
    payload["tool_calls"].insert(0, {"tool": "ask_user_input", "args": {
        "question": "Proceed?", "options": ["Yes", "No"],
    }})
    prompts = _mock_model(backend_main, monkeypatch, [payload])
    response = client.post(f"/api/sessions/{context_session.id}/chat", json={"message": "Review."})
    assert "only tool" in _errors(response)[0]
    assert len(prompts) == 1
    assert [call.tool for call in context_session.pending_tool_calls] == ["ask_user_input"]


@pytest.mark.parametrize("changed", [False, True])
def test_reloaded_context_uses_current_version_and_never_renders_a_stale_full_body(
    client, backend_main, monkeypatch, context_session, changed
) -> None:
    session = context_session
    backend_main.apply_tool_effect(session, "read_child_context", {
        "reads": [{"skill_name": "child-07", "full": True}],
    })
    backend_main.persist_session(session)
    restored = backend_main.session_store.load(session.id)
    assert restored.child_full_md == {}
    assert restored.child_context_reads == session.child_context_reads
    assert "EXECUTION_ONLY_" not in restored.model_dump_json()
    if changed:
        md = backend_main.store.load_skill("child-07").skill_md
        backend_main.store.save_skill(SkillFiles(name="child-07", skill_md=md.replace(
            "EXECUTION_ONLY_child-07", "NEW_EXECUTION_ONLY_child-07",
        )))
    backend_main.sessions.pop(session.id)
    prompts = _mock_model(backend_main, monkeypatch, [{"text": "Reviewed.", "tool_calls": []}])

    response = client.post(f"/api/sessions/{session.id}/chat", json={"message": "Continue."})

    assert not _errors(response)
    assert ("read_status: stale_version" in prompts[0]) == changed
    assert (f"\n<<<BEGIN SKILL.md: child-07>>>" in prompts[0]) != changed
    assert "NEW_EXECUTION_ONLY_child-07" not in prompts[0]


def _child_patch(backend_main, session):
    backend_main.apply_tool_effect(session, "read_child_context", {
        "reads": [{"skill_name": "child-07", "full": True}],
    })
    args = {
        "skill_name": "child-07",
        "base_hash": compute_hash(session.child_full_md["child-07"]),
        "patch": "*** Begin Patch\n*** Update File: SKILL.md\n@@\n"
                 "-EXECUTION_ONLY_child-07\n+UPDATED_EXECUTION\n*** End Patch",
        "change_summary": "Clarify execution.",
    }
    return args


def test_full_read_then_child_proposal_waits_for_acceptance_and_does_not_save(
    client, backend_main, monkeypatch, context_session
) -> None:
    session = context_session
    args = _child_patch(backend_main, session)
    session.child_context_reads = []
    prompts = _mock_model(backend_main, monkeypatch, [
        _read({"skill_name": "child-07", "full": True}),
        {"text": "", "tool_calls": [{"tool": "propose_child_edit", "args": args}]},
    ])
    response = client.post(f"/api/sessions/{session.id}/chat", json={"message": "Clarify the child."})
    assert not _errors(response)
    assert len(prompts) == 2
    assert session.neighbor_edits == []
    call = session.pending_tool_calls[0]
    assert call.tool == "propose_child_edit"

    accepted = client.post(f"/api/sessions/{session.id}/tool-result", json={
        "tool_call_id": call.call_id, "result": {"action": "accept"},
    })

    assert accepted.status_code == 200, accepted.json()
    edit = session.neighbor_edits[0]
    assert len(edit.versions) == 2
    assert "UPDATED_EXECUTION" in edit.versions[-1].skill_md
    assert "UPDATED_EXECUTION" not in backend_main.store.load_skill("child-07").skill_md


def test_child_edit_requires_a_full_read_not_just_a_matching_hash(backend_main, context_session) -> None:
    args = _child_patch(backend_main, context_session)
    backend_main.apply_tool_effect(context_session, "read_child_context", {
        "reads": [{"skill_name": "child-07", "sections": ["Execution"]}],
    })
    with pytest.raises(ValueError, match="full read"):
        backend_main.apply_tool_effect(context_session, "propose_child_edit", args)


@pytest.mark.parametrize("switch_version", [False, True])
def test_stale_child_patch_is_rejected_without_falling_back(
    client, backend_main, context_session, switch_version
) -> None:
    session = context_session
    args = _child_patch(backend_main, session)
    backend_main.apply_tool_effect(session, "propose_child_edit", args)
    call = PendingToolCall(tool="propose_child_edit", args=args)
    session.pending_tool_calls.append(call)
    original = session.child_full_md["child-07"]
    proposed = client.post(f"/api/sessions/{session.id}/neighbor-edits/child-07/propose", json={
        "skill_md": original.replace("EXECUTION_ONLY_child-07", "NEW_SELECTED_EXECUTION"),
        "label": "New selected version",
    })
    assert proposed.status_code == 200
    edit = session.neighbor_edits[0]
    version_id = edit.selected_version_id
    if not switch_version:
        call.args["base_hash"] = compute_hash(edit.versions[-1].skill_md)
    count = len(edit.versions)

    response = client.post(f"/api/sessions/{session.id}/tool-result", json={
        "tool_call_id": call.call_id, "result": {"action": "accept"},
    })

    assert response.status_code == 409
    expected = "child_version_changed" if switch_version else "neighbor_patch_failed"
    assert response.json()["detail"]["kind"] == expected
    assert len(edit.versions) == count
    assert edit.selected_version_id == version_id
    assert backend_main.store.load_skill("child-07").skill_md == original


def test_capability_cannot_use_child_read_tool(backend_main) -> None:
    with pytest.raises(ValueError, match="only available for a scenario"):
        backend_main.apply_tool_effect(Session(), "read_child_context", {
            "reads": [{"skill_name": "child", "full": True}],
        })
