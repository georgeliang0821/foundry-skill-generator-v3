from __future__ import annotations


def test_apply_patch_api_without_session(client) -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ hello
-hello
+hi
*** End Patch"""

    response = client.post(
        "/api/patch/apply",
        json={"target_file": "SKILL.md", "current_content": "hello", "patch": patch},
    )

    assert response.status_code == 200
    assert response.json()["updated_content"] == "hi"


def test_apply_patch_api_version_mismatch_returns_409(client) -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ hello
-hello
+hi
*** End Patch"""

    response = client.post(
        "/api/patch/apply",
        json={
            "target_file": "SKILL.md",
            "current_content": "hello",
            "patch": patch,
            "expected_version_hash": "stale",
        },
    )

    assert response.status_code == 409


def test_apply_patch_api_with_session_updates_current_draft(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{session_id}/draft",
        json={"skill_md": "hello"},
    )
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ hello
-hello
+hi
*** End Patch"""

    response = client.post(
        "/api/patch/apply",
        json={
            "session_id": session_id,
            "target_file": "SKILL.md",
            "current_content": "hello",
            "patch": patch,
        },
    )

    session = client.get(f"/api/sessions/{session_id}").json()
    assert response.status_code == 200
    assert session["current_skill"]["skill_md"] == "hi"


def test_apply_patch_rejects_non_skill_md_target(client) -> None:
    response = client.post(
        "/api/patch/apply",
        json={"target_file": "env.yaml", "current_content": "env", "patch": "*** Begin Patch\n*** End Patch"},
    )

    assert response.status_code == 422
