"""Orchestrator adapter for Skill Generator v2."""

from __future__ import annotations

import json
import os
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from typing import Any

from .diagnostics import elapsed_ms, env_flag, log_event, log_exception, now_ms
from .material_fidelity import materials_prompt_chars
from .models import PendingToolCall, Session
from .state_machine import build_system_prompt, load_prompt


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "ask_user_input",
        "description": "Ask the user to choose from structured options.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question", "options"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_positive_samples",
        "description": "Ask the user to fill the POSITIVE routing samples directly in chat via an editable table card (queries that SHOULD route to this new skill). Use this in PREPARE step E.1 instead of a plain text message. The card writes the samples into the Prepare Brief and they appear in the right-hand Routing samples panel; when the user submits you receive their queries and continue with E.2 (validate + derive negatives). The card opens EMPTY -- do NOT prefill it; the user authors every positive sample themselves.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_materials",
        "description": "Request more materials from the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["kind", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_verify_checklist",
        "description": "Update one verify checklist item.",
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "status": {"type": "string"},
                "content": {},
            },
            "required": ["item", "status", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_skill_draft",
        "description": "Propose the complete SKILL.md artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_md": {"type": "string"},
            },
            "required": ["skill_md"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_patch",
        "description": "Propose a V4A patch.",
        "parameters": {
            "type": "object",
            "properties": {
                "target_file": {"type": "string"},
                "patch": {"type": "string"},
                "reason": {"type": "string"},
                "addresses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Which items of the '## Open Fix List' this patch closes. Copy each item "
                        "VERBATIM from that list (equivalently, from the latest record_reflection "
                        "what_to_change). Required whenever an Open Fix List is present, so the "
                        "backend can track what still remains."
                    ),
                },
            },
            "required": ["target_file", "patch", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rename_skill",
        "description": (
            "Rename this skill. The backend rewrites the frontmatter `name` itself and moves "
            "the Blob folder, the SQL row and every grant to the new name; the old name is "
            "deleted. Use this for ANY name change -- never propose_patch. REFINE/TEST only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "new_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["new_name", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_test_run",
        "description": "Request a skill-selection test run.",
        "parameters": {
            "type": "object",
            "properties": {
                "positive_samples": {"type": "array", "items": {"type": "string"}},
                "negative_samples": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["positive_samples", "negative_samples"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_test_results",
        "description": "Show test results.",
        "parameters": {"type": "object", "properties": {"run": {}}, "required": ["run"]},
    },
    {
        "name": "stage_transition",
        "description": "Declare stage transition.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["target", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_stage_transition",
        "description": "Request a stage transition (validated against the allow-list).",
        "parameters": {
            "type": "object",
            "properties": {
                "target_stage": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["target_stage", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_understanding",
        "description": "Persist the PREPARE-stage understanding brief.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_goal": {"type": "string"},
                "input_sources": {"type": "array", "items": {"type": "string"}},
                "key_capabilities": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "differentiation": {"type": "string"},
                "neighbor_skills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string"},
                            "axis": {"type": "string"},
                            "scenario": {"type": "string"},
                        },
                        "required": ["skill"],
                        "additionalProperties": False,
                    },
                },
                "open_questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_research",
        "description": "Persist the PREPARE-stage research brief.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "adjacent_skills": {"type": "array"},
                "pitfalls": {"type": "array", "items": {"type": "string"}},
                "recommended_apis": {"type": "array"},
                "open_questions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_reflection",
        "description": "Persist a TEST-stage reflection; auto-transitions back to REFINE.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_run_id": {"type": "string"},
                "what_went_wrong": {"type": "array", "items": {"type": "string"}},
                "what_to_change": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "One ATOMIC, independently patchable fix per entry -- never bundle several "
                        "fixes into one string. This array becomes the Open Fix List that drives the "
                        "REFINE round, and each entry must be closable by a single propose_patch."
                    ),
                },
                "confidence_delta": {"type": "number"},
                "raw": {"type": "string"},
            },
            "required": ["what_went_wrong", "what_to_change", "raw"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_prepare_checklist",
        "description": "Mark a PREPARE verify-checklist item as confirmed/unconfirmed and record the evidence that justifies the decision.",
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "confirmed": {"type": "boolean"},
                "evidence": {
                    "type": "string",
                    "description": "A DETAILED record (multi-line allowed, up to ~1200 chars) of why this checkpoint is satisfied: what was discussed, the user's own words or decision, the concrete facts, and for differentiation/adjacent/no-duplicate the specific Peer Skills compared. Required when confirmed=true.",
                },
            },
            "required": ["item", "confirmed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_test_samples",
        "description": "Persist the PREPARE routing test samples. Positive samples are user-authored queries that SHOULD route to this skill (you do not propose them). Negative samples are structured items you suggest based on differentiation: each is a similar query that should route to a peer skill instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "positive": {"type": "array", "items": {"type": "string"}},
                "negative": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "route_to_peer": {"type": "string"},
                            "why_not_this": {"type": "string"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_variables",
        "description": "Persist the skill's variables. FOUR kinds: kind='aca_env' is an ACA environment variable (set in_aca=true if it already exists in the fetched ACA env, false if it must be added; in_aca does NOT change the SKILL.md text; missing at runtime -> non-zero exit). kind='obo_token' is an OBO token variable (in_aca=true if already registered in OBO_SCOPE_REGISTRY; missing means the OBO chain is broken -> non-zero exit). kind='runtime' is a value that differs on every user query (resource id, file name, URL, ...), inferred from the query/positive samples or asked from the user, documented as a Required Input; when missing the script prints [NEEDS_INFO] missing=... and exits 0. kind='platform_identity' is the platform-injected verified actor string, whose ONLY legal name is EAA_VERIFIED_USER_UPN: declare it ONLY when a downstream interface demands the actor's UPN/email/alias as a STRING, never as a substitute for an OBO resource token the downstream can validate itself. Proactively PREFILL your best guesses from the materials/positive samples/query/ACA variables, then let the user confirm or edit; when the user describes a variable in chat, call this to update it.",
        "parameters": {
            "type": "object",
            "properties": {
                "variables": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string", "enum": ["aca_env", "obo_token", "runtime", "platform_identity"]},
                            "in_aca": {"type": "boolean", "description": "aca_env/obo_token only: true if it already exists (ACA env / OBO registry)."},
                            "description": {"type": "string", "description": "What the variable is and, for runtime, how to obtain it from the user."},
                            "example": {"type": "string", "description": "An example value (mainly for runtime). Never copied verbatim into SKILL.md."},
                            "required": {"type": "boolean"},
                        },
                        "required": ["name", "kind"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["variables"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_neighbor_edit",
        "description": "Propose an edit to an EXISTING neighbor skill (e.g. tightening its description or adding a 'When NOT to Use -> use <new skill>' backlink) so the new skill and the neighbor stay mutually exclusive. The neighbor's CURRENT SKILL.md (the exact version the edit is applied to) is provided in the system prompt under '## Neighbor Skills (full SKILL.md for editing)', between the <<<BEGIN ...>>> / <<<END ...>>> markers -- read it there, do NOT ask the user. Send a V4A `patch` against that current text: a single hunk with a few unchanged CONTEXT lines copied from the file plus your `-`/`+` edits (indentation is matched leniently, so you do not need to reproduce YAML indent perfectly). Keep the hunk SMALL and target a UNIQUE spot (e.g. the description lines, or the exact bullet under '## When NOT to Use This Skill'). Full rewrites are NOT allowed. Each accepted edit is appended as a NEW, uniquely-versioned entry (the original and every prior version are always kept, never overwritten); NOTHING is written to Blob until the user Saves. If the patch does not apply, copy the surrounding CONTEXT lines more exactly from the current text and retry.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The neighbor skill to edit (must be a skill the user can access)."},
                "patch": {"type": "string", "description": "A V4A patch against the neighbor's current SKILL.md: one hunk with a few unchanged CONTEXT lines (copied from the file to locate the spot) plus your `-`/`+` edits. Indentation is matched leniently. Full rewrites are not allowed."},
                "label": {"type": "string", "description": "A short label for this version, e.g. 'add backlink to <new skill>'."},
            },
            "required": ["skill_name", "patch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_delegation",
        "description": "SCENARIO SKILLS ONLY (rejected in a capability session). Persist the delegation_ok checkpoint. Two separate things are recorded. (1) `delegation`: for each child capability skill this scenario skill hands work to, record which child it is, the single `credentials` key the serialized payload goes under, which of the child's `##` sections the scenario will point at with fetch_skill, what the HOST must do itself because the child cannot, which of the child's operations this scenario drives, every round trip the child can demand before it completes, and one realistic sample payload. (2) `dependency_skills`: EVERY OTHER already-existing skill the scenario's flow uses at any step, even generic ones. `metadata.children` is the union of both and the host treats it as a whitelist -- a skill left out simply never becomes available, with no error message, so list it if in doubt. Read the credentials key, the supported operations and the failure messages from the child's full SKILL.md injected under '## Child Skills (full SKILL.md)'; record the section NAMES rather than copying the field table. A scenario skill has no variables of its own, so this REPLACES record_variables.",
        "parameters": {
            "type": "object",
            "properties": {
                "delegation": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "child_skill": {"type": "string", "description": "The existing capability skill that performs the work. It must already exist and be accessible; a scenario session never creates its child."},
                            "credentials_key": {"type": "string", "description": "The single key under `credentials` that the host puts the serialized payload under, e.g. hr_leave_json."},
                            "sections": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "The child's `##` section names this scenario will fetch, e.g. Required Inputs and the [NEEDS_INFO] contract section. Use the headings exactly as they appear in the child's body; the scenario points at them instead of restating the field table.",
                            },
                            "host_capabilities": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "What the host must perform itself because the child cannot, each with the reason why the child cannot do it.",
                            },
                            "operations": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "The child operations this scenario will drive, taken from the child's own SKILL.md. List every operation the child supports first, confirm each one with the user, then record only the ones that are in scope -- an operation the child supports and this scenario never drives is a capability the host can never reach through it. The ones left out belong in the drafted 'not applicable' section with their reason.",
                            },
                            "handshakes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Every round trip the child can demand before completing: the needs_input reason, what the host does about it, and whether the same session_id is reused.",
                            },
                        },
                        "required": ["child_skill"],
                        "additionalProperties": False,
                    },
                },
                "dependency_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every other existing skill the flow uses at any step and does not delegate a payload to, e.g. a deck renderer or a mail sender. These join metadata.children but are NOT marked as this scenario's internal children.",
                },
            },
            "required": ["delegation"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_child_edit",
        "description": "SCENARIO SKILLS ONLY (rejected in a capability session). Propose an edit to a DECLARED CHILD capability skill so the scenario's payload contract and the child's '## Required Inputs' stay consistent. Use it in the SAME turn as any patch that adds, renames or removes a delegated payload field, or changes the credentials key. The child's CURRENT SKILL.md (the exact version the edit is applied to) is provided in the system prompt under '## Child Skills (full SKILL.md)', between the <<<BEGIN ...>>> / <<<END ...>>> markers -- read it there, do NOT ask the user. Send a V4A `patch` against that current text: a single small hunk with a few unchanged CONTEXT lines copied verbatim plus your `-`/`+` edits (indentation is matched leniently). Full rewrites are NOT allowed. Each accepted edit is appended as a NEW uniquely-versioned entry (the original is always kept); NOTHING is written to Blob until the user Saves. If the patch does not apply, re-copy the surrounding CONTEXT lines more exactly and retry.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "The child skill to edit. It must be one of the declared metadata.children."},
                "patch": {"type": "string", "description": "A V4A patch against the child's current SKILL.md: one hunk with a few unchanged CONTEXT lines plus your `-`/`+` edits. Full rewrites are not allowed."},
                "label": {"type": "string", "description": "A short label for this version, e.g. 'accept new calendar_events field'."},
            },
            "required": ["skill_name", "patch"],
            "additionalProperties": False,
        },
    },
]


