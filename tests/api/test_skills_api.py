from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("neighbor", [False, True])
def test_save_backup_failure_returns_503_without_live_write(client, backend_main, grant_skill, monkeypatch, neighbor) -> None:
    original = "---\nname: demo-skill\ndescription: Demo\n---\nOriginal"
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo-skill", skill_md=original))
    grant_skill("demo-skill")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo-skill"}).json()["id"]
    replacement = original.replace("Original", "Changed")
    if neighbor:
        assert client.post(f"/api/sessions/{sid}/neighbor-edits/demo-skill/propose", json={"skill_md": replacement}).status_code == 200
        endpoint = f"/api/sessions/{sid}/neighbor-edits/demo-skill/save"
    else:
        client.put(f"/api/sessions/{sid}/draft", json={"skill_md": replacement})
        endpoint = f"/api/sessions/{sid}/save"
    def fail(files, expected_version_hash="", *, create_only=False, captured_by=""):
        assert captured_by == "test@example.com"
        raise backend_main.SkillBackupError("Skill backup failed; live content was not saved.")
    monkeypatch.setattr(backend_main.store, "save_skill", fail)
    response = client.post(endpoint, json={})
    assert response.status_code == 503
    assert "backup failed" in response.json()["detail"]
    assert backend_main.store.load_skill("demo-skill").skill_md == original


def test_new_session_second_save_captures_previous_content(client, backend_main) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    original = "---\nname: demo-skill\ndescription: Demo\n---\nOriginal"
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": original})
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 200
    assert not local.history
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": original.replace("Original", "Changed")})
    assert client.post(f"/api/sessions/{sid}/save", json={}).status_code == 200
    backup = next(iter(local.history.values()))
    assert backup["files"]["SKILL.md"] == original.encode("utf-8")
    assert backup["manifest"]["captured_by"] == "test@example.com"


@pytest.mark.parametrize("neighbor", [False, True])
def test_save_versions_new_content_but_preserves_old_snapshot(client, backend_main, grant_skill, neighbor) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    original = '---\nname: demo-skill\ndescription: Demo\nmetadata:\n  version: "1.6"\n---\nOriginal'
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo-skill", skill_md=original))
    grant_skill("demo-skill")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo-skill"}).json()["id"]
    changed = original.replace("Original", "Changed")
    if neighbor:
        client.post(f"/api/sessions/{sid}/neighbor-edits/demo-skill/propose", json={"skill_md": changed})
        endpoint = f"/api/sessions/{sid}/neighbor-edits/demo-skill/save"
    else:
        client.put(f"/api/sessions/{sid}/draft", json={"skill_md": changed})
        endpoint = f"/api/sessions/{sid}/save"

    response = client.post(endpoint, json={})
    assert response.status_code == 200, response.text
    expected = changed.replace('"1.6"', '"1.6.1"')
    assert backend_main.store.load_skill("demo-skill").skill_md == expected
    backup = next(iter(local.history.values()))
    assert backup["files"]["SKILL.md"] == original.encode("utf-8")
    if neighbor:
        edit = backend_main.sessions[sid].neighbor_edits[0]
        assert edit.versions[0].skill_md == original
        assert edit.versions[1].skill_md == changed
        assert edit.versions[-1].skill_md == expected
        assert edit.selected_version_id == edit.saved_version_id
    else:
        assert response.json()["current_skill"]["skill_md"] == expected
    assert client.post(endpoint, json={}).status_code == 200
    assert backend_main.store.load_skill("demo-skill").skill_md == expected


def test_failed_lint_does_not_advance_version_or_create_snapshot(client, backend_main, grant_skill, monkeypatch) -> None:
    local = backend_main.store.store
    local.history_enabled = True
    original = '---\nname: demo-skill\ndescription: Demo\nmetadata:\n  version: "1.6"\n---\nOriginal'
    backend_main.store.save_skill(backend_main.SkillFiles(name="demo-skill", skill_md=original))
    grant_skill("demo-skill")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "demo-skill"}).json()["id"]
    changed = original.replace("Original", "Changed")
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": changed})
    calls = []

    def lint(name, files):
        calls.append(files["SKILL.md"])
        return backend_main.EaaLintResult(valid=len(calls) > 1, errors=["Rejected"] if len(calls) == 1 else [], warnings=[], ruleset_version="1.1")

    monkeypatch.setattr(backend_main, "lint_skill_package", lint)
    first = client.post(f"/api/sessions/{sid}/save", json={})
    assert first.status_code == 400
    assert not local.history
    assert backend_main.store.load_skill("demo-skill").skill_md == original
    assert backend_main.sessions[sid].current_skill.skill_md == changed
    second = client.post(f"/api/sessions/{sid}/save", json={})
    assert second.status_code == 200, second.text
    expected = changed.replace('"1.6"', '"1.6.1"')
    assert calls == [expected, expected]
    assert backend_main.store.load_skill("demo-skill").skill_md == expected
    assert len(local.history) == 1


def test_first_save_normalizes_declared_version(client, backend_main) -> None:
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    draft = '---\nname: demo-skill\ndescription: Demo\nmetadata:\n  version: "1.0"\n---\nOriginal'
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": draft})
    response = client.post(f"/api/sessions/{sid}/save", json={})
    assert response.status_code == 200, response.text
    assert response.json()["current_skill"]["skill_md"] == draft.replace('"1.0"', '"1.0.0"')


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


def test_list_skills_reports_the_skill_kind(client, backend_main, grant_skill) -> None:
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="demo-skill",
            skill_md="---\nname: demo-skill\ndescription: Demo\n---\n",
        )
    )
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="demo-scenario",
            skill_md=(
                "---\nname: demo-scenario\ndescription: Demo\nmetadata:\n"
                "  skill_type: scenario-orchestration\n  children:\n    - demo-skill\n---\n"
            ),
        )
    )
    grant_skill("demo-skill", description="Demo")
    grant_skill("demo-scenario", description="Demo")

    listed = client.get("/api/skills")

    assert listed.status_code == 200
    kinds = {item["name"]: item["skill_kind"] for item in listed.json()}
    assert kinds == {"demo-skill": "capability", "demo-scenario": "scenario"}


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
