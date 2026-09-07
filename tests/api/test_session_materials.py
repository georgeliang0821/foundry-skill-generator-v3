from __future__ import annotations


def _create_session(client) -> str:
    response = client.post("/api/sessions", json={"mode": "new", "materials": []})
    assert response.status_code == 200
    return response.json()["id"]


def test_add_material_appends_with_generated_id(client) -> None:
    session_id = _create_session(client)

    response = client.post(
        f"/api/sessions/{session_id}/materials",
        json={"kind": "text", "content": "Some context", "metadata": {"label": "ctx"}},
    )

    body = response.json()
    assert response.status_code == 200
    materials = body["materials"]
    assert len(materials) == 1
    item = materials[0]
    assert item["id"]
    assert item["kind"] == "text"
    assert item["content"] == "Some context"
    assert item["metadata"]["label"] == "ctx"

    # Persistence: re-read the session and confirm.
    read = client.get(f"/api/sessions/{session_id}")
    assert read.status_code == 200
    assert read.json()["materials"][0]["id"] == item["id"]


def test_update_material_preserves_id_and_created_at(client) -> None:
    session_id = _create_session(client)
    add = client.post(
        f"/api/sessions/{session_id}/materials",
        json={"kind": "text", "content": "Original"},
    ).json()
    material = add["materials"][0]
    material_id = material["id"]
    created_at = material["created_at"]

    response = client.put(
        f"/api/sessions/{session_id}/materials/{material_id}",
        json={"kind": "code", "content": "print('hi')", "metadata": {"lang": "py"}},
    )

    body = response.json()
    assert response.status_code == 200
    updated = next(m for m in body["materials"] if m["id"] == material_id)
    assert updated["kind"] == "code"
    assert updated["content"] == "print('hi')"
    assert updated["metadata"]["lang"] == "py"
    assert updated["created_at"] == created_at


def test_update_material_unknown_id_returns_404(client) -> None:
    session_id = _create_session(client)

    response = client.put(
        f"/api/sessions/{session_id}/materials/does-not-exist",
        json={"kind": "text", "content": "x"},
    )

    assert response.status_code == 404


def test_delete_material_removes_entry(client) -> None:
    session_id = _create_session(client)
    add = client.post(
        f"/api/sessions/{session_id}/materials",
        json={"kind": "text", "content": "to remove"},
    ).json()
    material_id = add["materials"][0]["id"]

    response = client.delete(f"/api/sessions/{session_id}/materials/{material_id}")

    body = response.json()
    assert response.status_code == 200
    assert all(m["id"] != material_id for m in body["materials"])

    read = client.get(f"/api/sessions/{session_id}").json()
    assert all(m["id"] != material_id for m in read["materials"])


def test_delete_material_unknown_id_returns_404(client) -> None:
    session_id = _create_session(client)

    response = client.delete(f"/api/sessions/{session_id}/materials/missing-id")

    assert response.status_code == 404


def test_add_material_missing_session_returns_404(client) -> None:
    response = client.post(
        "/api/sessions/no-such-session/materials",
        json={"kind": "text", "content": "x"},
    )

    assert response.status_code == 404


def test_topology_reports_sample_code_that_drifted_from_a_code_material(
    client, backend_main
) -> None:
    session_id = _create_session(client)
    client.post(
        f"/api/sessions/{session_id}/materials",
        json={"kind": "code", "content": "def submit_leave(conn):\n    return conn\n"},
    )
    backend_main.sessions[session_id].current_skill.skill_md = (
        "## API Reference / Sample Code\n\n"
        '```python\ndef main():\n    cursor.execute("EXEC hr.invented_proc")\n```\n'
    )

    response = client.get(f"/api/sessions/{session_id}/topology")

    assert response.status_code == 200
    fidelity = response.json()["fidelity"]
    assert {issue["rule"] for issue in fidelity} == {
        "dropped_from_material",
        "invented_sql_object",
    }
    assert all(issue["severity"] == "warning" for issue in fidelity)


def test_topology_has_no_fidelity_warnings_without_materials(client, backend_main) -> None:
    session_id = _create_session(client)
    backend_main.sessions[session_id].current_skill.skill_md = (
        '```python\ndef main():\n    cursor.execute("EXEC hr.invented_proc")\n```\n'
    )

    response = client.get(f"/api/sessions/{session_id}/topology")

    assert response.json()["fidelity"] == []
