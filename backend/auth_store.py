"""Auth state persistence for the local app.

Running under uvicorn keeps OAuth state and auth tokens in process memory
(LocalAuthStore). An optional AzureBlobAuthStore mirrors the same records to
Azure Blob Storage when a shared/multi-instance store is needed.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

from .diagnostics import elapsed_ms, log_event, log_exception, now_ms


def _normalize_prefix(prefix: str) -> str:
    return "/".join(part for part in (prefix or "").strip("/").split("/") if part)


def _safe_blob_segment(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", ".", "@"} else "-" for char in value.lower())
    return safe.strip("-._") or "unknown"


def _expired(record: dict[str, Any] | None) -> bool:
    if not record:
        return True
    try:
        return float(record.get("expires_at", 0)) <= time.time() + 30
    except (TypeError, ValueError):
        return True


class AuthStore(Protocol):
    def save_oauth_state(self, state: str, record: dict[str, Any]) -> None: ...
    def pop_oauth_state(self, state: str) -> dict[str, Any] | None: ...
    def save_auth_token(self, auth_id: str, record: dict[str, Any]) -> None: ...
    def get_auth_token(self, auth_id: str) -> dict[str, Any] | None: ...
    def delete_auth_token(self, auth_id: str) -> None: ...


class LocalAuthStore:
    def __init__(self, auth_tokens: dict[str, dict[str, Any]], oauth_states: dict[str, dict[str, Any]]) -> None:
        self.auth_tokens = auth_tokens
        self.oauth_states = oauth_states
        log_event("auth_store.local.ready")

    def save_oauth_state(self, state: str, record: dict[str, Any]) -> None:
        self.oauth_states[state] = dict(record)

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        record = self.oauth_states.pop(state, None)
        return None if _expired(record) else record

    def save_auth_token(self, auth_id: str, record: dict[str, Any]) -> None:
        self.auth_tokens[auth_id] = dict(record)

    def get_auth_token(self, auth_id: str) -> dict[str, Any] | None:
        record = self.auth_tokens.get(auth_id)
        if _expired(record):
            self.auth_tokens.pop(auth_id, None)
            return None
        return record

    def delete_auth_token(self, auth_id: str) -> None:
        self.auth_tokens.pop(auth_id, None)


class AzureBlobAuthStore(LocalAuthStore):
    def __init__(self, auth_tokens: dict[str, dict[str, Any]], oauth_states: dict[str, dict[str, Any]]) -> None:
        super().__init__(auth_tokens, oauth_states)
        from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        account_url = os.getenv("SGV2_AUTH_STORAGE_ACCOUNT_URL", "").strip().rstrip("/") or os.getenv("AZURE_STORAGE_ACCOUNT_URL", "").strip().rstrip("/")
        connection_string = os.getenv("SGV2_AUTH_STORAGE_CONNECTION_STRING", "").strip()
        if not connection_string:
            connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        if not account_url and not connection_string:
            raise ValueError("Azure Blob auth store requires SGV2_AUTH_STORAGE_ACCOUNT_URL/AZURE_STORAGE_ACCOUNT_URL or a storage connection string.")

        if connection_string:
            self.service = BlobServiceClient.from_connection_string(connection_string)
        else:
            credential = ChainedTokenCredential(ManagedIdentityCredential(), AzureCliCredential())
            self.service = BlobServiceClient(account_url=account_url, credential=credential)

        self.container = (
            os.getenv("SGV2_AUTH_BLOB_CONTAINER", "").strip()
            or os.getenv("SGV2_SESSION_BLOB_CONTAINER", "").strip()
            or os.getenv("AZURE_BLOB_CONTAINER", "").strip()
        )
        if not self.container:
            raise ValueError("Azure Blob auth store requires SGV2_AUTH_BLOB_CONTAINER or AZURE_BLOB_CONTAINER.")
        self.prefix = _normalize_prefix(os.getenv("SGV2_AUTH_BLOB_PREFIX", "auth"))
        host = account_url.split("://", 1)[-1].split("/", 1)[0] if account_url else "connection_string"
        self.id = f"azure:{host}/{self.container}/{self.prefix}"
        self._container_checked = False
        log_event("auth_store.azure.ready", store_id=self.id, container=self.container, prefix=self.prefix)
    def _container(self):
        return self.service.get_container_client(self.container)

    def _blob_name(self, kind: str, key: str) -> str:
        safe = _safe_blob_segment(key)
        path = f"{kind}/{safe}.json"
        return f"{self.prefix}/{path}" if self.prefix else path

    def _save_blob(self, kind: str, key: str, record: dict[str, Any]) -> None:
        started = now_ms()
        blob_name = self._blob_name(kind, key)
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        container = self._container()
        try:
            container.upload_blob(blob_name, data, overwrite=True)
        except Exception as exc:  # noqa: BLE001
            if "ContainerNotFound" not in str(exc):
                raise
            try:
                self.service.create_container(self.container)
            except Exception:
                pass
            container.upload_blob(blob_name, data, overwrite=True)
        log_event("auth_store.azure.save.done", kind=kind, blob_name=blob_name, bytes=len(data), duration_ms=elapsed_ms(started))
    def _load_blob(self, kind: str, key: str) -> dict[str, Any] | None:
        started = now_ms()
        blob_name = self._blob_name(kind, key)
        try:
            payload = self._container().download_blob(blob_name).readall().decode("utf-8")
            record = json.loads(payload)
            if not isinstance(record, dict):
                return None
            log_event("auth_store.azure.load.done", kind=kind, blob_name=blob_name, duration_ms=elapsed_ms(started))
            return record
        except Exception as exc:  # noqa: BLE001
            log_exception("auth_store.azure.load.failed", exc, kind=kind, blob_name=blob_name)
            return None

    def _delete_blob(self, kind: str, key: str) -> None:
        blob_name = self._blob_name(kind, key)
        try:
            self._container().delete_blob(blob_name)
        except Exception as exc:  # noqa: BLE001
            log_event("auth_store.azure.delete.skipped", level="warning", kind=kind, blob_name=blob_name, error=str(exc)[:200])

    def save_oauth_state(self, state: str, record: dict[str, Any]) -> None:
        super().save_oauth_state(state, record)
        self._save_blob("oauth-states", state, record)

    def pop_oauth_state(self, state: str) -> dict[str, Any] | None:
        record = self.oauth_states.pop(state, None) or self._load_blob("oauth-states", state)
        self._delete_blob("oauth-states", state)
        return None if _expired(record) else record

    def save_auth_token(self, auth_id: str, record: dict[str, Any]) -> None:
        super().save_auth_token(auth_id, record)
        self._save_blob("auth-tokens", auth_id, record)

    def get_auth_token(self, auth_id: str) -> dict[str, Any] | None:
        record = self.auth_tokens.get(auth_id) or self._load_blob("auth-tokens", auth_id)
        if _expired(record):
            self.delete_auth_token(auth_id)
            return None
        self.auth_tokens[auth_id] = record
        return record

    def delete_auth_token(self, auth_id: str) -> None:
        super().delete_auth_token(auth_id)
        self._delete_blob("auth-tokens", auth_id)


def make_auth_store(auth_tokens: dict[str, dict[str, Any]], oauth_states: dict[str, dict[str, Any]]) -> AuthStore:
    store_kind = os.getenv("SGV2_AUTH_STORE", "local").strip().lower()
    if store_kind in {"blob", "azure-blob", "azure_blob"}:
        return AzureBlobAuthStore(auth_tokens, oauth_states)
    return LocalAuthStore(auth_tokens, oauth_states)