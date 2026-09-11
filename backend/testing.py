"""Skill selection test helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .diagnostics import elapsed_ms, log_event, log_exception, now_ms
from .blob_store import parse_frontmatter, safe_skill_name
from .models import (
    Delegation,
    Mode,
    ScenarioLayerResult,
    SkillKind,
    TestResult,
    TestRun,
)
from .skill_lint import lint_skill
from .topology import has_errors, validate_topology

_TEST_INSTRUCTION = (
    "IMPORTANT (selection-test mode): Complete the user's request normally. "
    "At the end, return an explicit audit block so the test runner can verify routing.\n"
    "Use this exact format:\n"
    "Skill used: <skill-name-or-none>\n"
    "Final answer: <short summary of the answer you gave the user>\n"
    "If no skill was selected, write exactly: Skill used: none.\n"
    "測試模式：請正常回答使用者問題，但最後必須附上上述兩行，明確說明這次使用的 skill 和實際回覆摘要。"
)


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 20:
        return f"{token[:4]}...{token[-4:]} (len={len(token)})"
    return f"{token[:10]}...{token[-10:]} (len={len(token)})"


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _token_debug(token: str) -> dict[str, Any]:
    claims = _decode_jwt_claims(token)
    return {
        "aud": claims.get("aud"),
        "scp": claims.get("scp"),
        "roles": claims.get("roles"),
        "tid": claims.get("tid"),
        "appid": claims.get("appid") or claims.get("azp"),
        "exp": claims.get("exp"),
    }


def extract_skill_used(response_text: str) -> str | None:
    json_skill = _extract_skill_from_json_like_response(response_text)
    if json_skill is not None:
        return json_skill
    match = re.search(r"^skill used:\s*(.+?)\s*$", response_text or "", re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if value.lower() in {"none", "null", "n/a"}:
        return None
    return value


def _extract_skill_from_json_like_response(response_text: str | None) -> str | None:
    text = (response_text or "").strip()
    if not text:
        return None
    candidates = [text]
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL)
    if match:
        candidates.insert(0, match.group(1))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        value = data.get("skill_used") or data.get("selected_skill") or data.get("skill")
        if value is None:
            return None
        value = str(value).strip()
        return None if value.lower() in {"", "none", "null", "n/a"} else value
    return None


def _extract_name_list(raw_response: Any, key: str) -> list[str]:
    if not isinstance(raw_response, dict):
        return []
    value = raw_response.get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        name = str(item).strip()
        if name and name.lower() not in {"none", "null", "n/a"}:
            out.append(name)
    return out


def _extract_skills_referenced(raw_response: Any) -> list[str]:
    """Return the runtime's reported routed skills from the APIM top-level
    ``skills_referenced`` array. This is the authoritative routing signal and
    can disagree with the response prose's "Skill used:" line."""
    return _extract_name_list(raw_response, "skills_referenced")


def _extract_loaded_resources(raw_response: Any) -> list[str]:
    """Return the resources the runtime reported loading for this turn.

    Under route_only nothing executes, so this is the only way to tell whether
    the router actually loaded the skill's resources or merely named the skill.
    """
    # The key name has not yet been confirmed against a live runtime response;
    # an absent key yields [] rather than an error.
    return _extract_name_list(raw_response, "loaded_resources")


# High specificity on purpose. Entropy scanning would eat base64 images and the
# inline resource templates that make a response reviewable at all, and a false
# positive here is indistinguishable from the model writing the wrong value.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"(AccountKey=)[^;\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"([?&]sig=)[^&\s\"']+"), r"\1[REDACTED]"),
)


