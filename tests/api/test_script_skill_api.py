from __future__ import annotations

import pytest

from backend import state_machine
from backend.models import Material, MaterialKind, PendingToolCall, SkillFiles
from backend.state_machine import CODE_MATERIAL_EDITED_MESSAGE, SCRIPT_REPLACED_MESSAGE, build_system_prompt

SCRIPT = 'import json\nprint(json.dumps({"status": "ok"}))\n'
FLAGS_ON = {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "true"}}


def _aca_lookup(backend_main, monkeypatch, result: dict | None) -> list[str]:
    calls: list[str] = []

    def _load(session, *, reason: str = "manual") -> None:
        calls.append(reason)
        session.aca_env_result = result

    monkeypatch.setattr(backend_main, "load_aca_env_for_session", _load)
    return calls


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Does a thing.\n---\n\n## Overview\n\nBody.\n"


def _lint_recorder(backend_main, monkeypatch, ruleset: str = "1.1"):
    calls: list[dict] = []

    def _lint(skill_name, files, **kwargs):
        calls.append({"skill_name": skill_name, "files": dict(files)})
        return backend_main.EaaLintResult(valid=True, errors=[], warnings=[], ruleset_version=ruleset)

    monkeypatch.setattr(backend_main, "lint_skill_package", _lint)
    return calls


def _script_session(backend_main, name: str, *, script: str | None = SCRIPT):
    session = backend_main.Session(current_stage="refine")
    session.current_skill.skill_md = _skill_md(name)
    session.current_skill.script = script
    session.aca_env_result = FLAGS_ON
    backend_main.sessions[session.id] = session
    return session


def _seed(backend_main, grant_skill, name: str, *, script: str | None = SCRIPT) -> SkillFiles:
    saved = backend_main.store.save_skill(SkillFiles(name=name, skill_md=_skill_md(name), script=script))
    grant_skill(name)
    return saved


def test_save_writes_the_script_and_lints_it_under_its_skill_name(client, backend_main, fake_sql, monkeypatch) -> None:
    calls = _lint_recorder(backend_main, monkeypatch)
    session = _script_session(backend_main, "room-finder")

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 200
    assert backend_main.store.load_skill("room-finder").script == SCRIPT
    assert calls[0]["files"] == {"SKILL.md": _skill_md("room-finder"), "scripts/room-finder.py": SCRIPT}
    assert "room-finder" in fake_sql.skills


def test_script_save_is_refused_when_eaa_lint_predates_script_checks(client, backend_main, fake_sql, monkeypatch) -> None:
    _lint_recorder(backend_main, monkeypatch, ruleset="1.0")
    session = _script_session(backend_main, "room-finder")

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 503
    assert "1.1" in response.json()["detail"]
    assert fake_sql.skills == {}


def test_inline_session_cannot_overwrite_a_script_skill(client, backend_main, grant_skill, fake_sql) -> None:
    _seed(backend_main, grant_skill, "room-finder")
    session = _script_session(backend_main, "room-finder", script=None)

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "script_form_mismatch"
    assert backend_main.store.load_skill("room-finder").script == SCRIPT


def test_modify_session_loads_the_script(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder")

    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder", "materials": []})

    assert response.status_code == 200
    assert response.json()["current_skill"]["script"] == SCRIPT
    assert response.json()["skill_form"] == "script"


def test_modify_quick_edit_starts_in_refine_with_the_stored_skill(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder")

    body = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "room-finder", "start_stage": "refine", "materials": []},
    ).json()

    assert body["current_stage"] == "refine"
    assert body["current_skill"]["script"] == SCRIPT
    assert body["current_skill"]["skill_md"].strip()