class OrchestratorAgent:
    """High-level agent wrapper.

    `stream()` yields UI events shaped like SSE data payloads.
    """

    def __init__(self) -> None:
        self.use_foundry = True
        missing = [name for name in ("FOUNDRY_PROJECT_ENDPOINT",) if not os.getenv(name, "").strip()]
        if missing:
            raise RuntimeError(f"Foundry agent configuration is missing: {', '.join(missing)}")
        log_event(
            "agent.ready",
            use_foundry=self.use_foundry,
            foundry_endpoint=env_flag("FOUNDRY_PROJECT_ENDPOINT"),
            foundry_agent_name=os.getenv("FOUNDRY_AGENT_NAME", "") or "skill-generator-agent",
            foundry_agent_version=os.getenv("FOUNDRY_AGENT_VERSION", "") or "2",
        )

    def stream(self, session: Session, user_message: str) -> Iterable[dict[str, Any]]:
        started = now_ms()
        log_event(
            "agent.stream.start",
            session_id=session.id,
            stage=session.current_stage,
            use_foundry=self.use_foundry,
            message_chars=len(user_message or ""),
        )
        try:
            yield {
                "event": "llm_status",
                "data": {
                    "status": "started",
                    "agent": (os.getenv("FOUNDRY_AGENT_NAME", "") or "skill-generator-agent") + ":" + (os.getenv("FOUNDRY_AGENT_VERSION", "") or "2"),
                    "stage": str(session.current_stage),
                },
            }
            yield from self._foundry_stream(session, user_message)
            yield {
                "event": "llm_status",
                "data": {
                    "status": "completed",
                    "agent": (os.getenv("FOUNDRY_AGENT_NAME", "") or "skill-generator-agent") + ":" + (os.getenv("FOUNDRY_AGENT_VERSION", "") or "2"),
                    "stage": str(session.current_stage),
                },
            }
        except Exception as exc:  # noqa: BLE001
            log_exception(
                "agent.foundry.failed",
                exc,
                session_id=session.id,
                stage=session.current_stage,
                duration_ms=elapsed_ms(started),
            )
            yield {
                "event": "llm_status",
                "data": {
                    "status": "failed",
                    "agent": (os.getenv("FOUNDRY_AGENT_NAME", "") or "skill-generator-agent") + ":" + (os.getenv("FOUNDRY_AGENT_VERSION", "") or "2"),
                    "stage": str(session.current_stage),
                    "error": str(exc),
                },
            }
            raise
        log_event("agent.stream.done", session_id=session.id, stage=session.current_stage, duration_ms=elapsed_ms(started))

    def _foundry_stream(self, session: Session, user_message: str) -> Iterable[dict[str, Any]]:
        raw = _run_foundry_turn_sync(session, user_message)
        # The model occasionally returns invalid JSON on the first try, which
        # breaks the chat answer card. Retry ONCE with the same request plus a
        # format-fixing instruction before falling back to best-effort parsing.
        if _is_malformed_json_payload(raw):
            log_event(
                "llm.payload.json_retry",
                session_id=session.id,
                stage=str(session.current_stage),
                raw_preview=(raw or "")[:600],
            )
            retry_message = f"{user_message}\n\n{_JSON_REPAIR_INSTRUCTION}"
            try:
                retry_raw = _run_foundry_turn_sync(session, retry_message)
            except Exception as exc:  # noqa: BLE001 -- retry is best-effort.
                log_exception("llm.payload.json_retry_failed", exc, session_id=session.id)
                retry_raw = ""
            if retry_raw and not _is_malformed_json_payload(retry_raw):
                log_event("llm.payload.json_retry_ok", session_id=session.id)
                raw = retry_raw
            elif retry_raw:
                # Still malformed; keep the retry output (the parser will infer).
                raw = retry_raw
        payload = _parse_llm_payload(raw)
        text = str(payload.get("text", "") or "").strip()
        if text:
            yield {"event": "text_delta", "data": {"delta": text}}
        for call in payload.get("tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            tool = str(call.get("tool", "")).strip()
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if tool:
                yield self._tool(session, tool, args)

    def _tool(self, session: Session, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        call = PendingToolCall(tool=tool, args=args)
        session.pending_tool_calls.append(call)
        return {"event": "tool_call", "data": call.model_dump()}


def tool_schemas_json() -> str:
    return json.dumps(TOOL_SCHEMAS, ensure_ascii=False)


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_candidate(raw: str) -> str:
    text = _strip_json_fence(raw)
    if not text:
        return text
    if text[0] in "[{":
        return text

    starts = [index for index in [text.find("{"), text.find("[")] if index >= 0]
    if not starts:
        return text
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text


def _json_loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _normalize_tool_call(call: Any) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None

    function_payload = call.get("function") if isinstance(call.get("function"), dict) else {}
    tool = (
        call.get("tool")
        or call.get("name")
        or call.get("type")
        or function_payload.get("name")
        or call.get("tool_name")
    )
    if tool == "function":
        tool = function_payload.get("name")
    tool = str(tool or "").strip()
    if not tool:
        return None

    args = (
        call.get("args")
        if "args" in call
        else call.get("arguments")
        if "arguments" in call
        else function_payload.get("arguments")
        if "arguments" in function_payload
        else call.get("input")
    )
    args = _json_loads_maybe(args)
    if not isinstance(args, dict):
        args = {"value": args}
    return {"tool": tool, "args": args}


def _extract_inline_options(text: str) -> list[str]:
    labels = list(re.finditer(r"(?:^|\s)(?:[A-Da-d][\)\.、]|[1-9][\)\.、])\s*", text))
    options: list[str] = []
    for index, label in enumerate(labels):
        start = label.end()
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        option = text[start:end].strip(" \n\r\t-:：;；,，")
        if 0 < len(option) <= 160:
            options.append(option)
    return options


def _extract_line_options(text: str) -> list[str]:
    options: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(?:[-*•]|\d+[\)\.、]|[A-Da-d][\)\.、])\s+(.{1,160})\s*$", line)
        if match:
            option = match.group(1).strip(" -:：;；,，")
            if option:
                options.append(option)
    return options


