from __future__ import annotations

import json
import time

import pytest

from backend.models import Delegation, Mode, SkillKind
from backend.testing import (
    EXECUTE,
    ROUTE_ONLY,
    SCENARIO_DISCOVERABILITY_NOTE,
    ModeEchoError,
    _evaluate_apim_result,
    _post_apim_run,
    _test_request,
    extract_skill_used,
    run_selection_tests,
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("Answer\nSkill used: demo-skill\nFinal answer: ok", "demo-skill"),
        ('{"skill_used": "demo-skill"}', "demo-skill"),
        ('```json\n{"selected_skill": "demo-skill"}\n```', "demo-skill"),
        ("Skill used: none", None),
        ("Skill used: null", None),
        ("Skill used: n/a", None),
        ("No audit block", None),
    ],
)
def test_extract_skill_used(response: str, expected: str | None) -> None:
    assert extract_skill_used(response) == expected


def test_evaluate_apim_positive_result_passes_when_expected_skill_is_selected() -> None:
    result = _evaluate_apim_result(
        "Help me use demo",
        {"response_text": "Skill used: demo-skill", "duration_ms": 5},
        "demo-skill",
        "demo-skill",
        run_mode=EXECUTE,
    )

    assert result.passed is True
    assert result.actual_skill == "demo-skill"


def test_evaluate_apim_negative_result_passes_when_skill_is_not_selected() -> None:
    result = _evaluate_apim_result(
        "Tell me a joke",
        {"response_text": "Skill used: none", "duration_ms": 5},
        None,
        "demo-skill",
        run_mode=EXECUTE,
    )

    assert result.passed is True
    assert result.actual_skill is None


def test_evaluate_apim_error_result_is_indeterminate() -> None:
    result = _evaluate_apim_result(
        "Help me use demo",
        {"error": "missing token", "duration_ms": 0},
        "demo-skill",
        "demo-skill",
        run_mode=EXECUTE,
    )

    assert result.passed is None
    assert result.error == "missing token"


def test_run_selection_tests_calculates_hit_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runner(positive_samples, negative_samples, skill_name, delegated_token, run_mode=ROUTE_ONLY):
        positive = [
            _evaluate_apim_result(
                query,
                {"skills_referenced": [skill_name]},
                skill_name,
                skill_name,
                run_mode=run_mode,
            )
            for query in positive_samples
        ]
        negative = [
            _evaluate_apim_result(query, {"skills_referenced": []}, None, skill_name, run_mode=run_mode)
            for query in negative_samples
        ]
        return positive, negative

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        ["use demo", "run demo"],
        ["tell joke"],
        version_hash="v1",
        delegated_token="token",
    )

    assert run.skill_version_hash == "v1"
    assert run.positive_hit_rate == 1.0
    assert run.negative_correct_reject_rate == 1.0
    assert len(run.positive_results) == 2
    assert len(run.negative_results) == 1


_PREPARED_SKILL_MD = """---
name: demo-skill
description: Demo
---

## Environment Variables

- `API_HOST` (required): the host.

## API Reference

```python
import os


def main() -> None:
    print(os.environ["API_HOST"])
```
"""