def test_modify_defaults_to_prepare_and_new_ignores_start_stage(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder")

    modify = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder"}).json()
    new = client.post("/api/sessions", json={"mode": "new", "start_stage": "refine"}).json()

    assert (modify["current_stage"], new["current_stage"]) == ("prepare", "prepare")


def test_modify_ignores_script_coverage_flags_instead_of_refusing(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder")
    session = client.post(
        "/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder", "materials": []}
    ).json()

    backend_main.apply_tool_effect(
        backend_main.sessions[session["id"]],
        "record_variables",
        {"script_covers_operations": True, "prefer_inline": True},
    )

    brief = backend_main.sessions[session["id"]].prepare_brief
    assert (brief.script_covers_operations, brief.prefer_inline) == (False, False)


def test_modify_session_of_an_inline_skill_stays_inline(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder", script=None)
    materials = [{"kind": "code", "content": SCRIPT}]

    body = client.post(
        "/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder", "materials": materials}
    ).json()
    form = client.get(f"/api/sessions/{body['id']}/skill-form").json()

    assert (body["skill_form"], body["current_skill"]["script"]) == ("inline", None)
    assert form == {"form": "inline", "locked": True, "failures": [], "replacement_problems": []}


def test_convert_form_moves_a_stored_script_into_skill_md_and_the_save_removes_it(
    client, backend_main, grant_skill, fake_sql, monkeypatch
) -> None:
    _lint_recorder(backend_main, monkeypatch)
    _seed(backend_main, grant_skill, "room-finder")
    session_id = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder"}).json()["id"]

    converted = client.post(f"/api/sessions/{session_id}/convert-form", json={"target": "inline"})
    saved = client.post(f"/api/sessions/{session_id}/save", json={})

    body = converted.json()
    assert (converted.status_code, body["skill_form"], body["current_skill"]["script"]) == (200, "inline", None)
    assert SCRIPT.strip() in body["current_skill"]["skill_md"]
    assert saved.status_code == 200
    assert not backend_main.store.has_script("room-finder")
    assert SCRIPT.strip() in backend_main.store.load_skill("room-finder").skill_md


def test_convert_form_moves_the_sample_block_into_the_script(client, backend_main, grant_skill) -> None:
    md = _skill_md("room-finder") + f"\n## API Reference / Sample Code\n\n```python\n{SCRIPT}```\n"
    backend_main.store.save_skill(SkillFiles(name="room-finder", skill_md=md))
    grant_skill("room-finder")
    session_id = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder"}).json()["id"]
    backend_main.sessions[session_id].aca_env_result = FLAGS_ON

    response = client.post(f"/api/sessions/{session_id}/convert-form", json={"target": "script"})

    body = response.json()
    assert (response.status_code, body["skill_form"], body["current_skill"]["script"]) == (200, "script", SCRIPT)
    assert "```" not in body["current_skill"]["skill_md"]
    assert backend_main.store.load_skill("room-finder").script is None


def test_convert_to_script_is_refused_while_the_flags_are_off(client, backend_main, grant_skill, monkeypatch) -> None:
    _aca_lookup(backend_main, monkeypatch, {"architectural_config": {"SKILL_SCRIPTS_ENABLED": "false"}})
    _seed(backend_main, grant_skill, "room-finder", script=None)
    session_id = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder"}).json()["id"]

    response = client.post(f"/api/sessions/{session_id}/convert-form", json={"target": "script"})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "script_flags_off"
    assert backend_main.sessions[session_id].skill_form == "inline"


def test_convert_form_refusal_is_a_recoverable_409(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder", script=None)
    session_id = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "room-finder"}).json()["id"]
    backend_main.sessions[session_id].aca_env_result = FLAGS_ON

    response = client.post(f"/api/sessions/{session_id}/convert-form", json={"target": "script"})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "form_conversion_refused"
    assert "0 Python" in response.json()["detail"]["message"]


def test_skill_form_endpoint_says_why_a_code_material_did_not_replace_the_script(client, backend_main) -> None:
    session = backend_main.Session(
        mode="modify",
        skill_form="script",
        materials=[Material(kind=MaterialKind.CODE, content="def broken(:\n")],
    )
    session.current_skill.script = SCRIPT
    backend_main.sessions[session.id] = session

    form = client.get(f"/api/sessions/{session.id}/skill-form").json()

    assert (form["form"], form["locked"], form["failures"]) == ("script", True, [])
    assert [problem["check"] for problem in form["replacement_problems"]] == ["parses"]


def test_skill_list_marks_script_skills(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "room-finder")
    _seed(backend_main, grant_skill, "plain-skill", script=None)

    listed = {entry["name"]: entry["has_script"] for entry in client.get("/api/skills").json()}

    assert listed == {"room-finder": True, "plain-skill": False}


