from __future__ import annotations

import json
import time

import pytest

from backend.models import Delegation, Mode, SkillKind
from backend.skill_lint import is_needs_info_response, needs_info_line
from backend.testing import (
    EXECUTE,
    ROUTE_ONLY,
    SCENARIO_DISCOVERABILITY_NOTE,
    ModeEchoError,
    RunAuthError,
    _evaluate_apim_result,
    _check_resource_reads,
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


def test_needs_info_response_is_not_linted_as_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime may ask for a missing input instead of preparing code; that is not an A1."""
    response = "\n  [NEEDS_INFO] missing=DRY_RUN\n請提供 DRY_RUN。"

    def fake_runner(positive_samples, negative_samples, skill_name, delegated_token, run_mode=ROUTE_ONLY):
        positive = [
            _evaluate_apim_result(
                query,
                {"skills_referenced": [skill_name], "response_text": response},
                skill_name,
                skill_name,
                run_mode=run_mode,
            )
            for query in positive_samples
        ]
        return positive, []

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(_PREPARED_SKILL_MD, ["use demo"], [], version_hash="v1")

    assert run.positive_results[0].passed is True
    assert run.positive_results[0].prepared_code_lint == []


def test_script_that_only_prints_needs_info_is_not_linted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script that asks for the input and exits has no business code to compare with the body."""
    response = (
        "import sys\n"
        "if hasattr(sys.stdout, 'reconfigure'):\n"
        "    sys.stdout.reconfigure(encoding='utf-8')\n"
        "print('[NEEDS_INFO] missing=DRY_RUN')\n"
        "print('Please provide DRY_RUN.')\n"
        "sys.exit(0)\n"
    )

    def fake_runner(positive_samples, negative_samples, skill_name, delegated_token, run_mode=ROUTE_ONLY):
        positive = [
            _evaluate_apim_result(
                query,
                {"skills_referenced": [skill_name], "response_text": response},
                skill_name,
                skill_name,
                run_mode=run_mode,
            )
            for query in positive_samples
        ]
        return positive, []

    monkeypatch.setattr("backend.testing._run_apim_tests_sequential_sync", fake_runner)

    run = run_selection_tests(_PREPARED_SKILL_MD, ["use demo"], [], version_hash="v1")

    assert run.positive_results[0].prepared_code_lint == []


_CONDITIONAL_NEEDS_INFO = (
    "import os, sys\n"
    "if not os.environ.get('X'):\n"
    "    print('[NEEDS_INFO] missing=X')\n"
    "    sys.exit(0)\n"
    "print(os.environ['X'])\n"
)
_WORK_AFTER_MARKER = "import requests\nprint('[NEEDS_INFO] missing=X')\nrequests.get('https://example.com')\n"
_WORK_BEFORE_MARKER = "import requests\nrequests.get('https://example.com')\nprint('[NEEDS_INFO] missing=X')\n"


@pytest.mark.parametrize("code", [_CONDITIONAL_NEEDS_INFO, _WORK_AFTER_MARKER, _WORK_BEFORE_MARKER])
def test_script_doing_real_work_is_not_a_needs_info_response(code: str) -> None:
    assert not is_needs_info_response(code)


def test_needs_info_line_reads_the_marker_from_a_script() -> None:
    code = "import sys\nprint(f'[NEEDS_INFO] missing=A,B')\nraise SystemExit(0)\n"
    assert needs_info_line(code) == "[NEEDS_INFO] missing=A,B"
    assert needs_info_line("  [NEEDS_INFO] missing=C\nmore") == "[NEEDS_INFO] missing=C"
    assert needs_info_line("print('hi')") == ""


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
        "raw_response": {
            "loaded_resources": [
                ["demo-skill", "assets/style.css"],
                ["demo-skill", " style.css "],
                ["demo-skill", "Template.html", "assets/template.html"],
                ["demo-skill", "assets/x.md", "ASSETS/X.md"],
                ["demo-skill", ""],
                ["demo-skill", 3],
                ["demo-skill", "a", "b", "c"],
            ],
            "failed_resources": [["demo-skill", "nope.css", "Error: not found"], ["demo-skill", "x"]],
        },
        "duration_ms": 5,
    }
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=ROUTE_ONLY)
    # Both spellings of the same file are kept: they record what the model actually asked for.
    assert result.loaded_resources == [
        "demo-skill: assets/style.css",
        "demo-skill: style.css",
        "demo-skill: Template.html -> assets/template.html",
        "demo-skill: assets/x.md",
    ]
    assert result.failed_resources == ["demo-skill: nope.css -- Error: not found", "demo-skill: x"]