def test_run_selection_tests_lints_the_code_the_runtime_prepared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prepared script is a different artifact from the sample code block."""
    prepared = 'import os\n\n\ndef main() -> None:\n    print(os.environ.get("API_HOST", ""))\n'

    def fake_runner(positive_samples, negative_samples, skill_name, delegated_token, run_mode=ROUTE_ONLY):
        positive = [
            _evaluate_apim_result(
                query,
                {"skills_referenced": [skill_name], "response_text": prepared},
                skill_name,
                skill_name,
                run_mode=run_mode,
            )
            for query in positive_samples
        ]
        return positive, []

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(_PREPARED_SKILL_MD, ["use demo"], [], version_hash="v1")

    result = run.positive_results[0]
    assert result.apim_response == prepared
    assert [issue["rule"] for issue in result.prepared_code_lint] == ["D1", "D1"]


def test_prepared_code_lint_is_empty_without_a_prepared_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_runner(positive_samples, negative_samples, skill_name, delegated_token, run_mode=ROUTE_ONLY):
        return (
            [
                _evaluate_apim_result(
                    query,
                    {"skills_referenced": [skill_name]},
                    skill_name,
                    skill_name,
                    run_mode=run_mode,
                )
                for query in positive_samples
            ],
            [],
        )

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(_PREPARED_SKILL_MD, ["use demo"], [], version_hash="v1")

    assert run.positive_results[0].prepared_code_lint == []


def test_test_request_appends_audit_instruction() -> None:
    request = _test_request("Help me use demo", mode=EXECUTE)

    assert request.startswith("Help me use demo")
    assert "Skill used:" in request


def test_route_only_sends_the_query_verbatim() -> None:
    # Nothing executes, so there is no run to narrate and no audit block to
    # parse; appending one would only change the phrasing we measure routing on.
    assert _test_request("Help me use demo", mode=ROUTE_ONLY) == "Help me use demo"


def test_post_apim_run_without_token_returns_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")

    output = _post_apim_run("Help me use demo", delegated_token=None, mode=ROUTE_ONLY)

    assert output["error"] == "Microsoft login token is missing. Sign in before running selection tests."
    assert output["request_sent"].startswith("Help me use demo")


def test_evaluate_uses_skills_referenced_when_prose_says_none() -> None:
    # Regression: APIM top-level skills_referenced wins over a "Skill used: none"
    # prose line, and frontmatter names match runtime ids via safe_skill_name.
    output = {
        "response_text": "[NEEDS_INFO]: missing env\nSkill used: none\nFinal answer: cannot run.",
        "skills_referenced": ["fabric-data-agent-skill"],
        "raw_response": {"skills_referenced": ["fabric-data-agent-skill"]},
        "duration_ms": 5,
    }
    result = _evaluate_apim_result(
        "query", output, "Fabric Data Agent Skill", "Fabric Data Agent Skill", run_mode=EXECUTE
    )
    assert result.passed is True
    assert result.actual_skill == "Fabric Data Agent Skill"
    assert result.skills_referenced == ["fabric-data-agent-skill"]


def test_evaluate_negative_passes_when_referenced_is_a_different_skill() -> None:
    output = {
        "response_text": "Skill used: none",
        "skills_referenced": ["fabric-data-agent-skill"],
        "raw_response": {},
        "duration_ms": 5,
    }
    result = _evaluate_apim_result("calendar query", output, None, "ms-graph-calendar", run_mode=EXECUTE)
    assert result.passed is True
    assert result.actual_skill == "fabric-data-agent-skill"


def test_evaluate_falls_back_to_prose_when_no_referenced() -> None:
    output = {"response_text": "Skill used: demo-skill", "skills_referenced": [], "duration_ms": 5}
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=EXECUTE)
    assert result.passed is True
    assert result.actual_skill == "demo-skill"


def test_route_only_never_falls_back_to_prose() -> None:
    # route_only sends no audit instruction, so any "Skill used:" line is the
    # model narrating -- treating it as routing evidence would turn a negative
    # sample that merely MENTIONS the skill into a false failure.
    output = {"response_text": "Skill used: demo-skill", "skills_referenced": [], "duration_ms": 5}

    positive = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=ROUTE_ONLY)
    assert positive.passed is False
    assert positive.actual_skill is None
    assert "not executed" in positive.reasoning

    negative = _evaluate_apim_result("q", output, None, "demo-skill", run_mode=ROUTE_ONLY)
    assert negative.passed is True


def test_extract_skills_referenced_reads_raw_response() -> None:
    from backend.testing import _extract_skills_referenced
    assert _extract_skills_referenced({"skills_referenced": ["a", "none", "b"]}) == ["a", "b"]
    assert _extract_skills_referenced({"skills_referenced": "single"}) == ["single"]
    assert _extract_skills_referenced({}) == []
    assert _extract_skills_referenced(None) == []


def test_evaluate_backfills_loaded_resources_from_the_raw_response() -> None:
    output = {
        "response_text": "script text",
        "skills_referenced": ["demo-skill"],
        "raw_response": {"loaded_resources": ["SKILL.md", " query.sql ", "none"]},
        "duration_ms": 5,
    }
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=ROUTE_ONLY)
    assert result.loaded_resources == ["SKILL.md", "query.sql"]


def test_loaded_resources_is_empty_when_the_runtime_omits_it() -> None:
    # The key name is not yet confirmed against a live runtime, so an absent or
    # unexpected shape must degrade to "no signal", never to an error.
    for raw in ({}, {"loaded_resources": None}, {"loaded_resources": 7}):
        result = _evaluate_apim_result(
            "q",
            {"response_text": "x", "skills_referenced": [], "raw_response": raw, "duration_ms": 1},
            "demo-skill",
            "demo-skill",
            run_mode=ROUTE_ONLY,
        )
        assert result.loaded_resources == []


def _scenario_skill_md() -> str:
    return (
        "---\n"
        "name: parent-skill\n"
        "description: Parent\n"
        "metadata:\n"
        "  skill_type: scenario-orchestration\n"
        "  children: [child-skill]\n"
        "---\n\n"
        "# Parent\n"
    )


def _child_resolver(name: str) -> str | None:
    if name == "child-skill":
        return "---\nname: child-skill\ndescription: Child\n---\n\n# Child\n"
    return None


def test_scenario_l1_failure_short_circuits_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr("backend.testing._post_apim_run", lambda *args, **kwargs: calls.append((args, kwargs)))

    run = run_selection_tests(
        "no frontmatter",
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        mode=Mode.NEW,
        expected_name="parent-skill",
    )

    assert [layer.layer for layer in run.scenario_layers] == ["L1"]
    assert run.scenario_layers[0].passed is False
    assert calls == []
    assert SCENARIO_DISCOVERABILITY_NOTE in run.notes


def test_scenario_l2_parent_presence_lists_both_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.testing._post_apim_run",
        lambda *args, **kwargs: {
            "response_text": "Skill used: parent-skill",
            "skills_referenced": ["parent-skill"],
        },
    )

    run = run_selection_tests(
        _scenario_skill_md(),
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        mode=Mode.NEW,
        expected_name="parent-skill",
        child_resolver=_child_resolver,
    )

    l2 = run.scenario_layers[1]
    assert l2.passed is False
    assert "SHAPE" in l2.diagnosis
    assert "ENVIRONMENT" in l2.diagnosis


def test_scenario_l3_unreached_child_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.testing._post_apim_run",
        lambda *args, **kwargs: {"response_text": "Skill used: other-skill", "skills_referenced": ["other-skill"]},
    )

    run = run_selection_tests(
        _scenario_skill_md(),
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        mode=Mode.NEW,
        expected_name="parent-skill",
        delegation=[Delegation(child_skill="child-skill", credentials_key="payload_json")],
        child_resolver=_child_resolver,
    )

    assert [layer.passed for layer in run.scenario_layers] == [True, True, False]
    assert "No declared child was reached" in run.scenario_layers[2].details[0]


def test_evaluate_stores_the_response_text_exactly_once() -> None:
    # Regression: the same text used to be stored in reasoning, apim_response and
    # apim_raw_response["response"], and the UI then rendered two of the three.
    body = "x" * 5000
    output = {
        "response_text": body,
        "skills_referenced": ["demo-skill"],
        "raw_response": {"response": body, "status": "completed"},
        "duration_ms": 5,
    }
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=EXECUTE)

    assert result.apim_response == body
    assert body not in result.reasoning
    assert "response" not in result.apim_raw_response
    assert result.apim_raw_response["status"] == "completed"
    assert "PASS" in result.reasoning


def test_evaluate_scrubs_credential_shapes_before_persisting() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefg"
    output = {
        "response_text": f"conn = connect(token={jwt})\nAccountKey=abc123def456;\nurl?sig=Xyz%2B99",
        "skills_referenced": ["demo-skill"],
        "raw_response": {"trace": f"Bearer {jwt}"},
        "duration_ms": 5,
    }
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=EXECUTE)

    assert jwt not in result.apim_response
    assert jwt not in str(result.apim_raw_response)
    assert "[REDACTED_JWT]" in result.apim_response
    assert "AccountKey=[REDACTED]" in result.apim_response
    assert "sig=[REDACTED]" in result.apim_response


def test_scrub_leaves_reviewable_content_alone() -> None:
    from backend.testing import _scrub_secrets

    script = (
        "import os\n"
        "raw = os.environ['hr_leave_json']\n"
        "IMAGE = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='\n"
    )

    assert _scrub_secrets(script) == script


def test_each_sample_gets_a_fresh_apim_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selection-test samples must never reuse an APIM session_id.

    Three separate reasons, any one of which is sufficient:
      1. the runtime restores execution_count / final_script_path from the prior
         turn's metadata, so a reused id can skip the gatekeeper guard;
      2. a reused id is how a needs_input round trip is continued, so the second
         sample would be read as an answer to the first;
      3. recent_full_outputs injects the previous turn's stdout into the prompt,
         which contaminates the routing signal we are trying to measure.
    """
    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")
    seen: list[str] = []

    class _FakeResponse:
        def read(self) -> bytes:
            return b'{"response": "ok", "mode": "route_only", "skills_referenced": []}'

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        seen.append(json.loads(request.data.decode("utf-8"))["session_id"])
        return _FakeResponse()

    monkeypatch.setattr("backend.testing.urlopen", fake_urlopen)

    _post_apim_run("first", "token", mode=ROUTE_ONLY)
    _post_apim_run("second", "token", mode=ROUTE_ONLY)

    assert len(set(seen)) == 2
    assert all(sid.startswith("skill-generator-v2-") for sid in seen)