def test_save_is_refused_when_the_flags_were_turned_off(client, backend_main, fake_sql, monkeypatch) -> None:
    calls = _aca_lookup(
        backend_main, monkeypatch, {"architectural_config": {"DYNAMIC_SKILLS_ENABLED": "true", "SKILL_SCRIPTS_ENABLED": "false"}}
    )
    session = _script_session(backend_main, "room-finder")
    session.aca_env_result = FLAGS_ON

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert calls == ["script_save"]
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (detail["kind"], detail["recoverable"], detail["flags"]) == ("script_flags_off", True, ["SKILL_SCRIPTS_ENABLED"])
    assert fake_sql.skills == {}
    assert not backend_main.store.has_script("room-finder")


def test_inline_save_does_not_look_up_the_flags(client, backend_main, fake_sql, monkeypatch) -> None:
    calls = _aca_lookup(backend_main, monkeypatch, FLAGS_ON)
    session = _script_session(backend_main, "room-finder", script=None)

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 200
    assert calls == []


def test_skill_form_endpoint_lists_the_unmet_conditions(client, backend_main) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    backend_main.await_prepare_entry()
    backend_main.sessions[session_id].aca_env_result = FLAGS_ON

    form = client.get(f"/api/sessions/{session_id}/skill-form").json()

    assert (form["form"], form["locked"]) == ("inline", False)
    assert [f["check"] for f in form["failures"]] == ["code_material", "covers_operations"]


def test_coverage_confirmation_reopens_variables_ok(client, backend_main) -> None:
    session = backend_main.Session(materials=[Material(kind=MaterialKind.CODE, content=SCRIPT)])
    session.prepare_brief.verify_checklist["variables_ok"] = True
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/variables", json={"variables": [], "script_covers_operations": True}
    )

    brief = response.json()["prepare_brief"]
    assert brief["script_covers_operations"] is True
    assert brief["verify_checklist"]["variables_ok"] is False


def test_locked_form_refuses_a_switch(client, backend_main) -> None:
    session = _script_session(backend_main, "room-finder")
    session.skill_form = "script"
    session.prepare_brief.script_covers_operations = True

    response = client.post(
        f"/api/sessions/{session.id}/variables", json={"variables": [], "script_covers_operations": False}
    )
    unchanged = client.post(
        f"/api/sessions/{session.id}/variables", json={"variables": [], "script_covers_operations": True}
    )

    assert response.status_code == 409
    assert "locked as script" in response.json()["detail"]
    assert unchanged.status_code == 200
    assert backend_main.sessions[session.id].prepare_brief.script_covers_operations is True
    with pytest.raises(ValueError, match="locked as script"):
        backend_main.apply_tool_effect(session, "record_variables", {"variables": [], "script_covers_operations": False})


def test_inline_choice_is_recorded_until_the_lock(client, backend_main) -> None:
    session = backend_main.Session(materials=[Material(kind=MaterialKind.CODE, content=SCRIPT)])
    session.prepare_brief.verify_checklist["variables_ok"] = True
    backend_main.sessions[session.id] = session

    response = client.post(f"/api/sessions/{session.id}/variables", json={"variables": [], "prefer_inline": True})

    brief = response.json()["prepare_brief"]
    assert (brief["prefer_inline"], brief["verify_checklist"]["variables_ok"]) == (True, True)
    backend_main.sessions[session.id].skill_form = "inline"
    locked = client.post(f"/api/sessions/{session.id}/variables", json={"variables": [], "prefer_inline": False})
    assert locked.status_code == 409 and "prefer_inline" in locked.json()["detail"]


def test_record_variables_without_a_list_keeps_the_variables(backend_main) -> None:
    session = backend_main.Session(materials=[Material(kind=MaterialKind.CODE, content=SCRIPT)])
    backend_main.apply_tool_effect(
        session, "record_variables", {"variables": [{"name": "ROOM", "kind": "runtime", "source": "request"}]}
    )

    backend_main.apply_tool_effect(session, "record_variables", {"script_covers_operations": True, "prefer_inline": True})

    brief = session.prepare_brief
    assert [v.name for v in brief.variables] == ["ROOM"]
    assert (brief.script_covers_operations, brief.prefer_inline) == (True, True)


def test_draft_update_keeps_the_server_side_script(client, backend_main) -> None:
    session = _script_session(backend_main, "room-finder")

    response = client.put(
        f"/api/sessions/{session.id}/draft",
        json={"skill_md": _skill_md("room-finder") + "\nEdited.\n", "script": "print('injected')\n"},
    )

    assert response.status_code == 200
    assert backend_main.sessions[session.id].current_skill.script == SCRIPT


