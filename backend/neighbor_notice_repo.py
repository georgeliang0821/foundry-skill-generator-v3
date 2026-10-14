"""Relationship-only notifications; all SQL mutations run in one transaction."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from typing import Callable

from . import db


@dataclass
class NeighborNotice:
    id: int
    skill_key: str
    target_user: str
    proposer_upn: str
    source_skill_keys: list[str] = field(default_factory=list)
    status: str = "open"
    modify_session_id: str | None = None


def _sources(values: list[str]) -> list[str]:
    if not values or any(not isinstance(value, str) or not value.strip() or len(value) > 321 for value in values):
        raise ValueError("Source skill keys must be a nonempty list of exact skill keys.")
    return sorted(set(values))


def _identity(value: str) -> str:
    result = value.strip().lower()
    if not result or len(result) > 256:
        raise ValueError("A verified user identity is required.")
    return result


_COLUMNS = "id, skill_key, target_user, proposer_upn, source_skill_keys_json, status, modify_session_id"


def _notice(row) -> NeighborNotice:
    return NeighborNotice(*row[:4], _sources(json.loads(row[4])), *row[5:])


class LocalNeighborNoticeRepo:
    def __init__(self) -> None:
        self._rows: dict[int, NeighborNotice] = {}
        self._lock = threading.RLock()

    def upsert(self, skill_key: str, target_user: str, proposer_upn: str, sources: list[str]) -> NeighborNotice:
        with self._lock:
            existing = next((row for row in self._rows.values() if row.status == "open" and row.skill_key == skill_key and row.proposer_upn == proposer_upn), None)
            if existing:
                existing.source_skill_keys = _sources(existing.source_skill_keys + sources)
                existing.target_user = target_user
            else:
                existing = NeighborNotice(len(self._rows) + 1, skill_key, target_user, proposer_upn, sources)
                self._rows[existing.id] = existing
            return replace(existing, source_skill_keys=list(existing.source_skill_keys))

    def list_open(self, user: str) -> list[NeighborNotice]:
        with self._lock:
            return [replace(row, source_skill_keys=list(row.source_skill_keys)) for row in reversed(list(self._rows.values())) if row.target_user == user and row.status == "open"]

    def resolve(self, notice_id: int, user: str, status: str, create_session: Callable[[NeighborNotice], str] | None) -> NeighborNotice:
        with self._lock:
            row = self._rows.get(notice_id)
            if not row or row.target_user != user:
                raise LookupError("Notification not found.")
            if row.status != "open":
                if row.status != status:
                    raise ValueError("Notification is already closed.")
                return replace(row, source_skill_keys=list(row.source_skill_keys))
            session_id = create_session(replace(row, source_skill_keys=list(row.source_skill_keys))) if create_session else None
            row.modify_session_id = session_id
            row.status = status
            return replace(row, source_skill_keys=list(row.source_skill_keys))


_local_repo: LocalNeighborNoticeRepo | None = None


def make_notice_repo(*, local: bool) -> None:
    global _local_repo
    _local_repo = LocalNeighborNoticeRepo() if local else None


def upsert_open_notice(skill_key: str, target_user: str, proposer_upn: str, source_skill_keys: list[str]) -> NeighborNotice:
    if not skill_key or len(skill_key) > 321:
        raise ValueError("An exact target skill key is required.")
    target_user = _identity(target_user)
    proposer_upn = _identity(proposer_upn)
    sources = _sources(source_skill_keys)
    if _local_repo is not None:
        return _local_repo.upsert(skill_key, target_user, proposer_upn, sources)

    def operation(cursor):
        cursor.execute(f"SELECT {_COLUMNS} FROM dbo.skill_neighbor_notice WITH (UPDLOCK, HOLDLOCK) WHERE skill_key = ? AND proposer_upn = ? AND status = 'open'", (skill_key, proposer_upn))
        result = cursor.fetchone()
        if result:
            notice = _notice(result)
            sources_json = json.dumps(_sources(notice.source_skill_keys + sources))
            cursor.execute("UPDATE dbo.skill_neighbor_notice SET source_skill_keys_json = ?, target_user = ?, updated_at = SYSUTCDATETIME() WHERE id = ?", (sources_json, target_user, notice.id))
            return replace(notice, target_user=target_user, source_skill_keys=json.loads(sources_json))
        cursor.execute(f"INSERT INTO dbo.skill_neighbor_notice (skill_key, target_user, proposer_upn, source_skill_keys_json) OUTPUT inserted.id VALUES (?, ?, ?, ?)", (skill_key, target_user, proposer_upn, json.dumps(sources)))
        return NeighborNotice(int(cursor.fetchone()[0]), skill_key, target_user, proposer_upn, sources)

    return db.run(operation)


def list_open_notices(user: str) -> list[NeighborNotice]:
    user = _identity(user)
    if _local_repo is not None:
        return _local_repo.list_open(user)
    return [_notice(row) for row in db.fetchall(f"SELECT {_COLUMNS} FROM dbo.skill_neighbor_notice WHERE target_user = ? AND status = 'open' ORDER BY created_at DESC, id DESC", (user,))]


def resolve_notice(notice_id: int, user: str, status: str, create_session: Callable[[NeighborNotice], str] | None = None) -> NeighborNotice:
    user = _identity(user)
    if status not in {"accepted", "dismissed"} or (status == "accepted" and create_session is None):
        raise ValueError("Invalid notification action.")
    if _local_repo is not None:
        return _local_repo.resolve(notice_id, user, status, create_session)

    def operation(cursor):
        cursor.execute(f"SELECT {_COLUMNS} FROM dbo.skill_neighbor_notice WITH (UPDLOCK, HOLDLOCK) WHERE id = ? AND target_user = ?", (notice_id, user))
        result = cursor.fetchone()
        if not result:
            raise LookupError("Notification not found.")
        notice = _notice(result)
        if notice.status != "open":
            if notice.status != status:
                raise ValueError("Notification is already closed.")
            return notice
        session_id = create_session(notice) if create_session else None
        cursor.execute("UPDATE dbo.skill_neighbor_notice SET status = ?, modify_session_id = ?, closed_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME() WHERE id = ? AND status = 'open'", (status, session_id, notice_id))
        return replace(notice, status=status, modify_session_id=session_id)

    return db.run(operation)