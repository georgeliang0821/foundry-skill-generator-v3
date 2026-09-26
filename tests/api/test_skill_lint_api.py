from __future__ import annotations

import pytest

from backend.models import MessageRole, PendingToolCall


BROKEN_CODE_SKILL = """---
name: broken-skill
description: A skill whose sample code does not parse.
---

## Required Inputs

- `QUERY_JSON` (required): the request.

## API Reference / Sample Code

```python
import os


def main( -> None:
    print(os.environ["QUERY_JSON"])
```
"""

RAISING_SKILL = """---
name: raising-skill
description: A skill that misfiles a caller error as an exception.
---

## Required Inputs

- `QUERY_JSON` (required): the request.

## API Reference / Sample Code

```python
import os


def main() -> None:
    raw = os.environ.get("QUERY_JSON")
    if not raw:
        raise ValueError("no payload")
    print(raw)
```
"""


REQUEST_SKILL = '''---
name: request-skill
description: Process original report text.
---
## Required Inputs
```input-bindings
- name: description
  source: request
```
- `description`: required free text.
## `[NEEDS_INFO]` contract
DESCRIPTION: host asks for the original text and resends.
## API Reference / Sample Code
```python
def main():
    request_inputs = {"description": None}
    description = request_inputs.get("description")
    if description is None:
        print("[NEEDS_INFO] missing=DESCRIPTION")
        raise SystemExit(0)
    print(description)
```
'''


@pytest.mark.parametrize("enabled, expected", [(False, 400), (True, 200)])
def test_request_skill_save_requires_opt_in(client, monkeypatch, enabled, expected) -> None:
    monkeypatch.setenv("SGV2_ENABLE_REQUEST_INPUTS", str(enabled))
    assert client.get("/api/features").json() == {"request_inputs_enabled": enabled}
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": REQUEST_SKILL})
    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "request-skill"})
    assert response.status_code == expected
    if not enabled:
        assert "SGV2_ENABLE_REQUEST_INPUTS" in response.json()["detail"]


@pytest.mark.parametrize("code", ['    print("example")', ""])
def test_request_skill_cannot_save_without_binding_validation(client, monkeypatch, code) -> None:
    monkeypatch.setenv("SGV2_ENABLE_REQUEST_INPUTS", "true")
    skill_md = REQUEST_SKILL.split("## API Reference / Sample Code")[0]
    if code:
        skill_md += f"## API Reference / Sample Code\n```python\ndef main():\n{code}\n```\n"
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": skill_md})
    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "request-skill"})
    assert response.status_code == 400
    assert "A14" in response.json()["detail"]


def test_source_change_is_pending_and_invalidates_confirmation(client, backend_main, monkeypatch) -> None:
    monkeypatch.setenv("SGV2_ENABLE_REQUEST_INPUTS", "false")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    session.prepare_brief.verify_checklist["variables_ok"] = True
    session.prepare_brief.verify_evidence["variables_ok"] = "Previous confirmation"
    response = client.post(f"/api/sessions/{session_id}/variables", json={"variables": [
        {"name": "description", "source": "request"},
        {"name": "TOKEN", "kind": "obo_token"},
    ]})
    assert response.status_code == 200
    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["prepare_brief"]["variables"][0]["source"] == "request"
    assert body["prepare_brief"]["variables"][1]["kind"] == "obo_token"
    assert body["prepare_brief"]["variables"][1]["source"] == "credentials"
    assert not body["prepare_brief"]["verify_checklist"]["variables_ok"]
    assert "variables_ok" not in body["prepare_brief"]["verify_evidence"]
    with pytest.raises(ValueError, match="SGV2_ENABLE_REQUEST_INPUTS"):
        backend_main.apply_tool_effect(session, "update_prepare_checklist", {"item": "variables_ok", "confirmed": True})


def test_save_rejects_draft_that_changes_prepared_source(client, monkeypatch) -> None:
    monkeypatch.setenv("SGV2_ENABLE_REQUEST_INPUTS", "true")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.post(f"/api/sessions/{session_id}/variables", json={"variables": [{"name": "description"}]})
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": REQUEST_SKILL})
    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "request-skill"})
    assert response.status_code == 400
    assert "do not match" in response.json()["detail"]


def test_save_is_blocked_when_the_sample_code_does_not_parse(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": BROKEN_CODE_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "broken-skill"})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Skill lint failed:")
    assert "A1" in response.json()["detail"]


def test_save_is_not_blocked_by_advisory_lint_findings(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 200


def test_save_runs_the_eaa_lint_on_the_skill_folder(client, backend_main, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(backend_main, "run_eaa_skill_lint", lambda name, files: seen.append((name, files)) or [])
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 200
    assert seen == [("raising-skill", {"SKILL.md": RAISING_SKILL})]


def test_save_is_blocked_by_eaa_lint_errors_and_warnings(client, backend_main, monkeypatch) -> None:
    problems = ["ERROR reads OBO_CLIENT_SECRET", "WARN SKILL.md: credential=... placeholder"]
    monkeypatch.setattr(backend_main, "run_eaa_skill_lint", lambda name, files: problems)
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("EAA skill lint failed:")
    assert all(problem in detail for problem in problems)
    assert not backend_main.store.list_skills()


def test_save_fails_closed_when_the_eaa_lint_cannot_run(client, backend_main, monkeypatch) -> None:
    def unavailable(name, files):
        raise backend_main.EaaLintUnavailable("EAA_REPO_DIR is not set.")

    monkeypatch.setattr(backend_main, "run_eaa_skill_lint", unavailable)
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 503
    assert "EAA_REPO_DIR" in response.json()["detail"]
    assert not backend_main.store.list_skills()


@pytest.mark.parametrize(("variable", "message"), [
    ({"name": "PYTHON_TASK"}, "EAA discards"),
    ({"name": "GRAPH_ACCESS_TOKEN"}, "EAA discards"),
    ({"name": "OBO_CLIENT_SECRET", "kind": "aca_env"}, "platform secret"),
])
def test_save_rejects_variables_eaa_would_drop(client, variable, message) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.post(f"/api/sessions/{session_id}/variables", json={"variables": [variable]})
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_accepted_draft_records_lint_findings_as_a_system_message(client, backend_main) -> None:
    session = backend_main.Session()
    session.current_skill.skill_md = RAISING_SKILL
    session.pending_tool_calls.append(
        PendingToolCall(
            call_id="call_draft",
            tool="propose_skill_draft",
            args={"skill_md": RAISING_SKILL},
        )
    )
    backend_main.sessions[session.id] = session

    response = client.post(
        f"/api/sessions/{session.id}/tool-result",
        json={"tool_call_id": "call_draft", "result": {"action": "accept"}},
    )

    assert response.status_code == 200
    notes = [
        m
        for m in backend_main.sessions[session.id].conversation
        if m.role == MessageRole.SYSTEM and "skill_lint" in (m.metadata or {})
    ]
    assert len(notes) == 1
    assert "A3" in {issue["rule"] for issue in notes[0].metadata["skill_lint"]}
    assert "skill-lint finding" in notes[0].content


def test_topology_inspect_exposes_the_lint_findings(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.get(f"/api/sessions/{session_id}/topology")

    assert response.status_code == 200
    assert "A3" in {issue["rule"] for issue in response.json()["lint"]}