def test_rename_moves_the_script_with_the_skill(client, backend_main, grant_skill, fake_sql, monkeypatch) -> None:
    _lint_recorder(backend_main, monkeypatch)
    files = _seed(backend_main, grant_skill, "old-skill")
    session = _script_session(backend_main, "old-skill")
    session.remote_skill_id = session.target_skill_id = "old-skill"
    session.remote_version_hash = files.version_hash
    session.pending_tool_calls.append(
        PendingToolCall(call_id="call_rename", tool="rename_skill", args={"new_name": "new-skill", "reason": "asked"})
    )

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_rename", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    assert backend_main.store.load_skill("new-skill").script == SCRIPT
    assert not backend_main.store.has_script("old-skill")
    assert "old-skill" not in fake_sql.skills


def test_save_refuses_to_overwrite_a_concurrent_blob_change(client, backend_main, grant_skill, fake_sql) -> None:
    files = _seed(backend_main, grant_skill, "room-finder", script=None)
    session = _script_session(backend_main, "room-finder", script=None)
    session.remote_skill_id = "room-finder"
    session.remote_version_hash = files.version_hash
    gatekeeper_md = _skill_md("room-finder") + "\n## Gatekeeper Addendum\n\nLearned.\n"
    backend_main.store.save_skill(SkillFiles(name="room-finder", skill_md=gatekeeper_md))
    updated_at = fake_sql.skills["room-finder"]["updated_at"]

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "version_conflict"
    assert backend_main.store.load_skill("room-finder").skill_md == gatekeeper_md
    assert fake_sql.skills["room-finder"]["updated_at"] == updated_at


