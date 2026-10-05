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


def test_request_skill_saves(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": REQUEST_SKILL})
    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "request-skill"})
    assert response.status_code == 200


@pytest.mark.parametrize("code", ['    print("example")', ""])
def test_request_skill_cannot_save_without_binding_validation(client, code) -> None:
    skill_md = REQUEST_SKILL.split("## API Reference / Sample Code")[0]
    if code:
        skill_md += f"## API Reference / Sample Code\n```python\ndef main():\n{code}\n```\n"
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": skill_md})
    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "request-skill"})
    assert response.status_code == 400
    assert "A14" in response.json()["detail"]["message"]


def test_source_change_is_pending_and_invalidates_confirmation(client, backend_main) -> None:
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


def test_save_rejects_draft_that_changes_prepared_source(client) -> None:
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
    detail = response.json()["detail"]
    assert detail["kind"] == "skill_lint_failed"
    assert detail["recoverable"] is True
    assert detail["message"].startswith("Skill lint failed:")
    assert any(issue.startswith("A1:") for issue in detail["issues"])


def test_redraft_replaces_an_unsaved_draft_but_never_a_saved_skill(client, backend_main) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    session.current_stage = "draft"
    first = PendingToolCall(tool="propose_skill_draft", args={"skill_md": BROKEN_CODE_SKILL})
    second = PendingToolCall(tool="propose_skill_draft", args={"skill_md": RAISING_SKILL})
    session.pending_tool_calls = [first]
    backend_main.apply_tool_effect(session, "propose_skill_draft", first.args)
    session.pending_tool_calls.append(second)

    backend_main.apply_tool_effect(session, "propose_skill_draft", second.args)

    assert session.current_skill.skill_md == RAISING_SKILL
    assert [call.call_id for call in session.pending_tool_calls] == [second.call_id]
    session.remote_skill_id = "raising-skill"
    with pytest.raises(ValueError, match="saved skill"):
        backend_main.apply_tool_effect(session, "propose_skill_draft", first.args)


def test_save_is_not_blocked_by_advisory_lint_findings(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})

    response = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert response.status_code == 200


def _lint_result(backend_main, *, valid=True, errors=(), warnings=(), ruleset_version="1.0"):
    return backend_main.EaaLintResult(
        valid=valid, errors=list(errors), warnings=list(warnings), ruleset_version=ruleset_version
    )


def _save_raising_skill(client):
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(f"/api/sessions/{session_id}/draft", json={"skill_md": RAISING_SKILL})
    return session_id, client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})


def test_save_sends_the_skill_package_to_eaa_and_records_the_ruleset(client, backend_main, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(
        backend_main, "lint_skill_package",
        lambda name, files: seen.append((name, files)) or _lint_result(backend_main),
    )

    session_id, response = _save_raising_skill(client)

    assert response.status_code == 200
    assert seen == [("raising-skill", {"SKILL.md": RAISING_SKILL})]
    assert response.json()["eaa_ruleset_version"] == "1.0"
    assert backend_main.sessions[session_id].eaa_ruleset_version == "1.0"


def test_save_is_blocked_by_eaa_lint_errors(client, backend_main, monkeypatch) -> None:
    errors = ["reads OBO_CLIENT_SECRET, which is removed from the subprocess environment"]
    monkeypatch.setattr(backend_main, "lint_skill_package", lambda name, files: _lint_result(backend_main, valid=False, errors=errors))

    _, response = _save_raising_skill(client)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["kind"] == "eaa_lint_failed"
    assert detail["message"].startswith("EAA skill lint failed:")
    assert detail["issues"] == errors
    assert not backend_main.store.list_skills()


def test_save_is_blocked_when_eaa_rejects_the_package(client, backend_main, monkeypatch) -> None:
    def rejected(name, files):
        raise backend_main.EaaLintRejected("files_json must include SKILL.md")

    monkeypatch.setattr(backend_main, "lint_skill_package", rejected)

    _, response = _save_raising_skill(client)

    assert response.status_code == 400
    assert "files_json must include SKILL.md" in response.json()["detail"]
    assert not backend_main.store.list_skills()


def test_eaa_warnings_save_and_are_relayed_to_the_agent_once(client, backend_main, monkeypatch) -> None:
    warnings = [
        "metadata.mi_scopes: https://search.azure.com is not in MI_SCOPE_ALLOWLIST ['https://ai.azure.com']",
        "SKILL.md: installs packages at runtime",
    ]
    monkeypatch.setattr(backend_main, "lint_skill_package", lambda name, files: _lint_result(backend_main, warnings=warnings))
    session_id, first = _save_raising_skill(client)

    second = client.post(f"/api/sessions/{session_id}/save", json={"name": "raising-skill"})

    assert first.status_code == second.status_code == 200
    notes = [
        m for m in backend_main.sessions[session_id].conversation
        if m.role == MessageRole.SYSTEM and "lint_skill_package" in m.content
    ]
    assert len(notes) == 1
    assert "部署前需把 `https://search.azure.com` 加入 ACA 環境變數 `MI_SCOPE_ALLOWLIST`" in notes[0].content
    assert "- SKILL.md: installs packages at runtime" in notes[0].content


def test_save_fails_closed_when_the_eaa_lint_cannot_run(client, backend_main, monkeypatch) -> None:
    def unavailable(name, files):
        raise backend_main.EaaLintUnavailable("MCP_ENDPOINT is not set.")

    monkeypatch.setattr(backend_main, "lint_skill_package", unavailable)

    _, response = _save_raising_skill(client)

    assert response.status_code == 503
    assert "MCP_ENDPOINT" in response.json()["detail"]
    assert not backend_main.store.list_skills()


def test_e2e_fake_lint_skips_mcp(client, backend_main, monkeypatch) -> None:
    monkeypatch.setenv("SGV2_E2E_MODE", "1")
    monkeypatch.setenv("SGV2_E2E_FAKE_LINT", "1")
    monkeypatch.setattr(backend_main, "lint_skill_package", lambda name, files: pytest.fail("MCP must not be called"))

    _, response = _save_raising_skill(client)

    assert response.status_code == 200
    assert response.json()["eaa_ruleset_version"] == "e2e"


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
