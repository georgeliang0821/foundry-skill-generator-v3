from __future__ import annotations

import base64

from backend.models import PendingToolCall, SkillAsset, SkillFiles, SkillKind


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _session(backend_main, **fields):
    session = backend_main.Session(**fields)
    backend_main.sessions[session.id] = session
    return session


def _skill_md(name: str, asset_paths=()) -> str:
    md = f"---\nname: {name}\ndescription: Does a thing.\n---\n\n## Overview\n\nBody.\n"
    if asset_paths:
        md += "\n## Skill Resources\n\n" + "".join(f"- `{path}` -- data.\n" for path in asset_paths)
    return md


def _seed(backend_main, grant_skill, name: str, assets: dict[str, str]) -> SkillFiles:
    saved = backend_main.store.save_skill(
        SkillFiles(name=name, skill_md=_skill_md(name, list(assets)), assets=assets)
    )
    grant_skill(name)
    return saved


def _open_modify(client, name: str):
    return client.post("/api/sessions", json={"mode": "modify", "target_skill_id": name, "materials": []})


def test_upload_returns_metadata_only_and_persists_content(client, backend_main) -> None:
    session = _session(backend_main)

    response = client.post(
        f"/api/sessions/{session.id}/assets", json={"path": "assets/style.css", "content_base64": _b64(b"body{}")}
    )

    assert response.status_code == 200
    (asset,) = response.json()["assets"]
    assert asset["path"] == "assets/style.css" and asset["size"] == 6 and "content" not in asset
    assert backend_main.session_store.load(session.id).assets[0].content == "body{}"


def test_upload_to_the_same_path_replaces_it(client, backend_main) -> None:
    session = _session(backend_main)
    url = f"/api/sessions/{session.id}/assets"
    client.post(url, json={"path": "assets/a.css", "content_base64": _b64(b"old")})

    response = client.post(url, json={"path": "assets/a.css", "content_base64": _b64(b"newer")})

    assert [(a["path"], a["size"]) for a in response.json()["assets"]] == [("assets/a.css", 5)]


def test_upload_rejects_a_bad_file_with_its_reason(client, backend_main) -> None:
    session = _session(backend_main)

    response = client.post(
        f"/api/sessions/{session.id}/assets", json={"path": "assets/logo.png", "content_base64": _b64(b"\x89PNG")}
    )

    assert response.status_code == 422
    assert "binary format" in response.json()["detail"]
    assert backend_main.sessions[session.id].assets == []


def test_upload_rejects_a_tenth_file(client, backend_main) -> None:
    session = _session(backend_main)
    url = f"/api/sessions/{session.id}/assets"
    for i in range(9):
        assert client.post(url, json={"path": f"assets/f{i}.txt", "content_base64": _b64(b"x")}).status_code == 200

    response = client.post(url, json={"path": "assets/f9.txt", "content_base64": _b64(b"x")})

    assert response.status_code == 422
    assert "limit is 9" in response.json()["detail"]


def test_upload_rejects_invalid_base64(client, backend_main) -> None:
    session = _session(backend_main)

    response = client.post(f"/api/sessions/{session.id}/assets", json={"path": "assets/a.css", "content_base64": "!!"})

    assert response.status_code == 422


def test_upload_is_refused_for_a_scenario_skill(client, backend_main) -> None:
    session = _session(backend_main, skill_kind=SkillKind.SCENARIO)

    response = client.post(
        f"/api/sessions/{session.id}/assets", json={"path": "assets/a.css", "content_base64": _b64(b"a")}
    )

    assert response.status_code == 409


def test_delete_asset(client, backend_main) -> None:
    session = _session(backend_main)
    client.post(f"/api/sessions/{session.id}/assets", json={"path": "references/b.md", "content_base64": _b64(b"b")})

    assert client.delete(f"/api/sessions/{session.id}/assets/references/b.md").json()["assets"] == []
    assert client.delete(f"/api/sessions/{session.id}/assets/references/b.md").status_code == 404


def test_another_users_session_is_not_found(client, backend_main) -> None:
    session = _session(backend_main, owner_upn="someone-else@example.com")

    response = client.post(
        f"/api/sessions/{session.id}/assets", json={"path": "assets/a.css", "content_base64": _b64(b"a")}
    )

    assert response.status_code == 404


def test_modify_session_loads_the_stored_assets(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "demo", {"assets/style.css": "body{}", "references/notes.md": "n"})

    response = _open_modify(client, "demo")

    assert response.status_code == 200
    assert [a["path"] for a in response.json()["assets"]] == ["assets/style.css", "references/notes.md"]
    session = backend_main.sessions[response.json()["id"]]
    assert session.assets[0].content == "body{}" and session.assets_synced