def _scrub_secrets(text: str) -> str:
    out = text or ""
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _scrub_deep(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_secrets(value)
    if isinstance(value, dict):
        return {k: _scrub_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_deep(item) for item in value]
    return value


# Top-level REST body field, sibling of `request` and `credentials` -- never
# prose smuggled into the request text. We always send it explicitly and never
# rely on the runtime's default.
ROUTE_ONLY = "route_only"
EXECUTE = "execute"


class ModeEchoError(RuntimeError):
    """The runtime did not confirm the execution mode we asked for.

    This aborts the entire batch on purpose, with no fallback and no opt-out
    flag. Selection tests only ever send route_only, so an unconfirmed mode
    means we cannot rule out that the skill really ran and really wrote.
    """


def verify_mode_echo(requested: str, echoed: Any) -> None:
    """Both directions: a missing echo is as fatal as a mismatched one."""
    if echoed is None:
        raise ModeEchoError(
            f"The runtime did not echo a top-level `mode` field (we sent mode={requested!r}). "
            "Selection tests are aborted: an unconfirmed mode is indistinguishable from a "
            "silent upgrade to execute, which would let a routing test write real data. "
            "If the runtime deployment predates the mode protocol, it must be upgraded before "
            "selection tests can run."
        )
    actual = str(echoed).strip()
    if actual != requested:
        raise ModeEchoError(
            f"The runtime reported mode={actual!r} but we sent mode={requested!r}. "
            "Selection tests are aborted: these results describe a different execution mode "
            "than the one under test."
        )


def _test_request(query: str, *, mode: str) -> str:
    # Under route_only nothing is executed, so there is no run to narrate and no
    # "Skill used:" line to parse. Sending the query verbatim also keeps the
    # routing measurement on the real user phrasing instead of on a query we
    # appended test scaffolding to.
    if mode == ROUTE_ONLY:
        return query
    return f"{query}\n\n{_TEST_INSTRUCTION}"


def _post_apim_run(
    query: str,
    delegated_token: str | None,
    *,
    mode: str,
    scenario: str = "",
    timeout: float = 120,
) -> dict[str, Any]:
    endpoint = os.getenv("SKILL_SELECTION_TEST_RUN_URL", "").strip()
    request_sent = _test_request(query, mode=mode)
    if not endpoint:
        log_event("apim.run.skipped", level="warning", reason="endpoint_not_configured")
        raise RuntimeError("SKILL_SELECTION_TEST_RUN_URL is not configured.")
    if not delegated_token:
        log_event("apim.run.skipped", level="warning", reason="missing_delegated_token")
        return {"request_sent": request_sent, "error": "Microsoft login token is missing. Sign in before running selection tests.", "duration_ms": 0}

    credentials: dict[str, str] = {
        "access_token": delegated_token,
        "authorization": f"Bearer {delegated_token}",
        "token_type": "Bearer",
    }
    payload = {
        "request": request_sent,
        "session_id": f"skill-generator-v2-{uuid4().hex}",
        "mode": mode,
        "scenario": scenario,
        "credentials": credentials,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {delegated_token}",
    }
    started = now_ms()
    log_event(
        "apim.run.start",
        endpoint=endpoint,
        mode=mode,
        scenario=scenario,
        has_delegated_token=bool(delegated_token),
        delegated_token=_mask_token(delegated_token),
        token_claims=_token_debug(delegated_token),
        body_credentials_present=bool(payload.get("credentials", {}).get("access_token")),
        request_chars=len(request_sent),
        request_preview=request_sent[:800],
    )

    try:
        request = Request(endpoint, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is configured/trusted APIM.
            response_body = response.read().decode("utf-8", errors="replace")
        duration_ms = elapsed_ms(started)
        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            data = {"response": response_body}
        text = str(data.get("response") or response_body)
        log_event(
            "apim.run.done",
            duration_ms=duration_ms,
            status=data.get("status", "unknown"),
            session_id=data.get("session_id", ""),
            mode_sent=mode,
            mode_echoed=data.get("mode"),
            skill_used=extract_skill_used(text) or "none",
            skills_referenced=_extract_skills_referenced(data),
            response_chars=len(text),
            response_preview=_scrub_secrets(text[:1200]),
            upload_count=len(data.get("uploads")) if isinstance(data.get("uploads"), list) else 0,
        )
        # After the done log so the failure is diagnosable, before the return so
        # no caller ever sees results from an unconfirmed mode.
        verify_mode_echo(mode, data.get("mode"))
        return {
            "request_sent": request_sent,
            "response_text": text,
            "raw_response": data,
            "status": str(data.get("status") or ""),
            "session_id": str(data.get("session_id") or ""),
            "uploads": data.get("uploads") if isinstance(data.get("uploads"), list) else [],
            "skills_referenced": _extract_skills_referenced(data),
            "duration_ms": duration_ms,
            "error": None,
        }
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        log_event(
            "apim.run.http_error",
            level="error",
            status_code=exc.code,
            reason=exc.reason,
            headers=response_headers,
            error_body=error_body[:2000],
            duration_ms=elapsed_ms(started),
        )
        return {
            "request_sent": request_sent,
            "error": f"APIM /run returned HTTP {exc.code}: {error_body}",
            "raw_response": {"status_code": exc.code, "reason": exc.reason, "headers": response_headers, "body": error_body},
            "duration_ms": elapsed_ms(started),
        }
    except URLError as exc:
        log_exception("apim.run.url_error", exc, endpoint=endpoint, duration_ms=elapsed_ms(started))
        return {"request_sent": request_sent, "error": str(exc), "duration_ms": elapsed_ms(started)}
    except (TimeoutError, socket.timeout):
        log_event("apim.run.timeout", level="error", endpoint=endpoint, timeout=timeout, duration_ms=elapsed_ms(started))
        return {
            "request_sent": request_sent,
            "error": f"APIM /run timed out after {timeout:.0f}s.",
            "duration_ms": elapsed_ms(started),
        }


def _evaluate_apim_result(
    query: str,
    output: dict[str, Any],
    expected_skill: str | None,
    skill_name: str,
    *,
    run_mode: str,
) -> TestResult:
    response_text = _scrub_secrets(str(output.get("response_text") or ""))
    error = output.get("error")
    referenced = output.get("skills_referenced")
    if not isinstance(referenced, list):
        referenced = _extract_skills_referenced(output.get("raw_response"))
    loaded_resources = _extract_loaded_resources(output.get("raw_response"))
    # Under route_only we never appended _TEST_INSTRUCTION, so there is no
    # "Skill used:" line to read. Parsing prose anyway would only let free-form
    # narration about a skill be mistaken for the runtime having routed to it.
    text_skill = None if run_mode == ROUTE_ONLY else extract_skill_used(response_text)
    # Decide whether THIS skill was routed to. The runtime's skills_referenced
    # array is authoritative; under execute we fall back to the prose line.
    # Compare on safe_skill_name so frontmatter names (e.g. "Fabric Data Agent
    # Skill") match runtime ids (e.g. "fabric-data-agent-skill").
    target_norm = safe_skill_name(skill_name)
    referenced_norm = {safe_skill_name(r) for r in referenced}
    selected = target_norm in referenced_norm or (
        bool(text_skill) and safe_skill_name(text_skill) == target_norm
    )
    if error:
        passed = None
    elif expected_skill:
        passed = selected
    else:
        passed = not selected
    if selected:
        actual = skill_name
    elif referenced:
        actual = referenced[0]
    else:
        actual = text_skill

    # apim_response is the single home for the full text. reasoning used to hold
    # a second copy and raw_response["response"] a third, which tripled session
    # storage and made the UI render hundreds of KB more than once.
    raw = output.get("raw_response")
    raw = _scrub_deep(dict(raw)) if isinstance(raw, dict) else {}
    raw.pop("response", None)

    if error:
        reasoning = f"Not established: {error}"
    else:
        expectation = f"expected {expected_skill}" if expected_skill else f"expected NOT {skill_name}"
        verdict = "PASS" if passed else "FAIL"
        reasoning = (
            f"{verdict} - {expectation}; routed to "
            f"{', '.join(referenced) or (text_skill or '(none)')}"
        )
        if run_mode == ROUTE_ONLY:
            reasoning += " (route_only: routing only, the skill was not executed)"

    return TestResult(
        query=query,
        expected_skill=expected_skill,
        actual_skill=actual,
        skills_referenced=[str(r) for r in referenced],
        loaded_resources=loaded_resources,
        passed=passed,
        reasoning=reasoning,
        apim_response=response_text,
        apim_status=str(output.get("status") or ""),
        apim_session_id=str(output.get("session_id") or ""),
        apim_uploads=[str(item) for item in output.get("uploads", [])],
        apim_raw_response=raw,
        request_sent=str(output.get("request_sent") or query),
        duration_ms=int(output.get("duration_ms") or 0),
        error=str(error) if error else None,
    )


async def _run_apim_tests_sequential(
    positive_samples: list[str],
    negative_samples: list[str],
    skill_name: str,
    delegated_token: str | None,
    run_mode: str = ROUTE_ONLY,
) -> tuple[list[TestResult], list[TestResult]]:
    """Send one sample at a time.

    Firing the batch concurrently was our own doing, and because every sample
    carries a fresh session_id it slipped past the runtime's only concurrency
    guard (which blocks reruns of the SAME session), racing the per-turn skills
    provider against itself.
    """
    jobs: list[tuple[str, str, str | None]] = []
    jobs.extend(("positive", query, skill_name) for query in positive_samples)
    jobs.extend(("negative", query, None) for query in negative_samples)

    log_event(
        "selection_test.batch.start",
        skill_name=skill_name,
        positive_samples=len(positive_samples),
        negative_samples=len(negative_samples),
        runner="APIM /run",
        sequential=True,
        mode=run_mode,
    )

    positive_results: list[TestResult] = []
    negative_results: list[TestResult] = []
    for kind, query, expected in jobs:
        output = await asyncio.to_thread(_post_apim_run, query, delegated_token, mode=run_mode)
        result = _evaluate_apim_result(query, output, expected, skill_name, run_mode=run_mode)
        if kind == "positive":
            positive_results.append(result)
        else:
            negative_results.append(result)
    return positive_results, negative_results


def _run_apim_tests_sequential_sync(
    positive_samples: list[str],
    negative_samples: list[str],
    skill_name: str,
    delegated_token: str | None,
    run_mode: str = ROUTE_ONLY,
) -> tuple[list[TestResult], list[TestResult]]:
    return asyncio.run(
        _run_apim_tests_sequential(positive_samples, negative_samples, skill_name, delegated_token, run_mode)
    )


_SIGNAL_LOSS_NOTE = (
    "ROUTING SIGNAL LOST -- this run proves nothing about routing. Every sample "
    "in the batch, including the positive controls, came back with an empty "
    "skills_referenced array, so the negatives were NOT scored as correct "
    "rejections. The runtime builds the per-turn candidate set with "
    "create_provider_scope, which yields an EMPTY provider rather than raising "
    "when a token is missing, the SQL fetch fails, or materialize fails. An "
    "empty candidate set makes every negative sample look like a perfect "
    "rejection and every positive look like a miss. Check the runtime's provider "
    "scope and the delegated token before reading anything into these results."
)


def _apply_signal_loss_guard(
    positive_results: list[TestResult],
    negative_results: list[TestResult],
) -> bool:
    """Void the negatives when the whole batch reported no routing at all.

    The positive samples are the control: if not even one of them routed
    anywhere, an empty array is far better explained by a broken candidate set
    than by the runtime correctly declining every negative.
    """
    scored = [r for r in positive_results + negative_results if r.error is None]
    positives_scored = [r for r in positive_results if r.error is None]
    if not positives_scored or not scored:
        return False
    if any(r.skills_referenced for r in scored):
        return False
    for result in negative_results:
        if result.error is None:
            result.passed = None
            result.reasoning = "Not established: routing signal lost across the whole batch."
    return True


SCENARIO_DISCOVERABILITY_NOTE = (
    "The scenario skill's own discoverability was NOT verified. The host agent "
    "chooses it by reading its name and description, and this runner drives the "
    "backend, not the host. Judge the description by review; a green run is not "
    "evidence that the host will select this skill."
)

_L2_DIAGNOSIS = (
    "The scenario skill appeared in skills_referenced, so it was not excluded "
    "from the backend's candidate set. There are exactly two possible causes and "
    "this run cannot tell them apart: (1) SHAPE -- the SAVED frontmatter does not "
    "satisfy T1-T4, so it is not recognised as a parent (recheck the saved copy, "
    "not the local draft); (2) ENVIRONMENT -- the test endpoint is running with "
    "the static skill provider, which applies no exclusion at all. If the shape "
    "is provably correct, it is the environment, and no edit to this skill can "
    "fix it."
)

_L3_DIAGNOSIS = (
    "The positive sample did not route to any declared child. The probe named "
    "this scenario, so the runtime loaded only the skills listed in "
    "metadata.children. There are three causes: (1) OMITTED -- the child is "
    "missing from metadata.children, so it was never loaded; this is fixable "
    "here by adding the entry, and it is the cause to rule out first because it "
    "fails closed and silently in production; (2) NOT DEPLOYED -- the child is "
    "absent from the test endpoint; (3) NAMING -- the child's own name and "
    "description do not match the sample. Causes 2 and 3 cannot be fixed by "
    "editing this skill. This layer does not verify the payload contract -- "
    "L1's content lint owns that."
)


def _scenario_lint(
    skill_content: str,
    delegation: list[Delegation],
    child_resolver: Callable[[str], str | None] | None,
) -> list:
    """Content lint for the scenario body. Never raises: it must not break a test run."""
    try:
        children: dict[str, str] = {}
        for d in delegation or []:
            child_md = child_resolver(d.child_skill) if (child_resolver and d.child_skill) else None
            if child_md:
                children[d.child_skill] = child_md
        return lint_skill(
            skill_content,
            SkillKind.SCENARIO,
            child_full_md=children,
            host_capabilities=[c for d in (delegation or []) for c in (d.host_capabilities or [])],
        )
    except Exception:  # noqa: BLE001 - a broken lint must never fail the run
        return []


def _lint_prepared_code(skill_content: str, results: list[TestResult]) -> None:
    """Lint the script the runtime WROTE for each sample, in place.

    The body prose -- not the sample code block -- is what the runtime reads, so
    the two artifacts drift and only one of them was ever linted. Run this once
    here rather than at prompt-build time: a later patch changes ``skill_content``
    but not the script an earlier run produced.
    """
    for result in results:
        if not (result.apim_response or "").strip():
            continue
        try:
            issues = lint_skill(
                skill_content, SkillKind.CAPABILITY, code_override=result.apim_response
            )
        except Exception:  # noqa: BLE001 - a broken lint must never fail the run
            continue
        result.prepared_code_lint = [issue.to_dict() for issue in issues]


def _run_scenario_tests(
    skill_content: str,
    positive_samples: list[str],
    *,
    kind_mode: Mode,
    expected_name: str,
    delegation: list[Delegation],
    child_resolver: Callable[[str], str | None] | None,
    delegated_token: str | None,
    run_mode: str = ROUTE_ONLY,
) -> list[ScenarioLayerResult]:
    """Run the L1/L2/L3 ladder. A layer only runs when the previous one passed."""
    skill_name = expected_name or (parse_frontmatter(skill_content)[0] or "skill")

    # --- L1: static topology, sends nothing ---------------------------------
    issues = validate_topology(
        skill_content,
        SkillKind.SCENARIO,
        mode=kind_mode,
        expected_name=expected_name or None,
        child_resolver=child_resolver,
    )
    l1 = ScenarioLayerResult(
        layer="L1",
        title="Static topology (T1-T10) and contract lint",
        passed=not has_errors(issues),
        details=[f"{i.rule} [{i.severity.value}] {i.message}" for i in issues] or ["All topology rules passed."],
    )
    if not l1.passed:
        l1.diagnosis = "The skill file itself is malformed. Fix it with propose_patch and rerun; no request was sent."
    else:
        # The payload contract is checked here and nowhere else: no layer below
        # executes the child, so nothing downstream can catch a stale contract.
        lint = _scenario_lint(skill_content, delegation, child_resolver)
        if lint:
            l1.details.extend(f"{i.rule} [{i.severity}] {i.message}" for i in lint)
            l1.diagnosis = (
                "Topology passes, but the body is out of step with the child. Fix it with "
                "propose_patch before relying on the layers below."
            )
    layers = [l1]
    if not l1.passed:
        return layers

    # --- L2: the parent must NOT be in the backend's candidate set ----------
    probe = next((q for q in positive_samples if q.strip()), "")
    l2 = ScenarioLayerResult(layer="L2", title="Parent absence from skills_referenced")
    if not probe:
        l2.details = ["Skipped: no positive sample to probe with."]
        layers.append(l2)
        return layers
    # Naming the scenario converges the runtime's pool to its declared children,
    # which is what production does and what makes an incomplete list observable.
    output = _post_apim_run(probe, delegated_token, mode=run_mode, scenario=skill_name)
    referenced = output.get("skills_referenced")
    if not isinstance(referenced, list):
        referenced = _extract_skills_referenced(output.get("raw_response"))
    result = _evaluate_apim_result(probe, output, None, skill_name, run_mode=run_mode)
    l2.results = [result]
    if output.get("error"):
        l2.passed = None
        l2.details = [f"Not established: {output['error']}"]
    else:
        parent_present = safe_skill_name(skill_name) in {safe_skill_name(r) for r in referenced}
        l2.passed = not parent_present
        l2.details = [f"skills_referenced: {', '.join(referenced) or '(none)'}"]
        if parent_present:
            l2.diagnosis = _L2_DIAGNOSIS
    layers.append(l2)
    if l2.passed is not True:
        return layers

    # --- L3: the sample must reach a declared child --------------------------
    # Asserted against L2's response. Executing the child would prove more, but
    # it would also really write, and no routing test may have side effects.
    l3 = ScenarioLayerResult(layer="L3", title="Child reachability")
    declared = [d.child_skill for d in delegation if d.child_skill]
    if not declared:
        l3.details = ["Skipped: no child skill is declared."]
        layers.append(l3)
        return layers
    routed = {safe_skill_name(r) for r in referenced}
    reached = [c for c in declared if safe_skill_name(c) in routed]
    l3.passed = bool(reached)
    if reached:
        l3.details = [f"Reached: {', '.join(f'`{c}`' for c in reached)}."]
        if len(reached) < len(declared):
            l3.details.append(
                "One sample routes to one child, so the other declared children are "
                "neither confirmed nor refuted by this run."
            )
    else:
        l3.details = [
            f"No declared child was reached. skills_referenced: {', '.join(referenced) or '(none)'}; "
            f"declared: {', '.join(declared)}."
        ]
        l3.diagnosis = _L3_DIAGNOSIS
    layers.append(l3)
    return layers


def run_selection_tests(
    skill_content: str,
    positive_samples: list[str],
    negative_samples: list[str],
    *,
    version_hash: str = "",
    delegated_token: str | None = None,
    kind: SkillKind = SkillKind.CAPABILITY,
    mode: Mode = Mode.NEW,
    expected_name: str = "",
    delegation: list[Delegation] | None = None,
    child_resolver: Callable[[str], str | None] | None = None,
    run_mode: str = ROUTE_ONLY,
) -> TestRun:
    if SkillKind(kind) is SkillKind.SCENARIO:
        layers = _run_scenario_tests(
            skill_content,
            positive_samples,
            kind_mode=mode,
            expected_name=expected_name,
            delegation=list(delegation or []),
            child_resolver=child_resolver,
            delegated_token=delegated_token,
            run_mode=run_mode,
        )
        run = TestRun(
            skill_version_hash=version_hash,
            scenario_layers=layers,
            notes=[SCENARIO_DISCOVERABILITY_NOTE],
        )
        log_event(
            "selection_test.scenario.done",
            skill_name=expected_name,
            layers={layer.layer: layer.passed for layer in layers},
        )
        return run

    skill_name, _ = parse_frontmatter(skill_content)
    skill_name = skill_name or "skill"
    positive_results: list[TestResult] = []
    negative_results: list[TestResult] = []

    started = now_ms()
    positive_results, negative_results = _run_apim_tests_sequential_sync(
        positive_samples,
        negative_samples,
        skill_name,
        delegated_token,
        run_mode,
    )
    log_event(
        "selection_test.batch.done",
        skill_name=skill_name,
        positive_results=len(positive_results),
        negative_results=len(negative_results),
        duration_ms=elapsed_ms(started),
    )

    signal_lost = _apply_signal_loss_guard(positive_results, negative_results)
    if signal_lost:
        log_event("selection_test.signal_lost", level="warning", skill_name=skill_name, mode=run_mode)

    pos_passed = [r for r in positive_results if r.passed is True]
    neg_passed = [r for r in negative_results if r.passed is True]
    _lint_prepared_code(skill_content, positive_results + negative_results)
    run = TestRun(
        skill_version_hash=version_hash,
        positive_results=positive_results,
        negative_results=negative_results,
        positive_hit_rate=(len(pos_passed) / len(positive_results)) if positive_results else 0.0,
        negative_correct_reject_rate=(len(neg_passed) / len(negative_results)) if negative_results else 0.0,
        notes=[_SIGNAL_LOSS_NOTE] if signal_lost else [],
    )
    log_event(
        "selection_test.run.done",
        skill_name=skill_name,
        mode=run_mode,
        positive_hit_rate=run.positive_hit_rate,
        negative_correct_reject_rate=run.negative_correct_reject_rate,
        positive_results=len(run.positive_results),
        negative_results=len(run.negative_results),
    )
    return run
