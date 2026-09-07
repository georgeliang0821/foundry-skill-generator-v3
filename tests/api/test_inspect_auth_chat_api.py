from __future__ import annotations

from backend.models import Session


def test_inspect_endpoint_returns_tools_prompts_and_transitions(client) -> None:
    response = client.get("/api/inspect")

    body = response.json()
    assert response.status_code == 200
    assert body["tools"]
    assert body["prompts"]
    assert body["transitions"]
    assert [group["key"] for group in body["stage_groups"]] == [
        "PREPARE",
        "DRAFT",
        "REFINE",
        "TEST",
        "DONE",
    ]
    assert {prompt["filename"] for prompt in body["prompts"]} == {
        "00_global_system.md",
        "01_prepare.md",
        "01_prepare_scenario_addendum.md",
        "02_draft.md",
        "02_draft_scenario.md",
        "03_refine.md",
        "04_test.md",
        "04_test_scenario_addendum.md",
        "05_done.md",
        "09_best_practices.md",
        "10_format_spec.md",
        "10_format_spec_scenario.md",
        "11_output_rules.md",
    }
    assert all("<!-- Missing:" not in prompt["content"] for prompt in body["prompts"])
    assert {transition["from"] for transition in body["transitions"]} == {
        "prepare",
        "draft",
        "refine",
        "test",
        "done",
    }
    kinds = {item["kind"]: item for item in body["skill_kinds"]}
    assert set(kinds) == {"capability", "scenario"}
    assert "02_draft.md" in kinds["capability"]["prompts"]
    assert "02_draft_scenario.md" not in kinds["capability"]["prompts"]
    assert "02_draft_scenario.md" in kinds["scenario"]["prompts"]
    assert "10_format_spec_scenario.md" in kinds["scenario"]["prompts"]
    assert {rule["rule"] for rule in body["topology_rules"]} == {
        "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10",
        "P1", "P2", "P3", "P4", "P5", "P6",
        "C1", "C2", "C3",
    }

    rules = "\n".join(body["output_rules"]).lower()
    prompts = "\n".join(prompt["content"] for prompt in body["prompts"]).lower()
    combined = f"{rules}\n{prompts}"
    assert "name and description only (not tags)" in combined
    assert "positive samples miss" in combined
    assert "negative samples incorrectly select" in combined
    assert "one-question-at-a-time" in combined
    assert "exactly one thing per ask_user_input" in combined
    assert "prefer selectable options" in combined
    assert "strictly top-to-bottom in this order" in combined
    assert "skimmable structured record" in combined
    # Phase 3: three blocks; routing_uniqueness merges uniqueness + samples.
    assert "definition_clear" in combined
    assert "routing_uniqueness_confirmed" in combined
    assert "variables_ok" in combined
    assert "modify the existing skill" in combined
    assert "input_sources" in combined
    assert "record each field immediately" in combined
    assert "the more the better" in combined
    assert "route_to_peer" in combined
    # Neighbor single source + mutual-exclusion check.
    assert "neighbor_skills" in combined
    assert "mutual-exclusion check" in combined
    assert "routing samples in strict order" in combined
    # Variables: three kinds + needs-info contract.
    assert "kind='aca_env'" in combined
    assert "kind='obo_token'" in combined
    assert "kind='runtime'" in combined
    assert "in_aca" in combined
    assert "[needs_info]" in combined
    # Phase 3 SKILL.md sections + removed When to Use.
    assert "## overview" in combined
    assert "## when not to use" in combined
    assert "## required inputs" in combined
    assert "## environment variables" in combined
    assert "## obo token scopes" in combined


def test_read_session_hydrates_empty_current_skill_from_remote(client, backend_main) -> None:
    session = Session(current_stage="test", owner_upn="test@example.com")
    session.target_skill_id = "remote-skill"
    session.remote_skill_id = "remote-skill"
    session.current_skill.skill_md = ""
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="remote-skill",
            skill_md="---\nname: remote-skill\ndescription: Remote\n---\nbody",
        )
    )
    backend_main.sessions[session.id] = session

    response = client.get(f"/api/sessions/{session.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["current_skill"]["skill_md"].startswith("---\nname: remote-skill")
    assert backend_main.sessions[session.id].current_skill.skill_md

def test_chat_tolerates_non_dict_agent_events(client, backend_main) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    class WeirdAgent:
        def stream(self, session, message):
            yield "plain text event"
            yield {"event": "text_delta", "data": "more text"}

    backend_main.agent = WeirdAgent()

    response = client.post(f"/api/sessions/{session_id}/chat", json={"message": "go"})

    assert response.status_code == 200
    event_names = [evt["event"] for evt in response.json()["events"]]
    assert "error" not in event_names
    session = backend_main.sessions[session_id]
    assert "plain text eventmore text" in session.conversation[-1].content


def test_record_research_normalizes_string_items(backend_main) -> None:
    session = Session()

    backend_main.apply_tool_effect(
        session,
        "record_research",
        {
            "summary": "ok",
            "adjacent_skills": ["Azure Search skill"],
            "recommended_apis": ["Responses API"],
        },
    )

    assert session.prepare_brief.research.adjacent_skills == [{"name": "Azure Search skill"}]
    assert session.prepare_brief.research.recommended_apis == [{"name": "Responses API"}]

def test_auth_status_reports_unauthenticated(client) -> None:
    response = client.get("/api/auth/status")

    body = response.json()
    assert response.status_code == 200
    assert body["authenticated"] is False
    assert body["client_id"] == "test-client"


def test_auth_logout_clears_cookie(client, backend_main) -> None:
    backend_main.auth_tokens["auth-id"] = {"access_token": "token", "expires_at": 9999999999}
    client.cookies.set("sgv2_auth", "auth-id")

    response = client.post("/api/auth/logout", follow_redirects=False)

    assert response.status_code == 303
    assert "auth-id" not in backend_main.auth_tokens


def test_chat_sse_applies_passive_tool_effect_and_persists_assistant(client, backend_main, monkeypatch) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    class FakeAgent:
        def stream(self, session, message):
            yield {"event": "text_delta", "data": {"delta": "Moving to research."}}
            yield {
                "event": "tool_call",
                "data": {
                    "call_id": "call_stage",
                    "tool": "record_understanding",
                    "args": {
                        "user_goal": "demo goal",
                        "key_capabilities": ["a", "b"],
                        "differentiation": "unique",
                    },
                },
            }

    backend_main.agent = FakeAgent()

    response = client.post(f"/api/sessions/{session_id}/chat", json={"message": "Here is enough material"})
    assert response.status_code == 200
    payload = response.json()
    event_names = [evt["event"] for evt in payload["events"]]

    session = backend_main.sessions[session_id]
    assert "text_delta" in event_names
    assert "state_update" in event_names
    assert "done" in event_names
    # The handler accepts the legacy user_goal arg and stores it as skill_goal.
    assert session.prepare_brief.understanding.skill_goal == "demo goal"
    assert session.conversation[-1].role == "assistant"
    assert session.conversation[-1].content == "Moving to research."
    assert session.pending_tool_calls == []