def test_neighbor_save_refuses_to_overwrite_a_concurrent_blob_change(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "peer-skill", script=None)
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.post(
        f"/api/sessions/{session_id}/neighbor-edits/peer-skill/propose",
        json={"skill_md": _skill_md("peer-skill") + "\nEdited.\n", "label": "v1"},
    )
    gatekeeper_md = _skill_md("peer-skill") + "\n## Gatekeeper Addendum\n\nLearned.\n"
    backend_main.store.save_skill(SkillFiles(name="peer-skill", skill_md=gatekeeper_md))

    response = client.post(f"/api/sessions/{session_id}/neighbor-edits/peer-skill/save", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "version_conflict"
    assert backend_main.store.load_skill("peer-skill").skill_md == gatekeeper_md


def test_neighbor_save_leaves_the_script_untouched(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "peer-skill")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.post(
        f"/api/sessions/{session_id}/neighbor-edits/peer-skill/propose",
        json={"skill_md": _skill_md("peer-skill") + "\nEdited.\n", "label": "v1"},
    )

    response = client.post(f"/api/sessions/{session_id}/neighbor-edits/peer-skill/save", json={})

    assert response.status_code == 200
    assert backend_main.store.load_skill("peer-skill").script == SCRIPT


def test_session_lint_and_save_use_the_script_form_rules(client, backend_main, fake_sql, monkeypatch) -> None:
    calls = _lint_recorder(backend_main, monkeypatch)
    script = 'import argparse, json\np = argparse.ArgumentParser()\nprint(json.dumps({"status": "ok"}))\n'
    session = _script_session(backend_main, "room-finder", script=script)

    topology = client.get(f"/api/sessions/{session.id}/topology").json()
    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert [item["rule"] for item in topology["lint"]] == ["S9", "S10b"]
    assert response.status_code == 400
    assert "S10b" in response.json()["detail"]["message"]
    assert calls == [] and fake_sql.skills == {}


# --- conftest ACA lookup fake -------------------------------------------------


def test_the_default_aca_lookup_keeps_the_session_result(client, backend_main) -> None:
    with_flags = _script_session(backend_main, "room-finder")
    without = backend_main.Session()
    backend_main.sessions[without.id] = without

    assert client.post(f"/api/sessions/{with_flags.id}/aca-env").json()["aca_env_result"] == FLAGS_ON
    assert client.post(f"/api/sessions/{without.id}/aca-env").json()["aca_env_result"] is None


def test_the_default_aca_lookup_never_turns_the_flags_on(client, backend_main, fake_sql) -> None:
    session = _script_session(backend_main, "room-finder")
    session.aca_env_result = None

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["kind"] == "script_flags_off"


# --- code material changes ----------------------------------------------------

NEW_SCRIPT = 'import json\nprint(json.dumps({"status": "done"}))\n'


class _SilentAgent:
    def stream(self, session, message):
        return iter(())


def _post(kind: str, content: str):
    return lambda client, sid: client.post(f"/api/sessions/{sid}/materials", json={"kind": kind, "content": content})


def _put(material_id: str, kind: str, content: str):
    return lambda client, sid: client.put(
        f"/api/sessions/{sid}/materials/{material_id}", json={"kind": kind, "content": content}
    )


def _delete(material_id: str):
    return lambda client, sid: client.delete(f"/api/sessions/{sid}/materials/{material_id}")


def _chat(kind: str, content: str):
    return lambda client, sid: client.post(
        f"/api/sessions/{sid}/chat", json={"materials": [{"kind": kind, "content": content}]}
    )


def _code(material_id: str, content: str = SCRIPT) -> Material:
    return Material(id=material_id, kind=MaterialKind.CODE, content=content)


def _text(material_id: str, content: str = "notes") -> Material:
    return Material(id=material_id, kind=MaterialKind.TEXT, content=content)


def _confirmed_session(backend_main, monkeypatch, materials, *, skill_form=None, script=None, mode="new"):
    monkeypatch.setattr(backend_main, "agent", _SilentAgent())
    session = backend_main.Session(mode=mode, materials=materials, skill_form=skill_form)
    session.current_skill.script = script
    brief = session.prepare_brief
    brief.script_covers_operations = True
    brief.verify_checklist["variables_ok"] = True
    brief.verify_evidence["variables_ok"] = "user confirmed"
    backend_main.sessions[session.id] = session
    return session


def _confirmation(session) -> tuple[bool, bool, bool]:
    brief = session.prepare_brief
    return brief.script_covers_operations, brief.verify_checklist["variables_ok"], "variables_ok" in brief.verify_evidence


def _system_messages(session) -> list[str]:
    return [m.content for m in session.conversation if m.role == "system"]


@pytest.mark.parametrize(
    "change",
    [
        _post("code", NEW_SCRIPT),
        _put("c1", "code", NEW_SCRIPT),
        _put("t1", "code", "notes"),
        _put("c1", "text", SCRIPT),
        _delete("c1"),
        _chat("code", NEW_SCRIPT),
    ],
    ids=["post", "put-content", "put-text-to-code", "put-code-to-text", "delete", "chat"],
)
def test_a_code_material_change_before_the_lock_reopens_the_coverage_confirmation(
    client, backend_main, monkeypatch, change
) -> None:
    session = _confirmed_session(backend_main, monkeypatch, [_code("c1"), _text("t1")])

    assert change(client, session.id).status_code == 200
    assert _confirmation(backend_main.sessions[session.id]) == (False, False, False)
    assert _system_messages(backend_main.sessions[session.id]) == [CODE_MATERIAL_EDITED_MESSAGE]


@pytest.mark.parametrize(
    "change",
    [
        _post("text", "more notes"),
        _put("c1", "code", SCRIPT),
        _put("t1", "text", "edited notes"),
        _delete("t1"),
        _chat("text", "more notes"),
    ],
    ids=["post-text", "put-same-code", "put-text", "delete-text", "chat-text"],
)
def test_a_non_code_change_keeps_the_coverage_confirmation(client, backend_main, monkeypatch, change) -> None:
    session = _confirmed_session(backend_main, monkeypatch, [_code("c1"), _text("t1")])

    assert change(client, session.id).status_code == 200
    assert _confirmation(backend_main.sessions[session.id]) == (True, True, True)
    assert _system_messages(backend_main.sessions[session.id]) == []


def test_a_code_change_after_an_inline_lock_changes_nothing(client, backend_main, monkeypatch) -> None:
    session = _confirmed_session(backend_main, monkeypatch, [_code("c1")], skill_form="inline")

    _put("c1", "code", NEW_SCRIPT)(client, session.id)

    stored = backend_main.sessions[session.id]
    assert _confirmation(stored) == (True, True, True)
    assert stored.current_skill.script is None and _system_messages(stored) == []


@pytest.mark.parametrize("mode", ["new", "modify"])
@pytest.mark.parametrize(
    ("materials", "change"),
    [
        ([], _post("code", NEW_SCRIPT)),
        ([_code("c1")], _put("c1", "code", NEW_SCRIPT)),
        ([_text("t1", NEW_SCRIPT)], _put("t1", "code", NEW_SCRIPT)),
        ([_code("c1"), _code("c2", NEW_SCRIPT)], _delete("c1")),
        ([], _chat("code", NEW_SCRIPT)),
    ],
    ids=["post", "put-content", "put-text-to-code", "delete-old", "chat"],
)
def test_a_clean_new_code_material_replaces_the_locked_script(
    client, backend_main, monkeypatch, materials, change, mode
) -> None:
    session = _confirmed_session(backend_main, monkeypatch, materials, skill_form="script", script=SCRIPT, mode=mode)

    assert change(client, session.id).status_code == 200

    stored = backend_main.sessions[session.id]
    assert stored.current_skill.script == NEW_SCRIPT
    assert _system_messages(stored) == [SCRIPT_REPLACED_MESSAGE]
    assert _confirmation(stored) == (True, True, True)
    assert "did not replace the script" not in build_system_prompt(stored)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_post("code", NEW_SCRIPT), "- code_material: Exactly one code material is required to replace the script; there are 2."),
        (_put("c1", "code", "def broken(:\n"), "- parses: The code material is not valid Python:"),
        (_put("c1", "code", 'import sys\nprint("hi")\nsys.exit(2)\n'), "- script_lint (S4):"),
        (_put("c1", "code", SCRIPT), None),
        (_delete("c1"), None),
    ],
    ids=["two-code-materials", "does-not-parse", "script-lint", "same-content", "removed"],
)
def test_the_locked_script_is_kept_when_the_code_material_cannot_replace_it(
    client, backend_main, monkeypatch, change, reason
) -> None:
    session = _confirmed_session(backend_main, monkeypatch, [_code("c1")], skill_form="script", script=SCRIPT)

    assert change(client, session.id).status_code == 200

    stored = backend_main.sessions[session.id]
    prompt = build_system_prompt(stored)
    assert stored.current_skill.script == SCRIPT and _system_messages(stored) == []
    if reason is None:
        assert "did not replace the script" not in prompt
    else:
        assert "The code material did not replace the script; the previous script is kept:" in prompt
        assert reason in prompt