def test_batch_runs_samples_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Concurrency here was self-inflicted: a fresh session_id per sample bypasses
    # the runtime's same-session guard, racing the per-turn skills provider.
    from backend.testing import _run_apim_tests_sequential_sync

    in_flight = 0
    max_in_flight = 0

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.01)
        in_flight -= 1
        return {"response_text": "Skill used: demo-skill", "skills_referenced": ["demo-skill"]}

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    positive, negative = _run_apim_tests_sequential_sync(
        ["a", "b", "c"], ["d", "e"], "demo-skill", "token"
    )

    assert max_in_flight == 1
    assert len(positive) == 3
    assert len(negative) == 2


# --- mode protocol -----------------------------------------------------------


def _stub_apim(monkeypatch: pytest.MonkeyPatch, body: dict) -> list[dict]:
    """Capture what we POST and reply with a caller-supplied body."""
    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")
    sent: list[dict] = []

    class _FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr("backend.testing.urlopen", fake_urlopen)
    return sent


def test_mode_is_a_top_level_body_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sibling of request/credentials, never smuggled into the prompt text.
    sent = _stub_apim(monkeypatch, {"response": "ok", "mode": "route_only", "skills_referenced": []})

    _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)

    assert sent[0]["mode"] == "route_only"
    assert sent[0]["request"] == "Help me use demo"
    assert "route_only" not in sent[0]["request"]