def test_loaded_resources_still_accepts_plain_strings() -> None:
    output = {
        "response_text": "x",
        "skills_referenced": [],
        "raw_response": {"loaded_resources": ["SKILL.md", " query.sql ", "none"]},
        "duration_ms": 1,
    }
    result = _evaluate_apim_result("q", output, "demo-skill", "demo-skill", run_mode=ROUTE_ONLY)
    assert result.loaded_resources == ["SKILL.md", "query.sql"]


def test_loaded_resources_is_empty_when_the_runtime_omits_it() -> None:
    # An absent or unexpected shape must degrade to "no signal", never to an error.
    for raw in ({}, {"loaded_resources": None}, {"loaded_resources": 7}):
        result = _evaluate_apim_result(
            "q",
            {"response_text": "x", "skills_referenced": [], "raw_response": raw, "duration_ms": 1},
            "demo-skill",
            "demo-skill",
            run_mode=ROUTE_ONLY,
        )
        assert result.loaded_resources == []


_RESOURCES_MD = (
    "---\nname: demo-skill\ndescription: d\n---\n\n## Overview\n\nBody.\n\n## Skill Resources\n\n"
    "- `assets/style.css` (required, embed) -- styles.\n"
    "- `references/codes.md` (on-demand, reference) -- codes.\n"
    "- `assets/notes.md` -- unlabelled.\n"
)


def _routed(query: str, raw: dict, *, passed: bool | None = True, response: str = "print(1)"):
    from backend.models import TestResult

    return TestResult(query=query, passed=passed, apim_response=response, apim_raw_response=raw)


def _rules(findings: list[dict]) -> list[tuple[str, str, str]]:
    return [(f["rule"], f["severity"], f["detail"]) for f in findings]


def test_resource_check_reports_a_required_resource_that_was_not_read() -> None:
    results = [
        _routed("q1", {"loaded_resources": [["demo-skill", "style.css"]]}),
        _routed("q2", {"loaded_resources": [["other-skill", "assets/style.css", "assets/style.css"]]}),
        _routed("q3", {"loaded_resources": []}),
    ]

    findings = _check_resource_reads(_RESOURCES_MD, results, "demo-skill")

    # On-demand codes.md is never a finding; another skill's read does not count for this one.
    assert _rules(findings) == [("F4", "warning", "assets/notes.md"), ("F5", "warning", "assets/style.css")]
    assert "q2; q3" in findings[1]["message"] and "q1" not in findings[1]["message"]


def test_resource_check_compares_the_resolved_name_when_the_runtime_sends_it() -> None:
    read = [_routed("q", {"loaded_resources": [["demo-skill", "STYLE.css", "assets/style.css"]]})]
    wrong = [_routed("q", {"loaded_resources": [["demo-skill", "style.css", "references/style.css"]]})]

    assert [f["rule"] for f in _check_resource_reads(_RESOURCES_MD, read, "demo-skill")] == ["F4"]
    assert [f["rule"] for f in _check_resource_reads(_RESOURCES_MD, wrong, "demo-skill")] == ["F4", "F5"]


def test_resource_check_never_reads_an_absent_key_as_nothing_read() -> None:
    untracked = [_routed("q", {})]
    needs_info = [_routed("q", {"loaded_resources": []}, response="[NEEDS_INFO] missing=X")]
    elsewhere = [_routed("q", {"loaded_resources": []}, passed=False)]

    assert _rules(_check_resource_reads(_RESOURCES_MD, untracked, "demo-skill"))[1:] == [("F5", "info", "")]
    assert _rules(_check_resource_reads(_RESOURCES_MD, needs_info, "demo-skill"))[1:] == [("F5", "info", "")]
    assert _rules(_check_resource_reads(_RESOURCES_MD, elsewhere, "demo-skill"))[1:] == []


def test_resource_check_reports_failed_reads_once_per_name() -> None:
    raw = {
        "loaded_resources": [["demo-skill", "assets/style.css"]],
        "failed_resources": [["demo-skill", "style.cs", "Error: not found"], ["other-skill", "x", "Error"]],
    }

    findings = _check_resource_reads(_RESOURCES_MD, [_routed("q1", raw), _routed("q2", raw)], "demo-skill")

    assert _rules(findings) == [("F4", "warning", "assets/notes.md"), ("F6", "warning", "style.cs")]
    assert "Error: not found" in findings[1]["message"]


