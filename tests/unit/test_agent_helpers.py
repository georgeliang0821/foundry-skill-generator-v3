from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agent import (
    FOUNDRY_OUTPUT_RULES,
    FOUNDRY_OUTPUT_SHAPE,
    TOOL_SCHEMAS,
    _escape_handlebars,
    _extract_json_candidate,
    _infer_ask_user_input,
    _load_output_rules,
    _normalize_tool_call,
    _parse_llm_payload,
    _session_context,
)
from backend.models import Material, MaterialKind, Session


@pytest.mark.parametrize("name,required", [
    ("select_neighbor_skill", ["skill_name"]),
    ("confirm_neighbor_notification", ["skill_name", "notify"]),
])
def test_neighbor_tools_are_registered_for_model_calls(name, required) -> None:
    matching = [schema for schema in TOOL_SCHEMAS if schema["name"] == name]
    assert len(matching) == 1
    parameters = matching[0]["parameters"]
    assert parameters["required"] == required
    assert set(parameters["properties"]) == set(required)
    assert parameters["additionalProperties"] is False


def test_output_rules_come_from_the_prompt_file() -> None:
    shape, rules = _load_output_rules()

    assert (shape, rules) == (FOUNDRY_OUTPUT_SHAPE, FOUNDRY_OUTPUT_RULES)
    assert "tool_calls" in shape
    assert rules[0].startswith("Return ONLY valid JSON")
    assert len(rules) > 1


def test_output_rules_forbid_named_handoffs_and_require_scope_only() -> None:
    _, rules = _load_output_rules()
    text = "\n".join(rules)
    assert "No named references to other skills anywhere in SKILL.md" in text
    assert "stopping conditions" in text
    assert "in-scope portion" in text
    assert "-> use `neighbor-skill`" not in text
    assert "backlink must name" not in text
    assert "-> use <new-skill-name>" not in text


def test_output_rules_use_the_existing_patch_card_for_acceptance() -> None:
    text = "\n".join(FOUNDRY_OUTPUT_RULES)
    assert "awaiting_card_acceptance" in text
    assert "Chat agreement or an `ask_user_input` answer is not a patch tool result" in text
    assert "unless an existing actionable tool card already provides that decision" in text
    assert "Do not emit a separate `ask_user_input` for the same acceptance" in text


def test_load_output_rules_raises_when_the_prompt_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.agent.load_prompt", lambda _name: "<!-- Missing prompt -->")

    with pytest.raises(RuntimeError, match="expected a ```json fence"):
        _load_output_rules()


def test_extract_json_candidate_from_text_wrapper() -> None:
    raw = 'Here is the response:\n{"text": "ok", "tool_calls": []}\nThanks'

    assert _extract_json_candidate(raw) == '{"text": "ok", "tool_calls": []}'


def test_escape_handlebars_neutralizes_braces_foundry_would_reject() -> None:
    # A child SKILL.md that embeds an HTML prototype doubles every CSS brace,
    # which Foundry's Handlebars pass over the system message rejects with
    # 400 agent_prompt_rendering_failed.
    raw = "<style>*{{box-sizing:border-box}}body{{margin:0}}</style>"

    escaped = _escape_handlebars(raw)

    assert "\\{{box-sizing" in escaped
    assert escaped.count("\\{{") == 2
    # Handlebars renders \{{ back to a literal {{, so the model sees the original.
    assert escaped.replace("\\{{", "{{") == raw


def test_escape_handlebars_leaves_clean_text_and_single_braces_alone() -> None:
    raw = "Path: Procurement/{case_id}/input/ and a closing }} brace."

    assert _escape_handlebars(raw) == raw


def test_escape_handlebars_is_idempotent() -> None:
    once = _escape_handlebars("body{{margin:0}}")

    assert _escape_handlebars(once) == once


def test_session_context_redacts_materials_that_the_system_message_carries() -> None:
    body = "    print('leave')\n" * 100
    content = f"def submit_leave():\n{body}"
    session = Session()
    session.materials.append(Material(id="m1", kind=MaterialKind.CODE, content=content))

    context = _session_context(session)

    # Only a short preview survives; the full text travels in the system message.
    assert body not in context
    assert "<redacted:" in context
    assert "## Materials" in context
    # The live session must be untouched: the session title and the Materials tab
    # both read the real content back out of it.
    assert session.materials[0].content == content


def test_normalize_openai_style_function_call() -> None:
    call = {
        "type": "function",
        "function": {
            "name": "stage_transition",
            "arguments": '{"target": "VERIFY", "summary": "ready"}',
        },
    }

    normalized = _normalize_tool_call(call)

    assert normalized == {
        "tool": "stage_transition",
        "args": {"target": "VERIFY", "summary": "ready"},
    }


