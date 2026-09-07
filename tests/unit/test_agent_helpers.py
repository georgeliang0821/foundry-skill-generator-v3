from __future__ import annotations

import pytest

from backend.agent import (
    FOUNDRY_OUTPUT_RULES,
    FOUNDRY_OUTPUT_SHAPE,
    _extract_json_candidate,
    _infer_ask_user_input,
    _load_output_rules,
    _normalize_tool_call,
    _parse_llm_payload,
    _session_context,
)
from backend.models import Material, MaterialKind, Session


def test_output_rules_come_from_the_prompt_file() -> None:
    shape, rules = _load_output_rules()

    assert (shape, rules) == (FOUNDRY_OUTPUT_SHAPE, FOUNDRY_OUTPUT_RULES)
    assert "tool_calls" in shape
    assert rules[0].startswith("Return ONLY valid JSON")
    assert len(rules) > 1


def test_load_output_rules_raises_when_the_prompt_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.agent.load_prompt", lambda _name: "<!-- Missing prompt -->")

    with pytest.raises(RuntimeError, match="expected a ```json fence"):
        _load_output_rules()


def test_extract_json_candidate_from_text_wrapper() -> None:
    raw = 'Here is the response:\n{"text": "ok", "tool_calls": []}\nThanks'

    assert _extract_json_candidate(raw) == '{"text": "ok", "tool_calls": []}'


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