def test_resource_check_is_silent_without_a_skill_resources_section() -> None:
    assert _check_resource_reads("---\nname: demo-skill\n---\n\n## Overview\n", [_routed("q", {})], "demo-skill") == []


def test_latest_test_run_prints_resource_reads_only_when_there_are_findings() -> None:
    from backend.models import Session, TestRun
    from backend.state_machine import _format_latest_test_run

    session = Session(test_runs=[TestRun()])
    assert "### Resource reads" not in _format_latest_test_run(session)
    session.test_runs[0].resource_findings = [{"rule": "F5", "severity": "warning", "message": "m", "detail": "assets/a"}]
    assert "- [warning] F5 [assets/a]: m" in _format_latest_test_run(session)


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


def test_token_is_sent_only_in_the_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime exports every credentials entry into the skill's environment,
    so a token in the body would hand the user's raw token to the script."""
    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")
    token = "eyJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJ4In0.sig-part-xyz"
    captured: list = []

    class _FakeResponse:
        def read(self) -> bytes:
            return json.dumps({"response": "ok", "mode": "route_only"}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        captured.append(request)
        return _FakeResponse()

    monkeypatch.setattr("backend.testing.urlopen", fake_urlopen)

    _post_apim_run("Help me use demo", token, mode=ROUTE_ONLY)

    request = captured[0]
    assert request.get_header("Authorization") == f"Bearer {token}"
    raw_body = request.data.decode("utf-8")
    assert token not in raw_body
    assert "Bearer" not in raw_body
    assert json.loads(raw_body)["credentials"] == {}


def _stub_http_error(monkeypatch: pytest.MonkeyPatch, code: int, body: bytes) -> list[int]:
    import io
    from urllib.error import HTTPError

    monkeypatch.setenv("SKILL_SELECTION_TEST_RUN_URL", "https://runtime.example.test/run")
    calls: list[int] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls.append(1)
        raise HTTPError(request.full_url, code, "err", {}, io.BytesIO(body))

    monkeypatch.setattr("backend.testing.urlopen", fake_urlopen)
    return calls


def test_http_401_aborts_and_points_at_the_runtime_log(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_http_error(monkeypatch, 401, b'{"error": "invalid_token"}')

    with pytest.raises(RunAuthError) as excinfo:
        _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)

    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "[oauth] Token rejected:" in message
    assert "EAA administrator" in message


def test_http_401_aborts_the_whole_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_http_error(monkeypatch, 401, b"")

    with pytest.raises(RunAuthError):
        run_selection_tests(
            "---\nname: demo-skill\ndescription: Demo\n---\n",
            ["a", "b"],
            ["c"],
            delegated_token="token",
        )

    assert len(calls) == 1


def test_other_http_errors_stay_per_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_http_error(monkeypatch, 500, b"boom")

    output = _post_apim_run("Help me use demo", "token", mode=ROUTE_ONLY)

    assert output["error"].startswith("APIM /run returned HTTP 500")


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


def test_fake_runner_checks_requested_scripts_only_for_a_script_skill() -> None:
    from backend.e2e import fake_run_selection_tests, set_scenario

    set_scenario("new_skill_happy_path")
    script = (
        "import argparse\n"
        "parser = argparse.ArgumentParser(add_help=False)\n"
        "parser.add_argument('--room')\n"
    )
    skill_md = "---\nname: demo-skill\ndescription: Demo\n---\n"

    inline = fake_run_selection_tests(skill_md, ["use demo"], [])
    run = fake_run_selection_tests(skill_md, ["use demo", "use demo again"], ["tell joke"], script=script)

    assert inline.positive_results[0].requested_scripts == []
    first, second = (result.requested_scripts[0] for result in run.positive_results)
    assert (first["script"], first["args"], first["valid"]) == ("scripts/demo-skill.py", ["--room", "e2e"], True)
    assert (second["valid"], second["problems"]) == (False, ["`--e2e-undeclared` is not declared by the script"])
    assert run.negative_results[0].requested_scripts == []


@pytest.mark.parametrize(("scenario", "flags_on"), [("new_skill_happy_path", False), ("script_flags_on", True)])
def test_fake_aca_lookup_turns_the_script_flags_on_only_in_its_scenario(scenario: str, flags_on: bool) -> None:
    from backend.e2e import fake_aca_env_result, set_scenario
    from backend.eaa_platform import script_flags_off

    set_scenario(scenario)
    try:
        result = fake_aca_env_result()
    finally:
        set_scenario("new_skill_happy_path")

    assert (not script_flags_off(result)) is flags_on
