"""JSON persistence for authoring sessions.

Sessions are stored as JSON files on local disk by default. Set
``SGV2_SESSION_STORE=blob`` to store the same session JSON documents in Azure
Blob Storage instead (useful for shared/multi-instance setups).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Protocol

from .diagnostics import elapsed_ms, log_event, log_exception, now_ms
from .models import MessageRole, Session, SessionSummary, migrate_legacy_session


class SessionStore(Protocol):
    def load_all(self) -> dict[str, Session]: ...
    def load(self, session_id: str) -> Session | None: ...
    def save(self, session: Session) -> None: ...
    def list(self, sessions: dict[str, Session]) -> list[SessionSummary]: ...


class _SessionSummaryMixin:
    def list(self, sessions: dict[str, Session]) -> list[SessionSummary]:
        return sorted(
            [self.summary(session) for session in sessions.values()],
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def summary(self, session: Session) -> SessionSummary:
        title = self._session_title(session)
        return SessionSummary(
            id=session.id,
            mode=session.mode,
            skill_kind=session.skill_kind,
            target_skill_id=session.target_skill_id,
            current_stage=session.current_stage,
            title=title,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=len(session.conversation),
            material_count=len(session.materials),
            pending_tool_count=len(session.pending_tool_calls),
        )

    def _session_title(self, session: Session) -> str:
        if session.mode == "modify" and session.target_skill_id:
            return session.target_skill_id
        for line in session.current_skill.skill_md.splitlines():
            if line.strip().lower().startswith("name:"):
                value = line.split(":", 1)[1].strip().strip("'\"")
                if value:
                    return value
        for message in session.conversation:
            if message.role != MessageRole.USER:
                continue
            if not message.content.strip():
                continue
            meta = getattr(message, "metadata", None) or {}
            if meta.get("auto_continue"):
                continue
            text = " ".join(message.content.split())
            return text[:80]
        if session.materials:
            text = " ".join(session.materials[0].content.split())
            return text[:80]
        if session.target_skill_id:
            return session.target_skill_id
        return "Untitled session"


class LocalSessionStore(_SessionSummaryMixin):
    def __init__(self, root: Path | None = None) -> None:
        configured = os.getenv("SGV2_SESSION_DIR", "").strip()
        self.root = Path(configured) if configured else root or Path(__file__).resolve().parents[1] / ".sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        log_event("session_store.ready", root=str(self.root), configured=bool(configured))

    def load_all(self) -> dict[str, Session]:
        started = now_ms()
        sessions: dict[str, Session] = {}
        for path in self.root.glob("*.json"):
            session = self._load_path(path)
            if session is not None:
                sessions[session.id] = session
        log_event("session_store.load_all.done", count=len(sessions), duration_ms=elapsed_ms(started))
        return sessions

    def load(self, session_id: str) -> Session | None:
        return self._load_path(self._path(session_id))

    def _load_path(self, path: Path) -> Session | None:
        if not path.exists():
            return None
        started = now_ms()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw = migrate_legacy_session(raw)
            session = Session.model_validate(raw)
            log_event("session_store.load_one.done", session_id=session.id, path=str(path), duration_ms=elapsed_ms(started))
            return session
        except Exception as exc:  # noqa: BLE001 - one bad session should not block startup.
            log_exception("session_store.load_one.failed", exc, path=str(path))
            return None

    def save(self, session: Session) -> None:
        started = now_ms()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(session.id)
        # The suffix must be unique per write, not just per process: two
        # concurrent saves of the SAME session would otherwise share one temp
        # path, and whichever lost the race would replace() a file the winner
        # had already moved away (WinError 2).
        tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        log_event(
            "session_store.save.done",
            session_id=session.id,
            stage=session.current_stage,
            path=str(path),
            bytes=path.stat().st_size if path.exists() else 0,
            duration_ms=elapsed_ms(started),
        )

    def _path(self, session_id: str) -> Path:
        safe = "".join(char for char in session_id if char.isalnum() or char in {"-", "_"})
        return self.root / f"{safe}.json"


def _normalize_prefix(prefix: str) -> str:
    return "/".join(part for part in (prefix or "").strip("/").split("/") if part)


def _safe_blob_segment(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_", ".", "@"} else "-" for char in value.lower())
    return safe.strip("-._") or "unknown"


class AzureBlobSessionStore(_SessionSummaryMixin):
    def __init__(self) -> None:
        from azure.identity import AzureCliCredential, ChainedTokenCredential, ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "").strip().rstrip("/")
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        if not account_url and not connection_string:
            raise ValueError("Azure Blob session store requires AZURE_STORAGE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING.")

        if connection_string:
            self.service = BlobServiceClient.from_connection_string(connection_string)
        else:
            credential = ChainedTokenCredential(ManagedIdentityCredential(), AzureCliCredential())
            self.service = BlobServiceClient(account_url=account_url, credential=credential)

        self.container = (
            os.getenv("SGV2_SESSION_BLOB_CONTAINER", "").strip()
            or os.getenv("AZURE_BLOB_CONTAINER", "").strip()
        )
        if not self.container:
            raise ValueError("Azure Blob session store requires SGV2_SESSION_BLOB_CONTAINER or AZURE_BLOB_CONTAINER.")
        self.prefix = _normalize_prefix(os.getenv("SGV2_SESSION_BLOB_PREFIX", "sessions"))
        host = account_url.split("://", 1)[-1].split("/", 1)[0] if account_url else "connection_string"
        self.id = f"azure:{host}/{self.container}/{self.prefix}"
        log_event("session_store.azure.ready", store_id=self.id, container=self.container, prefix=self.prefix)

    def _container(self):
        return self.service.get_container_client(self.container)

    def _blob_name(self, session: Session) -> str:
        owner = _safe_blob_segment(session.owner_upn or "unknown")
        session_id = _safe_blob_segment(session.id)
        return f"{self.prefix}/{owner}/{session_id}.json" if self.prefix else f"{owner}/{session_id}.json"

    def _load_blob(self, blob_name: str) -> Session | None:
        started = now_ms()
        try:
            payload = self._container().download_blob(blob_name).readall().decode("utf-8")
            raw = migrate_legacy_session(json.loads(payload))
            session = Session.model_validate(raw)
            log_event("session_store.azure.load_one.done", session_id=session.id, blob_name=blob_name, duration_ms=elapsed_ms(started))
            return session
        except Exception as exc:  # noqa: BLE001
            log_exception("session_store.azure.load_one.failed", exc, blob_name=blob_name)
            return None

    def load_all(self) -> dict[str, Session]:
        started = now_ms()
        sessions: dict[str, Session] = {}
        prefix = f"{self.prefix}/" if self.prefix else ""
        try:
            for blob in self._container().list_blobs(name_starts_with=prefix):
                name = str(blob.name)
                if not name.endswith(".json"):
                    continue
                session = self._load_blob(name)
                if session is not None:
                    sessions[session.id] = session
        except Exception as exc:  # noqa: BLE001
            log_exception("session_store.azure.load_all.failed", exc, store_id=self.id)
        log_event("session_store.azure.load_all.done", count=len(sessions), store_id=self.id, duration_ms=elapsed_ms(started))
        return sessions

    def load(self, session_id: str) -> Session | None:
        safe_id = _safe_blob_segment(session_id)
        prefix = f"{self.prefix}/" if self.prefix else ""
        suffix = f"/{safe_id}.json"
        try:
            for blob in self._container().list_blobs(name_starts_with=prefix):
                name = str(blob.name)
                if name.endswith(suffix):
                    return self._load_blob(name)
        except Exception as exc:  # noqa: BLE001
            log_exception("session_store.azure.load_by_id.failed", exc, session_id=session_id)
        return None

    def save(self, session: Session) -> None:
        started = now_ms()
        blob_name = self._blob_name(session)
        data = session.model_dump_json(indent=2).encode("utf-8")
        self._container().upload_blob(blob_name, data, overwrite=True)
        log_event(
            "session_store.azure.save.done",
            session_id=session.id,
            stage=session.current_stage,
            blob_name=blob_name,
            bytes=len(data),
            duration_ms=elapsed_ms(started),
        )


def make_session_store() -> SessionStore:
    store_kind = os.getenv("SGV2_SESSION_STORE", "local").strip().lower()
    if store_kind in {"blob", "azure-blob", "azure_blob"}:
        return AzureBlobSessionStore()
    return LocalSessionStore()