def test_execute_mode_still_carries_the_audit_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub_apim(monkeypatch, {"response": "ok", "mode": "execute", "skills_referenced": []})

    _post_apim_run("Help me use demo", "token", mode=EXECUTE)

    assert sent[0]["mode"] == "execute"
    assert "Skill used:" in sent[0]["request"]


def test_missing_mode_echo_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_apim(monkeypatch, {"response": "ok", "skills_referenced": ["demo-skill"]})

    with pytest.raises(ModeEchoError) as excinfo:
        _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)

    assert "did not echo" in str(excinfo.value)


def test_mismatched_mode_echo_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dangerous direction: we asked for route_only, the runtime executed.
    _stub_apim(monkeypatch, {"response": "ok", "mode": "execute", "skills_referenced": ["demo-skill"]})

    with pytest.raises(ModeEchoError) as excinfo:
        _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)

    assert "'execute'" in str(excinfo.value)
    assert "'route_only'" in str(excinfo.value)


def test_non_json_response_aborts_rather_than_assuming_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")

    class _FakeResponse:
        def read(self) -> bytes:
            return b"<html>gateway error</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr("backend.testing.urlopen", lambda *a, **k: _FakeResponse())

    with pytest.raises(ModeEchoError):
        _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)


def test_mode_echo_failure_aborts_the_whole_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No per-sample swallowing: one bad echo stops the run.

    An unconfirmed mode cannot be distinguished from a silent upgrade to
    execute, so continuing the batch would keep firing requests that may be
    really running the skill.
    """
    calls = {"n": 0}

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        raise ModeEchoError("no mode echo")

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    with pytest.raises(ModeEchoError):
        run_selection_tests(
            "---\nname: demo-skill\ndescription: Demo\n---\n",
            ["a", "b", "c"],
            ["d", "e"],
            delegated_token="token",
        )

    assert calls["n"] == 1


def test_selection_tests_default_to_route_only(monkeypatch: pytest.MonkeyPatch) -> None:
    modes: list[str] = []

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        modes.append(kwargs["mode"])
        return {"skills_referenced": ["demo-skill"]}

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    run_selection_tests(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        ["a"],
        ["b"],
        delegated_token="token",
    )

    assert modes == [ROUTE_ONLY, ROUTE_ONLY]


def test_scenario_layers_send_one_route_only_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # L1 sends nothing and L3 asserts against L2's response, so a scenario run
    # must never execute anything: the child does real writes.
    modes: list[str] = []

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        modes.append(kwargs["mode"])
        return {"response_text": "Skill used: child-skill", "skills_referenced": ["child-skill"]}

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    run = run_selection_tests(
        _scenario_skill_md(),
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        mode=Mode.NEW,
        expected_name="parent-skill",
        delegation=[Delegation(child_skill="child-skill", credentials_key="payload_json")],
        child_resolver=_child_resolver,
    )

    assert modes == [ROUTE_ONLY]
    assert [layer.passed for layer in run.scenario_layers] == [True, True, True]


def test_scenario_probe_names_the_scenario_so_the_runtime_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the name the runtime materializes every skill, so an incomplete
    # metadata.children list would pass L3 here and fail closed in production.
    scenarios: list[str] = []

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        scenarios.append(kwargs["scenario"])
        return {"response_text": "Skill used: child-skill", "skills_referenced": ["child-skill"]}

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    run_selection_tests(
        _scenario_skill_md(),
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        mode=Mode.NEW,
        expected_name="parent-skill",
        delegation=[Delegation(child_skill="child-skill", credentials_key="payload_json")],
        child_resolver=_child_resolver,
    )

    assert scenarios == ["parent-skill"]


def test_capability_probe_sends_no_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    # Capability skills are shared across scenarios; converging would misreport.
    scenarios: list[str] = []

    def fake_post(query, delegated_token, **kwargs):  # noqa: ANN001
        scenarios.append(kwargs.get("scenario", ""))
        return {"skills_referenced": ["demo-skill"]}

    monkeypatch.setattr("backend.testing._post_apim_run", fake_post)

    run_selection_tests(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        ["a"],
        ["b"],
        delegated_token="token",
    )

    assert scenarios == ["", ""]


def test_scenario_is_a_top_level_body_field(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub_apim(monkeypatch, {"response": "ok", "mode": "route_only", "skills_referenced": []})

    _post_apim_run("hello", "token", mode=ROUTE_ONLY, scenario="parent-skill")

    assert sent[0]["scenario"] == "parent-skill"


def test_batch_with_no_routing_at_all_is_reported_as_signal_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty candidate set makes every negative look like a perfect reject.

    create_provider_scope yields an EMPTY provider instead of raising when a
    token is missing or the SQL fetch fails, so the positives are the control:
    if not even they routed anywhere, the negatives establish nothing.
    """
    monkeypatch.setattr(
        "backend.testing._post_apim_run",
        lambda *a, **k: {"response_text": "I cannot help with that.", "skills_referenced": []},
    )

    run = run_selection_tests(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        ["use demo"],
        ["tell joke", "what is 2+2"],
        delegated_token="token",
    )

    assert run.negative_correct_reject_rate == 0.0
    assert all(r.passed is None for r in run.negative_results)
    assert any("ROUTING SIGNAL LOST" in note for note in run.notes)


