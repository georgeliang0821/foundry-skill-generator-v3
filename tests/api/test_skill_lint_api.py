from __future__ import annotations

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