def test_modify_session_is_refused_when_a_stored_file_cannot_be_carried(client, backend_main, grant_skill) -> None:
    _seed(backend_main, grant_skill, "demo", {})
    backend_main.store.store._assets["demo"] = {"assets/logo.png": b"\x89PNG", "assets/sub/x.css": b"x"}

    response = _open_modify(client, "demo")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["kind"] == "asset_rejected" and len(detail["problems"]) == 2


def test_save_writes_exactly_the_session_asset_set(client, backend_main, grant_skill, fake_sql) -> None:
    _seed(backend_main, grant_skill, "demo", {"assets/a.css": "a", "assets/b.css": "b"})
    session_id = _open_modify(client, "demo").json()["id"]
    client.delete(f"/api/sessions/{session_id}/assets/assets/a.css")
    client.post(f"/api/sessions/{session_id}/assets", json={"path": "references/c.md", "content_base64": _b64(b"c")})
    backend_main.sessions[session_id].current_skill.skill_md = _skill_md("demo", ["assets/b.css", "references/c.md"])

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 200
    assert backend_main.store.load_assets("demo") == {"assets/b.css": b"b", "references/c.md": b"c"}


def test_save_is_blocked_until_skill_md_names_every_asset(client, backend_main, grant_skill, fake_sql) -> None:
    _seed(backend_main, grant_skill, "demo", {"assets/a.css": "a"})
    session_id = _open_modify(client, "demo").json()["id"]
    backend_main.sessions[session_id].current_skill.skill_md = _skill_md("demo").replace(
        "Body.", 'Body. read_skill_resource(skill_name="demo", resource_name="a.css")'
    )

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 400
    assert "F2: The asset `assets/a.css`" in response.json()["detail"]["message"]


def test_save_without_assets_sends_an_empty_set_for_a_new_skill(client, backend_main, fake_sql) -> None:
    session = _session(backend_main, current_stage="refine")
    session.current_skill.skill_md = _skill_md("fresh")

    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 200
    assert backend_main.store.load_assets("fresh") == {}
    assert backend_main.sessions[session.id].assets_synced


def test_unsynced_session_keeps_the_stored_assets_on_save(client, backend_main, grant_skill, fake_sql) -> None:
    files = _seed(backend_main, grant_skill, "demo", {"assets/a.css": "a"})
    session = _session(backend_main, current_stage="refine", mode="modify", remote_skill_id="demo")
    session.current_skill.skill_md = _skill_md("demo", ["assets/a.css", "assets/b.css"])
    session.remote_version_hash = files.version_hash
    session.assets = [SkillAsset(path="assets/b.css", content="b")]

    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 200
    assert backend_main.store.load_assets("demo") == {"assets/a.css": b"a", "assets/b.css": b"b"}


def test_save_rejects_an_asset_set_the_skill_cannot_carry(client, backend_main, fake_sql) -> None:
    session = _session(backend_main, current_stage="refine", skill_form="script")
    session.current_skill.skill_md = _skill_md("fresh")
    session.assets = [SkillAsset(path="assets/a.css", content="a")]

    response = client.post(f"/api/sessions/{session.id}/save", json={})

    assert response.status_code == 400
    assert "Skill assets rejected" in response.json()["detail"]
    assert "fresh" not in fake_sql.skills


def test_rename_moves_the_whole_asset_set(client, backend_main, grant_skill, fake_sql) -> None:
    _seed(backend_main, grant_skill, "old-skill", {"assets/a.css": "a"})
    session_id = _open_modify(client, "old-skill").json()["id"]
    backend_main.sessions[session_id].pending_tool_calls.append(
        PendingToolCall(call_id="call_rename", tool="rename_skill", args={"new_name": "new-skill", "reason": "asked"})
    )

    response = client.post(
        f"/api/sessions/{session_id}/tool-result", json={"tool_call_id": "call_rename", "result": {"action": "accept"}}
    )

    assert response.status_code == 200
    assert backend_main.store.load_assets("new-skill") == {"assets/a.css": b"a"}
    assert backend_main.store.load_assets("old-skill") == {}


def test_eaa_lint_receives_only_skill_md(client, backend_main, grant_skill, fake_sql, monkeypatch) -> None:
    sent: list[dict] = []

    def _lint(skill_name, files, **kwargs):
        sent.append(dict(files))
        return backend_main.EaaLintResult(valid=True, errors=[], warnings=[], ruleset_version="test")

    monkeypatch.setattr(backend_main, "lint_skill_package", _lint)
    _seed(backend_main, grant_skill, "demo", {"assets/a.css": "a"})
    session_id = _open_modify(client, "demo").json()["id"]

    assert client.post(f"/api/sessions/{session_id}/save", json={}).status_code == 200
    assert [list(files) for files in sent] == [["SKILL.md"]]
