from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from .models import PendingToolCall, Session, UIHistoryEvent


DISPLAY_ARG_KEYS = {
    "question", "options", "prompt", "message", "summary", "reason",
    "change_summary", "target_file", "patch", "material_id", "skill_name",
    "new_name", "target", "item", "status", "confirmed", "evidence", "notify",
}
DISPLAY_RESULT_KEYS = {"action", "value", "answer", "selected", "custom", "positive", "name", "notes"}


def add_history(session: Session, kind: str, data: dict[str, Any], call_id: str = "") -> UIHistoryEvent:
    event = UIHistoryEvent(kind=kind, call_id=call_id, data=deepcopy(data))
    session.ui_history.append(event)
    return event


def record_tool(session: Session, call: PendingToolCall) -> None:
    if any(event.kind == "tool" and event.call_id == call.call_id for event in session.ui_history):
        return
    args = {key: value for key, value in call.args.items() if key in DISPLAY_ARG_KEYS and _display_value(value)}
    add_history(session, "tool", {"tool": call.tool, "args": args}, call.call_id)


def sync_history(session: Session) -> None:
    recorded = {event.data.get("index") for event in session.ui_history if event.kind == "message"}
    for index in range(session.ui_history_cursor, len(session.conversation)):
        message = session.conversation[index]
        if message.role in {"user", "assistant"} and index not in recorded:
            add_history(session, "message", {"index": index, "role": message.role})
        elif message.metadata.get("patch_error"):
            detail = message.metadata["patch_error"]
            call_id = message.metadata.get("call_id") or detail.get("call_id", "")
            if call_id and any(event.kind == "result" and event.call_id == call_id and event.data.get("failed") for event in session.ui_history):
                continue
            add_history(session, "status", {
                "text": f"Patch was not applied: {detail.get('guidance') or detail.get('error') or 'Invalid patch.'}",
                "failed": True,
            }, call_id)
        elif message.role == "tool" and not message.metadata.get("call_id"):
            try:
                result = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if not isinstance(result, dict):
                continue
            known = {key: value for key, value in result.items() if key in DISPLAY_RESULT_KEYS and _display_value(value)}
            if known:
                add_history(session, "status", {"text": f"Recorded {message.metadata.get('tool', 'tool')} result: {json.dumps(known, ensure_ascii=False)}"})
        elif message.metadata.get("patch_id") and message.content.startswith("Latest patch undone:"):
            add_history(session, "status", {"text": message.content})
    session.ui_history_cursor = len(session.conversation)
    for call in session.pending_tool_calls:
        record_tool(session, call)
    recorded_runs = {event.data.get("run_id") for event in session.ui_history if event.kind == "test"}
    for run in session.test_runs:
        if run.run_id not in recorded_runs:
            add_history(session, "test", {"run_id": run.run_id})


def record_result(
    session: Session, call: PendingToolCall, result: dict[str, Any],
    *, failed: bool = False, detail: Any = None, terminal: bool = True,
    patch_sync: dict[str, Any] | None = None,
) -> None:
    record_tool(session, call)
    payload = {key: value for key, value in result.items() if key in DISPLAY_RESULT_KEYS and _display_value(value)}
    data: dict[str, Any] = {"tool": call.tool, "result": payload, "failed": failed, "terminal": terminal}
    if detail is not None:
        data["detail"] = display_detail(detail)
    if patch_sync is not None:
        data["patch_sync"] = {"status": patch_sync["status"]}
        if patch_sync.get("detail"):
            reason = patch_sync["detail"]
            data["patch_sync"]["detail"] = display_detail(reason)
    add_history(session, "result", data, call.call_id)


def display_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("guidance") or detail.get("error") or "Operation failed.")
    return "Operation failed."


def _display_value(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float)) or (
        isinstance(value, list) and all(isinstance(item, (str, bool, int, float)) for item in value)
    )