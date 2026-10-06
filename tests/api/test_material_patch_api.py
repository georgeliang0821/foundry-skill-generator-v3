from __future__ import annotations

import pytest

from backend.models import Material, MaterialKind, PendingToolCall, Session, SkillKind, SkillVariable, Stage
from backend.state_machine import build_system_prompt

FLAGS_ON = {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "true"}}
FLAGS_OFF = {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "false"}}

CODE = """import requests

ROOM_ID = "room-1"
response = requests.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)
print(response.text)
"""

EDGE_PATCH = """*** Begin Patch
*** Update File: material
@@
-import requests
+import argparse
+import json
+import requests
@@
-ROOM_ID = "room-1"
+parser = argparse.ArgumentParser(add_help=False)
+parser.add_argument("--room-id", default="")
+try:
+    ROOM_ID = parser.parse_args().room_id
+except SystemExit:
+    print("[NEEDS_INFO] missing=ROOM_ID")
+    raise SystemExit(0)
 response = requests.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)
-print(response.text)
+print(json.dumps({"room": response.text}))
*** End Patch"""

# Adds the flag but leaves the plain-text stdout line.
PARTIAL_PATCH = """*** Begin Patch
*** Update File: material
@@
-ROOM_ID = "room-1"
+import argparse
+parser = argparse.ArgumentParser(add_help=False)
+parser.add_argument("--room-id", default="")
+try:
+    ROOM_ID = parser.parse_args().room_id
+except SystemExit:
+    print("[NEEDS_INFO] missing=ROOM_ID")
+    raise SystemExit(0)
*** End Patch"""

REWRITE_PATCH = """*** Begin Patch
*** Update File: material
@@
-response = requests.get(f"https://graph.example.invalid/rooms/{ROOM_ID}", timeout=10)
+response = None
*** End Patch"""


def _session(**overrides) -> Session:
    session = Session(
        **{
            "aca_env_result": FLAGS_ON,
            "materials": [Material(id="code-1", kind=MaterialKind.CODE, content=CODE)],
            **overrides,
        }
    )
    session.prepare_brief.script_covers_operations = True
    session.prepare_brief.verify_checklist["variables_ok"] = True
    return session


def _propose(backend_main, session: Session, patch: str = EDGE_PATCH, material_id: str = "code-1") -> None:
    backend_main.apply_tool_effect(
        session, "propose_material_patch", {"material_id": material_id, "patch": patch, "reason": "flag input"}
    )


def _stage(backend_main, session: Session, patch: str = EDGE_PATCH, call_id: str = "mp1") -> None:
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id=call_id,
            tool="propose_material_patch",
            args={"material_id": "code-1", "patch": patch, "reason": "flag input"},
        )
    )
    backend_main.sessions[session.id] = session


def _answer(client, session: Session, action: str, call_id: str = "mp1"):
    return client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": call_id, "result": {"action": action}},
    )


# --- proposal gate ------------------------------------------------------------