# --- flag lookup before the form locks ----------------------------------------


def _awaiting_session(backend_main, monkeypatch):
    session = backend_main.Session(
        materials=[Material(kind=MaterialKind.CODE, content=SCRIPT)],
        aca_env_error="HTTP 404 Not Found | <html>Azure Container App - Unavailable</html>",
    )
    monkeypatch.setattr(state_machine, "check_quality_gates", lambda _session: [])
    return session


def _to_draft(backend_main, session) -> None:
    backend_main.apply_tool_effect(session, "request_stage_transition", {"target_stage": "draft"})


def test_a_lookup_that_now_allows_scripts_holds_prepare_open_once(backend_main, monkeypatch) -> None:
    calls = _aca_lookup(backend_main, monkeypatch, FLAGS_ON)
    session = _awaiting_session(backend_main, monkeypatch)

    with pytest.raises(backend_main.FormLockDeferred) as exc:
        _to_draft(backend_main, session)

    assert calls == ["form_lock"]
    assert session.current_stage == "prepare" and session.skill_form is None
    assert backend_main._tool_effect_recovery_guidance(session, "request_stage_transition", {}, exc.value) == (
        backend_main.FORM_LOCK_DEFERRED_MESSAGE
    )

    _to_draft(backend_main, session)

    assert calls == ["form_lock"]
    assert session.current_stage == "draft" and session.skill_form == "inline"


def test_a_lookup_that_fails_again_locks_inline_and_says_why(backend_main, monkeypatch) -> None:
    calls = _aca_lookup(backend_main, monkeypatch, None)
    session = _awaiting_session(backend_main, monkeypatch)

    _to_draft(backend_main, session)

    assert calls == ["form_lock"]
    assert session.skill_form == "inline"
    assert any(
        "now locked as inline" in m.content and "Could not read the EAA script flags" in m.content
        for m in session.conversation
    )
