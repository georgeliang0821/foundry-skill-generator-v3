"""Local-only E2E test hooks for deterministic Playwright runs."""

from __future__ import annotations

import os
from typing import Any

from .blob_store import compute_hash, parse_frontmatter, safe_skill_name
from .models import (
    Delegation,
    PendingToolCall,
    ScenarioLayerResult,
    Session,
    SkillFiles,
    SkillKind,
    Stage,
    TestResult,
    TestRun,
)
from .testing import ROUTE_ONLY, SCENARIO_DISCOVERABILITY_NOTE, verify_mode_echo

_scenario = "new_skill_happy_path"


def e2e_enabled() -> bool:
    return os.getenv("SGV2_E2E_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def fake_agent_enabled() -> bool:
    return e2e_enabled() and os.getenv("SGV2_E2E_FAKE_AGENT", "").strip().lower() in {"1", "true", "yes", "on"}


def fake_test_runner_enabled() -> bool:
    return e2e_enabled() and os.getenv("SGV2_E2E_FAKE_TEST_RUNNER", "").strip().lower() in {"1", "true", "yes", "on"}


def set_scenario(name: str) -> str:
    global _scenario
    _scenario = (name or "new_skill_happy_path").strip()
    return _scenario


def get_scenario() -> str:
    return _scenario


def e2e_skill_files(name: str = "e2e-calendar-skill") -> SkillFiles:
    safe = safe_skill_name(name)
    skill_md = f"""---
name: {safe}
description: E2E fake skill for browser automation.
metadata:
  author: e2e
  version: "1.0"
  tags:
    - e2e
  uses_obo: false
---

## When to Use This Skill
Use when the user asks for the deterministic E2E calendar workflow.

## When NOT to Use This Skill
- Do not use for unrelated weather, jokes, or generic planning.

## Ground Rules
- Keep responses deterministic for Playwright.
- Surface local test errors clearly.

## API Reference / Key Patterns

### Sample Code

```python
def run():
    return "e2e result"
```
"""
    return SkillFiles(
        name=safe,
        skill_md=skill_md,
        version_hash=compute_hash(skill_md),
    )


def e2e_scenario_skill_files(name: str = "e2e-leave-workflow") -> SkillFiles:
    safe = safe_skill_name(name)
    skill_md = f"""---
name: {safe}
description: Orchestrates a deterministic E2E leave request through the HR child skill.
metadata:
  author: e2e
  version: "1.0"
  skill_type: scenario-orchestration
  children:
    - hr-leave-system
---

## Scenario Layer

The host reads the user's calendar, then delegates the HR operation to `hr-leave-system`.

## Request Flow

1. Read the requested date range from the host calendar capability.
2. Serialize the leave request under `credentials.hr_leave_json`.
3. Invoke `hr-leave-system` and preserve its session id for any follow-up handshake.

## Not Applicable

- Do not use this workflow for calendar-only questions or leave approvals.
"""
    return SkillFiles(
        name=safe,
        skill_md=skill_md,
        version_hash=compute_hash(skill_md),
    )


def _fake_apim_echo(run_mode: str) -> dict[str, Any]:
    """Stand in for the runtime's top-level `mode` echo.

    The fake runner never speaks HTTP, so it calls the real verifier itself --
    otherwise the E2E suite would be the one place where a runtime that drops
    the echo still looks healthy. The ``mode_echo_missing`` scenario omits the
    field so the abort path is exercised end to end.
    """
    if get_scenario() == "mode_echo_missing":
        return {}
    return {"mode": run_mode}


def fake_run_selection_tests(
    skill_content: str,
    positive_samples: list[str],
    negative_samples: list[str],
    *,
    version_hash: str = "",
    delegated_token: str | None = None,
    kind: SkillKind = SkillKind.CAPABILITY,
    delegation: list[Delegation] | None = None,
    run_mode: str = ROUTE_ONLY,
) -> TestRun:
    skill_name, _ = parse_frontmatter(skill_content)
    skill_name = skill_name or "e2e-calendar-skill"
    if kind is SkillKind.SCENARIO:
        child = next((item for item in delegation or [] if item.child_skill), Delegation())
        probe = next((item for item in positive_samples if item.strip()), "Run the E2E leave workflow")
        verify_mode_echo(run_mode, _fake_apim_echo(run_mode).get("mode"))
        l2_result = TestResult(
            query=probe,
            expected_skill=None,
            actual_skill=child.child_skill or None,
            skills_referenced=[child.child_skill] if child.child_skill else [],
            passed=True,
            reasoning="E2E fake parent-absence pass",
            apim_response=f"Skill used: {child.child_skill or 'none'}",
            apim_status="fake",
            apim_raw_response={"mode": run_mode},
            request_sent=probe,
            duration_ms=1,
        )
        # L3 asserts against L2's response and sends nothing of its own.
        l3_result = TestResult(
            query=probe,
            expected_skill=child.child_skill or None,
            actual_skill=child.child_skill or None,
            skills_referenced=[child.child_skill] if child.child_skill else [],
            passed=True,
            reasoning="E2E fake child-reachability pass",
            apim_response=f"Skill used: {child.child_skill or 'none'}",
            apim_status="fake",
            apim_raw_response={"mode": run_mode},
            request_sent=probe,
            duration_ms=1,
        )
        return TestRun(
            skill_version_hash=version_hash,
            scenario_layers=[
                ScenarioLayerResult(
                    layer="L1",
                    title="Static topology (T1-T10)",
                    passed=True,
                    details=["All topology rules passed."],
                ),
                ScenarioLayerResult(
                    layer="L2",
                    title="Parent absence from skills_referenced",
                    passed=True,
                    details=[f"skills_referenced: {child.child_skill or '(none)'}"],
                    results=[l2_result],
                ),
                ScenarioLayerResult(
                    layer="L3",
                    title="Child reachability",
                    passed=True,
                    details=[f"Reached: `{child.child_skill}`."],
                    results=[l3_result],
                ),
            ],
            notes=[SCENARIO_DISCOVERABILITY_NOTE],
        )
    scenario = get_scenario()
    fail_first_positive = scenario == "test_failure"
    verify_mode_echo(run_mode, _fake_apim_echo(run_mode).get("mode"))
    # route_only never appends the audit instruction and never parses prose, so
    # skills_referenced is the only signal -- keep the fake consistent with that.
    mode_note = " (route_only: routing only, the skill was not executed)" if run_mode == ROUTE_ONLY else ""
    positive_results: list[TestResult] = []
    for index, query in enumerate(positive_samples):
        passed = not (fail_first_positive and index == 0)
        positive_results.append(
            TestResult(
                query=query,
                expected_skill=skill_name,
                actual_skill=skill_name if passed else None,
                skills_referenced=[skill_name] if passed else [],
                passed=passed,
                reasoning=f"E2E fake positive {'pass' if passed else 'fail'}{mode_note}",
                apim_response=f"Skill used: {skill_name if passed else 'none'}",
                apim_status="fake",
                apim_raw_response={"mode": run_mode},
                request_sent=query,
                duration_ms=1,
            )
        )
    negative_results = [
        TestResult(
            query=query,
            expected_skill=None,
            actual_skill=None,
            passed=True,
            reasoning=f"E2E fake negative pass{mode_note}",
            apim_response="Skill used: none",
            apim_status="fake",
            apim_raw_response={"mode": run_mode},
            request_sent=query,
            duration_ms=1,
        )
        for query in negative_samples
    ]
    pos_passed = [result for result in positive_results if result.passed is True]
    neg_passed = [result for result in negative_results if result.passed is True]
    return TestRun(
        skill_version_hash=version_hash,
        positive_results=positive_results,
        negative_results=negative_results,
        positive_hit_rate=(len(pos_passed) / len(positive_results)) if positive_results else 0.0,
        negative_correct_reject_rate=(len(neg_passed) / len(negative_results)) if negative_results else 0.0,
    )


class FakeE2EAgent:
    """Deterministic agent replacement used only by Playwright E2E tests."""

    def stream(self, session: Session, user_message: str):
        scenario = get_scenario()
        stage = getattr(session.current_stage, "value", str(session.current_stage))
        if stage.startswith("Stage."):
            stage = stage.split(".", 1)[1]
        stage = stage.lower()
        message = user_message.lower()
        yield {"event": "llm_status", "data": {"status": "started", "model": "e2e-fake-model", "stage": stage}}

        has_tool_answers = any(getattr(item.role, "value", str(item.role)) in {"tool", "MessageRole.TOOL"} for item in session.conversation)
        if "user answered the ui confirmation questions" in message or (scenario.startswith("question") and has_tool_answers):
            yield {"event": "text_delta", "data": {"delta": "E2E fake agent accepted the UI answers."}}
        elif scenario.startswith("question"):
            yield from self._question_stream(session, scenario)
        elif scenario == "modify_existing":
            if stage in {Stage.REFINE.value, Stage.TEST.value}:
                yield from self._patch_stream(session, "Modify existing skill with deterministic E2E patch.")
            else:
                yield from self._stage_stream(session, stage)
        elif scenario in {"patch_refine", "patch_failure"} or "patch" in message or "boundary" in message or "change" in message:
            if scenario == "patch_failure":
                yield from self._patch_stream(session, "Missing anchor patch for E2E failure.", missing_anchor=True)
            else:
                yield from self._patch_stream(session, "Improve deterministic E2E selection boundary.")
        elif "test run has completed" in message and scenario == "test_failure":
            yield {"event": "text_delta", "data": {"delta": "E2E fake analysis found one failed positive sample."}}
            yield self._tool(
                session,
                "ask_user_input",
                {
                    "question": "Accept the E2E correction direction?",
                    "options": ["Accept correction", "Do not change"],
                },
            )
        elif "immediately request" in message or "patch was accepted" in message:
            yield {"event": "text_delta", "data": {"delta": "E2E fake agent is requesting a rerun."}}
            yield self._tool(
                session,
                "request_test_run",
                {
                    "positive_samples": ["Use the E2E calendar skill"],
                    "negative_samples": ["Tell me a joke"],
                },
            )
        else:
            yield from self._stage_stream(session, stage)

        yield {"event": "llm_status", "data": {"status": "completed", "model": "e2e-fake-model", "stage": stage}}

    def _stage_stream(self, session: Session, stage: str):
        # Keyed off Stage.*.value rather than string literals: the previous
        # version compared against retired uppercase stage names, fell through
        # to the no-op below every time, and never produced a draft.
        if stage == Stage.PREPARE.value:
            yield from self._prepare_stream(session)
            return
        if stage == Stage.DRAFT.value:
            yield from self._draft_stream(session)
            return
        yield {"event": "text_delta", "data": {"delta": f"E2E fake turn completed at {stage}."}}

    def _prepare_stream(self, session: Session):
        """Carry PREPARE through to a draft in one turn; the E2E helper sends a single chat."""
        yield {"event": "text_delta", "data": {"delta": "E2E fake agent completed the Prepare brief."}}
        yield self._tool(
            session,
            "record_understanding",
            {
                "skill_goal": "Create a deterministic E2E calendar skill.",
                "input_sources": ["E2E material for deterministic skill creation."],
                "key_capabilities": ["Run the deterministic E2E calendar workflow."],
                "differentiation": (
                    "Unlike generic-planner, this only serves the deterministic E2E calendar workflow."
                ),
                "out_of_scope": ["Weather forecasts", "Jokes"],
                "neighbor_skills": [
                    {
                        "skill": "generic-planner",
                        "axis": "scope",
                        "scenario": "Open-ended planning requests.",
                    }
                ],
            },
        )
        yield self._tool(
            session,
            "record_research",
            {
                "summary": "No adjacent skill covers the deterministic E2E calendar workflow.",
                "adjacent_skills": [{"name": "generic-planner"}],
                "pitfalls": ["Do not absorb open-ended planning requests."],
            },
        )
        if SkillKind(session.skill_kind) is SkillKind.SCENARIO:
            yield self._tool(
                session,
                "record_delegation",
                {
                    "delegation": [
                        {
                            "child_skill": "hr-leave-system",
                            "credentials_key": "hr_leave_json",
                            "host_capabilities": [
                                "Read the user's calendar because the HR child cannot access it."
                            ],
                            "operations": ["get_balance", "submit_request"],
                            "handshakes": [
                                "needs_input missing=LEAVE_CONFLICT_ACK -> ask the user and reuse the session_id"
                            ],
                        }
                    ]
                },
            )
        # Iterate the live checklist so a key rename cannot silently skip the gate.
        for item in session.prepare_brief.verify_checklist:
            yield self._tool(
                session,
                "update_prepare_checklist",
                {"item": item, "confirmed": True, "evidence": f"E2E deterministic confirmation for {item}."},
            )
        yield self._tool(
            session,
            "update_test_samples",
            {
                "positive": ["Use the E2E calendar skill", "Run the E2E deterministic workflow"],
                "negative": [
                    {
                        "query": "Tell me a joke",
                        "route_to_peer": "generic-planner",
                        "why_not_this": "Not calendar work.",
                    },
                    {
                        "query": "Explain the weather forecast",
                        "route_to_peer": "generic-planner",
                        "why_not_this": "Not calendar work.",
                    },
                ],
            },
        )
        yield self._tool(
            session,
            "stage_transition",
            {"target": Stage.DRAFT.value, "summary": "E2E prepare gate satisfied."},
        )
        if session.mode == "modify" and session.current_skill.skill_md.strip():
            yield self._tool(
                session,
                "stage_transition",
                {"target": Stage.REFINE.value, "summary": "E2E loaded draft accepted as the refinement baseline."},
            )
            return
        yield from self._draft_stream(session)

    def _draft_stream(self, session: Session):
        if session.current_skill.skill_md.strip():
            yield {"event": "text_delta", "data": {"delta": "E2E fake draft already exists."}}
            return
        files = e2e_scenario_skill_files() if SkillKind(session.skill_kind) is SkillKind.SCENARIO else e2e_skill_files()
        yield {"event": "text_delta", "data": {"delta": "E2E fake draft is ready."}}
        yield self._tool(session, "propose_skill_draft", {"skill_md": files.skill_md})

    def _question_stream(self, session: Session, scenario: str):
        yield {"event": "text_delta", "data": {"delta": "E2E fake agent needs confirmation."}}
        yield self._tool(
            session,
            "ask_user_input",
            {
                "question": "Which E2E direction should be used?",
                "options": ["Use deterministic draft", "Ask for more material"],
            },
        )
        if scenario in {"question_multi", "question_partial"}:
            yield self._tool(
                session,
                "ask_user_input",
                {
                    "question": "Which test sample set should be used?",
                    "options": ["Small sample set", "Large sample set"],
                },
            )

    def _patch_stream(self, session: Session, reason: str, *, missing_anchor: bool = False):
        patch = (
            """*** Begin Patch
*** Update File: SKILL.md
@@ missing anchor
-missing anchor
+patched text
*** End Patch"""
            if missing_anchor
            else """*** Begin Patch
*** Update File: SKILL.md
@@ ## Ground Rules
 ## Ground Rules
 - Keep responses deterministic for Playwright.
 - Surface local test errors clearly.
+- Include the E2E refined selection boundary.
*** End Patch"""
        )
        yield {"event": "text_delta", "data": {"delta": "E2E fake patch is ready."}}
        yield self._tool(session, "propose_patch", {"target_file": "SKILL.md", "patch": patch, "reason": reason})

    def _tool(self, session: Session, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        call = PendingToolCall(tool=tool, args=args)
        session.pending_tool_calls.append(call)
        return {"event": "tool_call", "data": call.model_dump()}
