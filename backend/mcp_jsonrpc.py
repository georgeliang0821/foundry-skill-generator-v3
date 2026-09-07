"""Minimal MCP client over Streamable HTTP (JSON-RPC 2.0 POST).

Ported from the standalone ``09_skill_generator`` Streamlit app so this FastAPI
backend can populate ``Session.aca_env_result`` during PREPARE. Uses urllib (no
third-party deps) and emits structured diagnostics via ``log_event``.

``MCP_ENDPOINT`` must be the MCP root (e.g. ``https://host/mcp``), not
``.../tools/...``.
"""

from __future__ import annotations

import itertools
import json
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from .diagnostics import elapsed_ms, log_event, log_exception, now_ms

MCP_JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_rpc_id_counter = itertools.count(1)
_rpc_id_lock = threading.Lock()


def _next_rpc_id() -> int:
    """Return the next monotonically-increasing JSON-RPC request ID."""
    with _rpc_id_lock:
        return next(_rpc_id_counter)


def _parse_mcp_body(raw: str) -> dict[str, Any]:
    """Parse an MCP JSON-RPC response body as either plain JSON or SSE.

    Streamable-HTTP MCP servers may answer a POST with ``text/event-stream``
    framing (``event: message`` / ``data: {...}``) instead of a bare JSON
    object. This helper accepts both and returns the JSON-RPC envelope.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("MCP response body was empty.")
    try:
        return json.loads(text)
    except ValueError:
        pass
    payload: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_part = line[len("data:"):].strip()
            if not data_part or data_part == "[DONE]":
                continue
            try:
                payload = json.loads(data_part)
            except ValueError:
                continue
    if payload is None:
        raise ValueError(f"MCP response was not JSON or parseable SSE: {text[:800]}")
    return payload


def mcp_post_jsonrpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 60,
) -> dict[str, Any]:
    """Send a single JSON-RPC 2.0 POST to the MCP endpoint and parse the reply."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": _next_rpc_id(), "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload).encode("utf-8")
    request = UrlRequest(url.rstrip("/"), data=body, headers=MCP_JSON_HEADERS, method="POST")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured MCP root.
        raw = response.read().decode("utf-8", errors="replace")
    return _parse_mcp_body(raw)


def jsonrpc_error_message(data: dict[str, Any]) -> str | None:
    """Extract the error message from a JSON-RPC response envelope, if present."""
    err = data.get("error")
    if not err:
        return None
    if isinstance(err, dict):
        return str(err.get("message", err))
    return str(err)


def _extract_tools_call_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Extract and JSON-parse the inner payload from a ``tools/call`` response.

    All MCP ``tools/call`` responses embed their actual payload as a JSON string
    inside ``result.content[0].text``.
    """
    return json.loads(data["result"]["content"][0]["text"])


def list_aca_environment_variables_jsonrpc(
    mcp_url: str,
    app_name: str,
    resource_group: str,
    subscription_id: str,
    *,
    timeout: float = 60,
) -> tuple[dict | None, str | None]:
    """Invoke the ``list_aca_environment_variables`` MCP tool.

    Returns ``(result_data, error)``. On success ``result_data`` contains
    ``revision``, ``variables``, and ``architectural_config`` (which may hold an
    ``OBO_SCOPE_REGISTRY`` map); ``error`` is ``None``. On failure
    ``result_data`` is ``None`` and ``error`` is a human-readable message.
    """
    started = now_ms()
    log_event("mcp.aca_env.start", endpoint=mcp_url, app_name=app_name, resource_group=resource_group)
    try:
        data = mcp_post_jsonrpc(
            mcp_url,
            "tools/call",
            {
                "name": "list_aca_environment_variables",
                "arguments": {
                    "app_name": app_name,
                    "resource_group": resource_group,
                    "subscription_id": subscription_id,
                },
            },
            timeout=timeout,
        )
    except (HTTPError, URLError) as exc:
        log_exception("mcp.aca_env.http_failed", exc, endpoint=mcp_url, duration_ms=elapsed_ms(started))
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        log_exception("mcp.aca_env.failed", exc, endpoint=mcp_url, duration_ms=elapsed_ms(started))
        return None, str(exc)

    err = jsonrpc_error_message(data)
    if err:
        log_event("mcp.aca_env.rpc_error", level="error", endpoint=mcp_url, error=err, duration_ms=elapsed_ms(started))
        return None, err

    try:
        payload = _extract_tools_call_payload(data)
        result = payload.get("data", payload) if isinstance(payload, dict) else payload
    except Exception as exc:  # noqa: BLE001
        log_exception("mcp.aca_env.parse_failed", exc, endpoint=mcp_url, duration_ms=elapsed_ms(started))
        return None, f"Failed to parse MCP response: {exc}"

    variables = result.get("variables", []) if isinstance(result, dict) else []
    obo = (result.get("architectural_config", {}) or {}).get("OBO_SCOPE_REGISTRY", {}) if isinstance(result, dict) else {}
    log_event(
        "mcp.aca_env.done",
        endpoint=mcp_url,
        revision=result.get("revision", "") if isinstance(result, dict) else "",
        variable_count=len(variables),
        obo_token_count=len(obo),
        duration_ms=elapsed_ms(started),
    )
    return result, None