def _infer_ask_user_input(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return None
    question_markers = ["?", "？", "請選擇", "選擇", "是否", "確認", "accept", "confirm", "choose"]
    if not any(marker.lower() in cleaned.lower() for marker in question_markers):
        return None

    options = _extract_line_options(text) or _extract_inline_options(text)
    if len(options) < 2 and "/" in cleaned:
        tail = re.split(r"[:：]", cleaned, maxsplit=1)[-1]
        slash_options = [part.strip() for part in re.split(r"\s*/\s*", tail) if 0 < len(part.strip()) <= 80]
        if 2 <= len(slash_options) <= 5:
            options = slash_options
    if not (2 <= len(options) <= 8):
        return None

    question = text.strip()
    for option in options:
        question = question.replace(option, "")
    question = re.sub(r"(?:[-*•]|\d+[\)\.、]|[A-Da-d][\)\.、])\s*", "", question)
    question = re.sub(r"\s+", " ", question).strip(" :：,，;；")
    if len(question) > 500:
        question = question[:500].rstrip() + "..."
    return {"tool": "ask_user_input", "args": {"question": question or "請選擇下一步", "options": options}}


# Appended to the SAME user message on a single retry when the model''s first
# reply was not valid JSON (so the chat answer card could not be rendered).
_JSON_REPAIR_INSTRUCTION = (
    "IMPORTANT: your previous reply was NOT valid JSON and could not be parsed, "
    "so the UI could not render it. Reply AGAIN to the exact same request, this "
    "time returning ONLY a single valid JSON object in the required shape "
    '({"text": "...", "tool_calls": [...]}) -- all keys and strings double-quoted, '
    "all inner quotes/newlines properly escaped, no trailing commas, and no prose "
    "or markdown code fences outside the JSON. If you are asking the user to choose, "
    "use an ask_user_input tool call with a proper options array."
)


def _is_malformed_json_payload(raw: str) -> bool:
    """True when the model output cannot be parsed as a JSON object/array, i.e.
    the answer card would not render correctly and a format-fixing retry is
    warranted."""
    cleaned = _extract_json_candidate(raw)
    if not cleaned:
        return True
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return True
    return not isinstance(data, (dict, list))


def _parse_llm_payload(raw: str) -> dict[str, Any]:
    cleaned = _extract_json_candidate(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        inferred = _infer_ask_user_input(raw)
        if inferred:
            log_event("llm.payload.repaired_text_question", raw_preview=raw[:1200])
            return {"text": "", "tool_calls": [inferred]}
        return {"text": raw, "tool_calls": []}
    if isinstance(data, list):
        return {"text": "", "tool_calls": [call for call in (_normalize_tool_call(item) for item in data) if call]}
    if not isinstance(data, dict):
        return {"text": str(raw), "tool_calls": []}
    data.setdefault("text", "")
    raw_calls = data.get("tool_calls") or data.get("tools") or data.get("actions") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    data["tool_calls"] = [call for call in (_normalize_tool_call(item) for item in raw_calls) if call]
    if not data["tool_calls"]:
        inferred = _infer_ask_user_input(str(data.get("text") or ""))
        if inferred:
            log_event("llm.payload.repaired_json_text", text_preview=str(data.get("text") or "")[:1200])
            data["text"] = ""
            data["tool_calls"] = [inferred]
    return data


MATERIAL_PREVIEW_CHARS = 200


def _redact_materials(snapshot: dict[str, Any]) -> None:
    """Materials ship in full in the system message; the snapshot only points at them."""
    for item in snapshot.get("materials") or []:
        content = item.get("content") or ""
        item["content"] = (
            f"<redacted: {len(content)} chars -- the full text is in the system message "
            f"under '## Materials', in the <<<BEGIN MATERIAL {item.get('id', '')}>>> block>"
        )
        item["content_preview"] = content[:MATERIAL_PREVIEW_CHARS]


def _session_context(session: Session) -> str:
    snapshot = session.model_dump(mode="json")
    # Keep the prompt bounded while still giving the model the operational state.
    if snapshot.get("conversation"):
        snapshot["conversation"] = snapshot["conversation"][-12:]
    _redact_materials(snapshot)
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


OUTPUT_RULES_PROMPT = "11_output_rules.md"


def _load_output_rules() -> tuple[str, list[str]]:
    """Parse prompts/11_output_rules.md into (json shape, rules).

    The prompt file is the single source of truth; see its header for the
    parsing contract. Rules[0] is the output contract.
    """
    text = load_prompt(OUTPUT_RULES_PROMPT)
    shape = ""
    fence = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if fence:
        shape = fence.group(1)
    rules = [
        line[2:].strip()
        for line in text.splitlines()
        if line.startswith("- ") and line[2:].strip()
    ]
    # load_prompt() degrades to a comment instead of raising, which would silently
    # strip every behavioural rule from the turn. Fail at startup instead.
    if not shape or not rules:
        raise RuntimeError(
            f"prompts/{OUTPUT_RULES_PROMPT} yielded {len(rules)} rules and "
            f"{len(shape)} shape chars; expected a ```json fence and at least one '- ' bullet."
        )
    return shape, rules


FOUNDRY_OUTPUT_SHAPE, FOUNDRY_OUTPUT_RULES = _load_output_rules()


def _build_foundry_user_prompt(session: Session, user_message: str) -> str:
    rules_block = "\n".join(f"- {rule}" for rule in FOUNDRY_OUTPUT_RULES[1:])
    return f"""You are driving the Skill Generator v2 UI.

{FOUNDRY_OUTPUT_RULES[0]}

JSON shape:
{FOUNDRY_OUTPUT_SHAPE}

Available tools:
{tool_schemas_json()}

Rules:
{rules_block}

Current session:
{_session_context(session)}

Latest user message:
{user_message}
"""


async def _run_foundry_turn(session: Session, user_message: str) -> str:
    """Send a turn to the Foundry **agent** (agent_reference path).

    Instead of calling a raw model + locally-injected instructions, we now
    invoke the configured Foundry agent (default ``skill-generator-agent`` v2)
    via ``AIProjectClient.get_openai_client().responses.create``. Our
    ``build_system_prompt(session)`` content is delivered as a leading
    ``role="system"`` message so the agent still has the runtime context
    (stage, briefs, tools schema) even if its baked-in instructions are short.
    """
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    agent_name = (os.getenv("FOUNDRY_AGENT_NAME") or "skill-generator-agent").strip()
    agent_version = (os.getenv("FOUNDRY_AGENT_VERSION") or "2").strip()
    system_prompt = build_system_prompt(session)
    query = _build_foundry_user_prompt(session, user_message)

    started = now_ms()
    log_event(
        "llm.call.start",
        agent_name=agent_name,
        agent_version=agent_version,
        project_endpoint=project_endpoint,
        session_id=session.id,
        stage=session.current_stage,
        user_message_chars=len(user_message or ""),
        system_prompt_chars=len(system_prompt),
        query_chars=len(query),
        materials_count=len(session.materials or []),
        materials_total_chars=sum(len(m.content or "") for m in (session.materials or [])),
        materials_prompt_chars=materials_prompt_chars(session.materials),
        user_message_preview=(user_message or "")[:800],
    )

    def _invoke() -> str:
        project = AIProjectClient(project_endpoint, DefaultAzureCredential())
        openai_client = project.get_openai_client()
        resp = openai_client.responses.create(
            input=[
                {"type": "message", "role": "system", "content": system_prompt},
                {"type": "message", "role": "user", "content": query},
            ],
            extra_body={
                "agent_reference": {
                    "name": agent_name,
                    "version": agent_version,
                    "type": "agent_reference",
                }
            },
        )
        return (getattr(resp, "output_text", "") or "").strip()

    text = await asyncio.to_thread(_invoke)
    log_event(
        "llm.call.done",
        agent_name=agent_name,
        agent_version=agent_version,
        session_id=session.id,
        stage=session.current_stage,
        response_chars=len(text),
        duration_ms=elapsed_ms(started),
    )
    return text


def _run_foundry_turn_sync(session: Session, user_message: str) -> str:
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run_foundry_turn(session, user_message)).result()
