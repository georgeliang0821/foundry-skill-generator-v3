from __future__ import annotations

from backend.models import TestResult as ModelTestResult
from backend.models import TestRun as ModelTestRun


def test_session_test_requires_saved_remote_binding(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{session_id}/draft",
        json={"skill_md": "---\nname: demo-skill\n---\n"},
    )

    response = client.post(f"/api/sessions/{session_id}/test", json={"source": "current"})

    assert response.status_code == 409
    assert "Save" in response.json()["detail"]


def test_session_test_uses_checklist_samples_and_moves_to_test(client, backend_main, monkeypatch) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    skill_md = "---\nname: demo-skill\ndescription: Demo\n---\n"
    draft = client.put(
        f"/api/sessions/{session_id}/draft",
        json={"skill_md": skill_md},
    ).json()
    session = backend_main.sessions[session_id]
    session.remote_skill_id = "demo-skill"
    session.remote_version_hash = draft["current_skill"]["version_hash"]
    session.verify_checklist.test_samples.content = {
        "positive": ["use demo"],
        "negative": ["tell joke"],
    }

    def fake_run_selection_tests(skill_content, positive_samples, negative_samples, **kwargs):
        assert skill_content == skill_md
        assert positive_samples == ["use demo"]
        assert negative_samples == ["tell joke"]
        return ModelTestRun(
            skill_version_hash=kwargs["version_hash"],
            positive_results=[
                ModelTestResult(query="use demo", expected_skill="demo-skill", actual_skill="demo-skill", passed=True)
            ],
            negative_results=[
                ModelTestResult(query="tell joke", expected_skill=None, actual_skill=None, passed=True)
            ],
            positive_hit_rate=1.0,
            negative_correct_reject_rate=1.0,
        )

    monkeypatch.setattr(backend_main, "run_selection_tests", fake_run_selection_tests)

    response = client.post(f"/api/sessions/{session_id}/test", json={"source": "current"})

    body = response.json()
    assert response.status_code == 200
    assert body["current_stage"] == "test"
    assert body["test_runs"][0]["positive_hit_rate"] == 1.0
