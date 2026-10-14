"""Small structured logging helpers for local backend debugging."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

SENSITIVE_KEYS = ("authorization", "secret", "password", "token", "key", "credential")
SAFE_KEYS = ("token_claims", "has_delegated_token")
DIAGNOSTICS_REVISION = "foundry-phases-v2"
_log_fields: ContextVar[dict[str, Any]] = ContextVar("log_fields", default={})


def now_ms() -> int:
    return int(time.perf_counter() * 1000)


def elapsed_ms(started_ms: int) -> int:
    return max(0, now_ms() - started_ms)


def _enabled() -> bool:
    return os.getenv("SGV2_DEBUG_LOG", "1").strip().lower() not in {"0", "false", "no", "off"}


def _mask_string(value: str) -> str:
    if not value:
        return value
    if value == "missing" or value.startswith("set(len="):
        return value
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-4:]} (len={len(value)})"


def _sanitize(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered not in SAFE_KEYS and any(marker in lowered for marker in SENSITIVE_KEYS):
        if isinstance(value, bool) or value is None:
            return value
        return _mask_string(str(value))
    if isinstance(value, dict):
        return {str(k): _sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, key=key) for item in value]
    return value


def log_event(event: str, /, level: str = "info", **fields: Any) -> None:
    """Print one sanitized JSON log line.

    The app is often run directly with uvicorn in PowerShell, so stdout logging is
    the most reliable sink. JSON lines keep the logs grep-friendly and compact.
    """
    if not _enabled():
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        **_log_fields.get(),
        **fields,
    }
    print(json.dumps(_sanitize(record), ensure_ascii=False, default=str), flush=True)


def log_exception(event: str, exc: BaseException, /, **fields: Any) -> None:
    log_event(event, level="error", error_type=type(exc).__name__, error=str(exc), **fields)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    context_handle = _log_fields.set({**_log_fields.get(), **fields})
    try:
        yield
    finally:
        _log_fields.reset(context_handle)


@contextmanager
def log_timing(event: str, /, **fields: Any) -> Iterator[None]:
    started = now_ms()
    log_event(f"{event}.start", **fields)
    try:
        yield
    except Exception as exc:
        log_exception(f"{event}.failed", exc, duration_ms=elapsed_ms(started), **fields)
        raise
    else:
        log_event(f"{event}.done", duration_ms=elapsed_ms(started), **fields)


def env_flag(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        return "missing"
    return f"set(len={len(value)})"