def test_signal_loss_guard_stays_quiet_when_positives_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter([
        {"skills_referenced": ["demo-skill"]},
        {"skills_referenced": []},
    ])
    monkeypatch.setattr("backend.testing._post_apim_run", lambda *a, **k: next(outputs))

    run = run_selection_tests(
        "---\nname: demo-skill\ndescription: Demo\n---\n",
        ["use demo"],
        ["tell joke"],
        delegated_token="token",
    )

    assert run.negative_correct_reject_rate == 1.0
    assert run.notes == []


# --- fake runner (backend/e2e.py) -------------------------------------------
#
# The fake runner builds TestRun objects directly instead of speaking HTTP, so
# it calls the real verifier itself. Without that, the E2E suite would be the
# one place where a runtime that drops the echo still looks healthy.


def test_fake_runner_echoes_the_requested_mode() -> None:
    from backend.e2e import fake_run_selection_tests, set_scenario

    set_scenario("new_skill_happy_path")
    try:
        run = fake_run_selection_tests(
            "---\nname: demo-skill\ndescription: Demo\n---\n",
            ["use demo"],
            ["tell joke"],
        )
    finally:
        set_scenario("new_skill_happy_path")

    assert run.positive_results[0].apim_raw_response["mode"] == ROUTE_ONLY
    assert run.positive_results[0].skills_referenced == ["demo-skill"]
    assert "not executed" in run.positive_results[0].reasoning