def test_parse_llm_payload_accepts_tool_aliases() -> None:
    payload = _parse_llm_payload(
        '{"text": "ok", "actions": [{"name": "request_materials", "arguments": {"kind": "text"}}]}'
    )

    assert payload["text"] == "ok"
    assert payload["tool_calls"] == [{"tool": "request_materials", "args": {"kind": "text"}}]


def test_parse_llm_payload_repairs_plain_text_question() -> None:
    payload = _parse_llm_payload("Please choose the next step:\n1. Continue\n2. Stop")

    assert payload["text"] == ""
    assert payload["tool_calls"][0]["tool"] == "ask_user_input"
    assert payload["tool_calls"][0]["args"]["options"] == ["Continue", "Stop"]


def test_infer_ask_user_input_requires_options() -> None:
    assert _infer_ask_user_input("Please confirm the next step") is None



def test_is_malformed_json_payload_detects_bad_output() -> None:
    from backend.agent import _is_malformed_json_payload

    # Valid JSON object/array (incl. fenced) -> not malformed.
    assert _is_malformed_json_payload('{"text": "hi", "tool_calls": []}') is False
    assert _is_malformed_json_payload("```json\n{\"a\": 1}\n```") is False
    assert _is_malformed_json_payload("[]") is False
    # Malformed / truncated / non-object -> malformed (retry warranted).
    assert _is_malformed_json_payload("not json at all") is True
    assert _is_malformed_json_payload('{"text": "hi", "tool_calls": [{') is True
    assert _is_malformed_json_payload("42") is True
    assert _is_malformed_json_payload("") is True


@pytest.mark.parametrize("fail", [None, "client_init", "auth", "responses_create", "timeout"])
def test_foundry_timing_keeps_request_id_across_threads(monkeypatch, capsys, fail) -> None:
    from backend import agent
    from backend.diagnostics import log_context, log_event
    from openai import APITimeoutError
    import httpx

    monkeypatch.setenv("SGV2_DEBUG_LOG", "1")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/foundry")
    monkeypatch.setattr(agent, "build_system_prompt", lambda session: "system")
    monkeypatch.setattr(agent, "_build_foundry_user_prompt", lambda session, message: "query")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: SimpleNamespace(close=lambda: None))

    def token_provider():
        if fail == "auth":
            raise RuntimeError("SDK failed")
        return "not-a-real-secret"

    monkeypatch.setattr("azure.identity.get_bearer_token_provider", lambda *args: token_provider)
    client_options = {}

    def create_response(**kwargs):
        log_event("test.sdk")
        assert client_options["api_key"]() == "not-a-real-secret"
        if fail == "timeout":
            raise APITimeoutError(request=httpx.Request("POST", "https://example.test/responses"))
        if fail == "responses_create":
            raise RuntimeError("SDK failed")
        return SimpleNamespace(output_text="ok", id="resp-test", _request_id="foundry-test")

    fake_client = SimpleNamespace(responses=SimpleNamespace(create=create_response), close=lambda: None)

    def get_openai_client(**kwargs):
        client_options.update(kwargs)
        if fail == "client_init":
            raise RuntimeError("SDK failed")
        return fake_client

    fake_project = SimpleNamespace(get_openai_client=get_openai_client, close=lambda: None)
    monkeypatch.setattr("azure.ai.projects.AIProjectClient", lambda *args: fake_project)
    with log_context(request_id="chat-thread-test"):
        if fail:
            expected_error = APITimeoutError if fail == "timeout" else RuntimeError
            with pytest.raises(expected_error):
                agent._run_foundry_turn_sync(Session(), "go")
        else:
            assert agent._run_foundry_turn_sync(Session(), "go") == "ok"
    log_event("outside")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(record["request_id"] == "chat-thread-test" for record in records[:-1])
    assert "request_id" not in records[-1]
    assert any(record["event"] == "llm.prompt_build.done" for record in records)
    assert client_options["timeout"] == 120.0
    assert client_options["max_retries"] == 0
    assert "not-a-real-secret" not in json.dumps(records)
    if fail == "client_init":
        assert any(record["event"] == "llm.client_init.failed" for record in records)
        assert not any(record["event"] == "llm.responses_create.start" for record in records)
    else:
        assert any(record["event"] == "llm.client_init.done" for record in records)
        auth_event = "llm.auth.failed" if fail == "auth" else "llm.auth.done"
        assert any(record["event"] == auth_event for record in records)
        sdk_event = "llm.responses_create.failed" if fail else "llm.responses_create.done"
        assert next(record for record in records if record["event"] == sdk_event)["duration_ms"] >= 0
    if fail:
        assert any(record["event"] == "llm.call.failed" for record in records)
    if not fail:
        received = next(record for record in records if record["event"] == "llm.response.received")
        assert received["foundry_response_id"] == "resp-test"
        assert received["foundry_request_id"] == "foundry-test"