def test_an_edge_patch_passes_the_gate(backend_main) -> None:
    _propose(backend_main, _session())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"current_stage": Stage.DRAFT}, "only allowed in PREPARE"),
        ({"aca_env_result": FLAGS_OFF}, "script flags are on"),
        ({"skill_form": "inline"}, "still choosing its form"),
        ({"mode": "modify"}, "new capability skill"),
    ],
)
def test_the_gate_refuses_outside_a_script_candidate_prepare(backend_main, overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        _propose(backend_main, _session(**overrides))


def test_a_scenario_skill_cannot_patch_materials(backend_main) -> None:
    with pytest.raises(ValueError, match="only available for a capability skill"):
        _propose(backend_main, _session(skill_kind=SkillKind.SCENARIO))


def test_the_gate_needs_exactly_one_code_material(backend_main) -> None:
    session = _session()
    session.materials.append(Material(id="code-2", kind=MaterialKind.CODE, content="print(1)\n"))
    with pytest.raises(ValueError, match="exactly one code material; there are 2"):
        _propose(backend_main, session)


def test_the_gate_refuses_a_text_material(backend_main) -> None:
    session = _session()
    session.materials.append(Material(id="text-1", kind=MaterialKind.TEXT, content='ROOM_ID = "room-1"'))
    with pytest.raises(ValueError, match="`text-1` is not this session's code material"):
        _propose(backend_main, session, material_id="text-1")


def test_a_rewrite_is_refused_with_the_rewrite_guidance(backend_main) -> None:
    with pytest.raises(ValueError, match="requests.get") as exc:
        _propose(backend_main, _session(), REWRITE_PATCH)
    assert "this change is a rewrite" in str(exc.value)
    assert "keep the inline form" in str(exc.value)


def test_a_missing_anchor_asks_for_verbatim_context(backend_main) -> None:
    patch = EDGE_PATCH.replace('-ROOM_ID = "room-1"', '-ROOM_ID = "room-2"')
    with pytest.raises(ValueError, match="VERBATIM") as exc:
        _propose(backend_main, _session(), patch)
    assert "rewrite" not in str(exc.value)


def test_a_patch_that_leaves_a_script_lint_error_is_refused(backend_main) -> None:
    with pytest.raises(ValueError, match="still cannot ship as the bundled script") as exc:
        _propose(backend_main, _session(), PARTIAL_PATCH)
    message = str(exc.value)
    assert "script_lint (S4): Line 12 (`print(response.text)`)" in message
    assert "in ONE new patch" in message
    assert "Refusal 1 of 3." in message
    assert "Nothing was applied" in message and "without asking again" in message


def test_a_rewrite_refusal_counts_but_does_not_invite_a_retry(backend_main) -> None:
    session = _session()
    with pytest.raises(ValueError, match="Refusal 1 of 3") as exc:
        _propose(backend_main, session, REWRITE_PATCH)
    assert "Nothing was applied" not in str(exc.value)
    assert session.material_patch_rejections == 1


def test_an_ineligible_call_is_not_counted(backend_main) -> None:
    session = _session(current_stage=Stage.DRAFT)
    with pytest.raises(ValueError, match="only allowed in PREPARE"):
        _propose(backend_main, session)
    assert session.material_patch_rejections == 0


def test_the_recovery_message_does_not_tell_the_agent_to_ask_the_user(backend_main) -> None:
    session = _session()
    try:
        _propose(backend_main, session, PARTIAL_PATCH)
    except ValueError as exc:
        guidance = backend_main._tool_effect_recovery_guidance(session, "propose_material_patch", {}, exc)
    assert guidance.startswith("The 'propose_material_patch' action could not be applied: The patched code")
    assert "ask_user_input" not in guidance


def test_a_request_input_without_a_flag_is_refused(backend_main) -> None:
    session = _session()
    session.prepare_brief.variables = [
        SkillVariable(name="ROOM_ID", kind="runtime", source="request"),
        SkillVariable(name="BUILDING", kind="runtime", source="request"),
        SkillVariable(name="TENANT", kind="runtime", source="credentials", credentials_key="TENANT"),
    ]
    with pytest.raises(ValueError, match="- inputs: These request inputs have no matching `add_argument` flag: `BUILDING`\\."):
        _propose(backend_main, session)


def test_refusals_stop_after_the_limit_and_reset_when_the_code_changes(backend_main) -> None:
    session = _session()
    for count in range(1, backend_main.MAX_MATERIAL_PATCH_REJECTIONS):
        with pytest.raises(ValueError, match=f"Refusal {count} of 3\\. Nothing was applied"):
            _propose(backend_main, session, PARTIAL_PATCH)
    with pytest.raises(ValueError, match="Refusal 3 of 3: the limit is reached") as last:
        _propose(backend_main, session, PARTIAL_PATCH)
    assert "Nothing was applied" not in str(last.value)

    with pytest.raises(ValueError, match="refused 3 times") as exc:
        _propose(backend_main, session)
    assert "Do not propose another patch" in str(exc.value)

    before = backend_main.code_material_contents(session)
    session.materials[0] = Material(id="code-1", kind=MaterialKind.CODE, content=CODE + "# edited\n")
    backend_main.apply_code_material_change(session, before)
    assert session.material_patch_rejections == 0
    _propose(backend_main, session)


# --- tool result --------------------------------------------------------------


def test_accepting_replaces_the_material_and_resets_the_coverage(client, backend_main) -> None:
    session = _session()
    _stage(backend_main, session)

    response = _answer(client, session, "accept")

    assert response.status_code == 200, response.text
    body = response.json()
    material = body["materials"][0]
    assert 'parser.add_argument("--room-id", default="")' in material["content"]
    assert material["origin"] == "agent_patch"
    assert material["user_content"] == CODE
    assert body["prepare_brief"]["script_covers_operations"] is False
    assert body["prepare_brief"]["verify_checklist"]["variables_ok"] is False
    assert any(m["metadata"].get("material_patched") == "code-1" for m in body["conversation"])
    assert body["pending_tool_calls"] == []

    prompt = build_system_prompt(backend_main.sessions[session.id])
    assert "<<<BEGIN MATERIAL code-1 (kind=code, origin=agent_patch, not run by the user)>>>" in prompt


def test_a_second_patch_keeps_the_users_original_text(client, backend_main) -> None:
    session = _session()
    _stage(backend_main, session)
    assert _answer(client, session, "accept").status_code == 200
    second = """*** Begin Patch
*** Update File: material
@@
-print(json.dumps({"room": response.text}))
+print(json.dumps({"room": response.text}), flush=True)
*** End Patch"""
    _stage(backend_main, session, second, call_id="mp2")

    body = _answer(client, session, "accept", call_id="mp2").json()

    assert body["materials"][0]["content"].endswith('print(json.dumps({"room": response.text}), flush=True)\n')
    assert body["materials"][0]["user_content"] == CODE


def test_accept_rechecks_against_the_current_material(client, backend_main) -> None:
    session = _session()
    _stage(backend_main, session)
    session.materials[0] = Material(id="code-1", kind=MaterialKind.CODE, content=CODE.replace("room-1", "room-9"))

    response = _answer(client, session, "accept")

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "material_patch_rejected"
    stored = backend_main.sessions[session.id]
    assert stored.materials[0].origin == "user"
    assert "room-9" in stored.materials[0].content
    assert any("Material patch was not applied" in m.content for m in stored.conversation)


def test_rejecting_leaves_the_material_alone(client, backend_main) -> None:
    session = _session()
    _stage(backend_main, session)

    body = _answer(client, session, "reject").json()

    assert body["materials"][0]["content"] == CODE
    assert body["materials"][0]["origin"] == "user"
    assert body["prepare_brief"]["script_covers_operations"] is True


def test_a_user_edit_clears_the_agent_mark(client, backend_main) -> None:
    session = _session()
    _stage(backend_main, session)
    patched = _answer(client, session, "accept").json()["materials"][0]["content"]

    response = client.put(
        f"/api/sessions/{session.id}/materials/code-1",
        json={"kind": "code", "content": patched + "# ran it\n"},
    )

    material = response.json()["materials"][0]
    assert material["origin"] == "user"
    assert material["user_content"] is None


# --- mechanical stdout fix ----------------------------------------------------

PROGRESS_CODE = """import json
import requests

print("querying rooms...")
response = requests.get("https://graph.example.invalid/rooms", timeout=10)
print(response.text)
"""


def _fix_session() -> Session:
    session = _session()
    session.materials[0] = Material(id="code-1", kind=MaterialKind.CODE, content=PROGRESS_CODE)
    return session


def test_the_skill_form_offers_the_fix_only_for_movable_prints(backend_main) -> None:
    assert "Mechanical stdout fix available: the plain-text print() calls on lines 4 can move" in (
        build_system_prompt(_fix_session())
    )
    assert "Mechanical stdout fix available" not in build_system_prompt(_session())


def test_the_chat_fills_the_computed_diff_into_the_call(backend_main) -> None:
    session = _fix_session()
    call = PendingToolCall(call_id="fx1", tool="propose_material_patch", args={"material_id": "code-1", "stdout_fix": True, "reason": "r"})
    session.pending_tool_calls.append(call)
    data = call.model_dump()

    backend_main._fill_stdout_fix_patch(session, data)

    assert '+print("querying rooms...", file=sys.stderr)' in data["args"]["patch"]
    assert session.pending_tool_calls[0].args["patch"] == data["args"]["patch"]
    backend_main.apply_tool_effect(session, "propose_material_patch", data["args"])


def test_a_stdout_fix_with_nothing_to_move_is_not_counted(backend_main) -> None:
    session = _session()
    with pytest.raises(ValueError, match="No print\\(\\) in the code material can move"):
        backend_main.apply_tool_effect(
            session, "propose_material_patch", {"material_id": "code-1", "stdout_fix": True, "reason": "r"}
        )
    assert session.material_patch_rejections == 0


def test_accepting_a_partial_stdout_fix_applies_it(client, backend_main) -> None:
    session = _fix_session()
    session.material_patch_rejections = 2
    _, _, patch, _ = backend_main._checked_stdout_fix(session, "code-1")
    _stage(backend_main, session, patch, call_id="fx1")
    session.pending_tool_calls[-1].args["stdout_fix"] = True

    response = _answer(client, session, "accept", call_id="fx1")

    assert response.status_code == 200, response.text
    body = response.json()
    content = body["materials"][0]["content"]
    assert content.startswith("import sys\nimport json\n")
    assert 'print("querying rooms...", file=sys.stderr)' in content
    assert "print(response.text)\n" in content
    assert body["materials"][0]["origin"] == "agent_patch"
    assert body["material_patch_rejections"] == 0
    assert any("lines 4 now write to stderr" in m["content"] for m in body["conversation"])


def test_accepting_a_stale_stdout_fix_is_refused(client, backend_main) -> None:
    session = _fix_session()
    _, _, patch, _ = backend_main._checked_stdout_fix(session, "code-1")
    _stage(backend_main, session, patch, call_id="fx1")
    session.pending_tool_calls[-1].args["stdout_fix"] = True
    session.materials[0] = Material(id="code-1", kind=MaterialKind.CODE, content=PROGRESS_CODE.replace("rooms...", "rooms now"))

    response = _answer(client, session, "accept", call_id="fx1")

    assert response.status_code == 409
    assert "changed after this stdout fix was proposed" in response.json()["detail"]["error"]
