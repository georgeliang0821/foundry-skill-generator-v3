"""Minimal MCP client over Streamable HTTP (JSON-RPC 2.0 POST).

Ported from the standalone ``09_skill_generator`` Streamlit app so this FastAPI
backend can populate ``Session.aca_env_result`` during PREPARE. Uses urllib (no
third-party deps) and emits structured diagnostics via ``log_event``.

``MCP_ENDPOINT`` must be the MCP root (e.g. ``https://host/mcp``), not
``.../tools/...``. When an audience is resolvable (see ``mcp_audience``) every
call carries an app-only bearer token; otherwise it is sent anonymously.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from .diagnostics import elapsed_ms, log_event, log_exception, now_ms

MCP_JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_rpc_id_counter = itertools.count(1)
_rpc_id_lock = threading.Lock()

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"audience": "", "value": "", "expires_at": 0.0}


def _next_rpc_id() -> int:
    """Return the next monotonically-increasing JSON-RPC request ID."""
    with _rpc_id_lock:
        return next(_rpc_id_counter)


def _http_error_detail(exc: HTTPError) -> str:
    """Describe an ``HTTPError`` including the parts ``str(exc)`` throws away.

    ``str(HTTPError)`` is just ``"HTTP Error 401: Unauthorized"``; the response
    body and the ``WWW-Authenticate`` challenge are where the actual reason is.
    """
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        body = ""
    parts = [f"HTTP {exc.code} {exc.reason}"]
    challenge = exc.headers.get("WWW-Authenticate", "") if exc.headers else ""
    if challenge:
        parts.append(f"WWW-Authenticate: {challenge}")
    if body:
        parts.append(body[:800])
    return " | ".join(parts)


def mcp_audience() -> str:
    """Return the resource the MCP server validates the token ``aud`` against.

    Empty means the endpoint is treated as anonymous and no token is attached.
    The MCP server's audience is the same Entra app that signs users in, so it
    is derived from ``MICROSOFT_OBO_SCOPE`` unless overridden.
    """
    override = os.getenv("MCP_OAUTH_AUDIENCE", "").strip().rstrip("/")
    if override:
        return override
    scope = os.getenv("MICROSOFT_OBO_SCOPE", "").strip()
    if not scope:
        return ""
    # api://<app-id>/user_impersonation -> api://<app-id>
    return scope.rsplit("/", 1)[0] if "/" in scope.split("://", 1)[-1] else scope


def acquire_mcp_token(audience: str, *, timeout: float = 30) -> str:
    """Fetch (and cache) an app-only token for the MCP resource.

    App-only rather than delegated because the PREPARE-entry call runs on a
    background thread with no user context. The Entra app that signs users in is
    also the MCP resource, so this is a client-credentials grant against itself:
    the token carries neither ``scp`` nor ``roles``, which the MCP server's
    token validation does not require.
    """
    now = time.time()
    with _token_lock:
        if _token_cache["audience"] == audience and float(_token_cache["expires_at"]) > now:
            return str(_token_cache["value"])

    tenant = os.getenv("MICROSOFT_TENANT_ID", "").strip()
    client_id = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
    client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
    if not (tenant and client_id and client_secret):
        raise RuntimeError(
            "The MCP endpoint requires OAuth but MICROSOFT_TENANT_ID / MICROSOFT_CLIENT_ID / "
            "MICROSOFT_CLIENT_SECRET are not all set."
        )

    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{audience}/.default",
        }
    ).encode("utf-8")
    request = UrlRequest(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - Entra token endpoint.
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Entra returned no access_token for the MCP resource.")
    try:
        expires_in = float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600.0
    with _token_lock:
        _token_cache.update(audience=audience, value=token, expires_at=now + expires_in - 60)
    log_event("mcp.token.acquired", audience=audience, expires_in=expires_in)
    return token


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
    token: str = "",
    timeout: float = 60,
) -> dict[str, Any]:
    """Send a single JSON-RPC 2.0 POST to the MCP endpoint and parse the reply."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": _next_rpc_id(), "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload).encode("utf-8")
    headers = dict(MCP_JSON_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = UrlRequest(url.rstrip("/"), data=body, headers=headers, method="POST")
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
    audience = mcp_audience()
    log_event(
        "mcp.aca_env.start",
        endpoint=mcp_url,
        app_name=app_name,
        resource_group=resource_group,
        auth_mode="app_only" if audience else "anonymous",
        audience=audience,
    )

    mcp_token = ""
    if audience:
        try:
            mcp_token = acquire_mcp_token(audience)
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            log_event(
                "mcp.token.failed",
                level="error",
                audience=audience,
                error=detail,
                duration_ms=elapsed_ms(started),
            )
            return None, f"Failed to acquire the MCP access token: {detail}"
        except Exception as exc:  # noqa: BLE001
            log_exception("mcp.token.failed", exc, audience=audience, duration_ms=elapsed_ms(started))
            return None, f"Failed to acquire the MCP access token: {exc}"

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
            token=mcp_token,
            timeout=timeout,
        )
    except HTTPError as exc:
        detail = _http_error_detail(exc)
        log_event(
            "mcp.aca_env.http_failed",
            level="error",
            endpoint=mcp_url,
            error=detail,
            auth_mode="app_only" if audience else "anonymous",
            duration_ms=elapsed_ms(started),
        )
        return None, detail
    except URLError as exc:
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
        auth_mode="app_only" if audience else "anonymous",
        revision=result.get("revision", "") if isinstance(result, dict) else "",
        variable_count=len(variables),
        obo_token_count=len(obo),
        duration_ms=elapsed_ms(started),
    )
    return result, None