def test_fake_runner_missing_echo_aborts_the_batch() -> None:
    from backend.e2e import fake_run_selection_tests, set_scenario

    set_scenario("mode_echo_missing")
    try:
        with pytest.raises(ModeEchoError):
            fake_run_selection_tests(
                "---\nname: demo-skill\ndescription: Demo\n---\n",
                ["use demo"],
                ["tell joke"],
            )
    finally:
        set_scenario("new_skill_happy_path")


def test_fake_scenario_runner_missing_echo_aborts_the_batch() -> None:
    from backend.e2e import fake_run_selection_tests, set_scenario

    set_scenario("mode_echo_missing")
    try:
        with pytest.raises(ModeEchoError):
            fake_run_selection_tests(
                _scenario_skill_md(),
                ["do the thing"],
                [],
                kind=SkillKind.SCENARIO,
                delegation=[Delegation(child_skill="child-skill")],
            )
    finally:
        set_scenario("new_skill_happy_path")


def test_fake_scenario_runner_never_executes() -> None:
    from backend.e2e import fake_run_selection_tests, set_scenario

    set_scenario("new_skill_happy_path")
    run = fake_run_selection_tests(
        _scenario_skill_md(),
        ["do the thing"],
        [],
        kind=SkillKind.SCENARIO,
        delegation=[Delegation(child_skill="child-skill")],
    )

    l2, l3 = run.scenario_layers[1], run.scenario_layers[2]
    assert l2.results[0].apim_raw_response["mode"] == ROUTE_ONLY
    assert l3.results[0].apim_raw_response["mode"] == ROUTE_ONLY
