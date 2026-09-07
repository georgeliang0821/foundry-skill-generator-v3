from __future__ import annotations

from backend.models import TestResult as ModelTestResult
from backend.models import TestRun as ModelTestRun


def test_save_session_skill_then_list_and_load(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{session_id}/draft",
        json={
            "skill_md": "---\nname: demo-skill\ndescription: Demo skill\n---\n",
        },
    )

    saved = client.post(f"/api/sessions/{session_id}/save", json={"name": "Demo Skill"})
    listed = client.get("/api/skills")
    loaded = client.get("/api/skills/demo-skill")

    assert saved.status_code == 200
    assert saved.json()["remote_skill_id"] == "demo-skill"
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "demo-skill"
    assert loaded.status_code == 200
    assert loaded.json()["skill_md"].startswith("---")


def test_save_empty_skill_returns_400(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(f"/api/sessions/{session_id}/save", json={})

    assert response.status_code == 400


def test_create_modify_session_loads_existing_skill(client, backend_main, grant_skill) -> None:
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="demo-skill",
            skill_md="---\nname: demo-skill\ndescription: Demo\n---\n",
        )
    )
    grant_skill("demo-skill", description="Demo")

    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo-skill", "materials": []})

    assert response.status_code == 200
    assert response.json()["remote_skill_id"] == "demo-skill"
    assert response.json()["current_skill"]["skill_md"].startswith("---")


def test_create_modify_session_missing_skill_returns_404(client) -> None:
    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "missing", "materials": []})

    assert response.status_code == 404


def test_skill_test_uses_inline_content_and_mocked_runner(client, backend_main, monkeypatch, grant_skill) -> None:
    grant_skill("inline-skill")
    def fake_run_selection_tests(skill_content, positive_samples, negative_samples, **kwargs):
        assert skill_content == "---\nname: inline-skill\n---\n"
        assert positive_samples == ["use it"]
        assert negative_samples == ["skip it"]
        return ModelTestRun(
            positive_results=[
                ModelTestResult(query="use it", expected_skill="inline-skill", actual_skill="inline-skill", passed=True)
            ],
            negative_results=[
                ModelTestResult(query="skip it", expected_skill=None, actual_skill=None, passed=True)
            ],
            positive_hit_rate=1.0,
            negative_correct_reject_rate=1.0,
        )

    monkeypatch.setattr(backend_main, "run_selection_tests", fake_run_selection_tests)

    response = client.post(
        "/api/skills/inline-skill/test",
        json={
            "skill_content": "---\nname: inline-skill\n---\n",
            "positive_samples": ["use it"],
            "negative_samples": ["skip it"],
        },
    )

    assert response.status_code == 200
    assert response.json()["positive_hit_rate"] == 1.0
