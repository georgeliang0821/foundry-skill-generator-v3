"""DAO for Azure SQL skills metadata and per-user grants (schema v2.2).

Tables (from dbo schema):
  - dbo.skills(skill_name, owner_upn, is_public, is_internal, enabled,
               created_at, updated_at,
               skill_key / blob_path / blob_prefix = PERSISTED computed columns)
    PK = skill_key. There is no ``version`` or ``description`` column: the
    description's single source of truth is the SKILL.md frontmatter on Blob.
  - dbo.user_skill_grants(user_upn, skill_key FK CASCADE, granted_at,
                          granted_by, expires_at)
    PK = (user_upn, skill_key)

Why these queries mirror ``dbo.v_my_skills`` instead of selecting from it:
this app connects with a single service principal (see ``db.py``), which is the
identity the RLS predicate bypasses. Selecting the view would therefore return
every row, and the view exposes no ``user_upn`` column to filter on afterwards.
So the view's WHERE logic (grant OR is_public) is reproduced here against the
base tables with an explicit ``g.user_upn = ?``.

This module is pure data access; permission ENFORCEMENT lives in ``acl.py``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import db
from .blob_store import blob_path_of, blob_prefix_of  # noqa: F401 -- re-exported
from .diagnostics import log_event


def skill_key_of(skill_name: str, owner_upn: str | None = None) -> str:
    """Mirrors the PERSISTED computed column dbo.skills.skill_key."""
    owner = (owner_upn or "").strip()
    return f"{skill_name}_{owner}" if owner else skill_name


@dataclass
class SkillRow:
    skill_name: str
    skill_key: str
    owner_upn: str | None
    is_public: bool
    is_internal: bool
    enabled: bool
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def skill_scope(self) -> str:
        return "private" if self.owner_upn else "global"

    def to_payload(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_key": self.skill_key,
            "owner_upn": self.owner_upn or "",
            "skill_scope": self.skill_scope,
            "is_public": bool(self.is_public),
            "is_internal": bool(self.is_internal),
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }


@dataclass
class GrantRow:
    user_upn: str
    skill_name: str
    skill_key: str
    granted_at: datetime | None
    granted_by: str | None
    expires_at: datetime | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_upn": self.user_upn,
            "skill_name": self.skill_name,
            "skill_key": self.skill_key,
            "granted_at": self.granted_at.isoformat() if self.granted_at else "",
            "granted_by": self.granted_by or "",
            "expires_at": self.expires_at.isoformat() if self.expires_at else "",
        }


class LocalSkillsRepo:
    """Process-local mirror of the skills metadata and grants tables."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillRow] = {}
        self._grants: dict[tuple[str, str], GrantRow] = {}
        self._lock = threading.RLock()
        log_event("skills_repo.local.ready")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _grant_is_live(grant: GrantRow | None, now: datetime) -> bool:
        if grant is None or grant.expires_at is None:
            return grant is not None
        expires_at = grant.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > now

    def _is_visible(self, user_upn: str, row: SkillRow, now: datetime) -> bool:
        grant = self._grants.get((user_upn, row.skill_key))
        return row.enabled and (row.is_public or self._grant_is_live(grant, now))

    def list_skills_for_user(self, user_upn: str) -> list[SkillRow]:
        if not user_upn:
            return []
        with self._lock:
            now = self._now()
            rows = [row for row in self._skills.values() if self._is_visible(user_upn, row, now)]
            return sorted(
                rows,
                key=lambda row: (
                    -(row.updated_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
                    row.skill_name,
                ),
            )

    def get_skill(self, skill_name: str, owner_upn: str | None = None) -> SkillRow | None:
        with self._lock:
            return self._skills.get(skill_key_of(skill_name, owner_upn))

    def upsert_skill(
        self,
        skill_name: str,
        *,
        owner_upn: str | None = None,
        is_public: bool = False,
        enabled: bool = True,
        is_internal: bool | None = None,
    ) -> bool:
        owner = (owner_upn or "").strip() or None
        if is_public and owner:
            raise ValueError("is_public requires a global skill (owner_upn must be empty).")
        key = skill_key_of(skill_name, owner)
        with self._lock:
            now = self._now()
            existing = self._skills.get(key)
            self._skills[key] = SkillRow(
                skill_name=skill_name,
                skill_key=key,
                owner_upn=owner,
                is_public=is_public,
                is_internal=bool(is_internal) if is_internal is not None else bool(existing and existing.is_internal),
                enabled=enabled,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            return existing is None

    def set_skill_visibility(
        self,
        skill_name: str,
        is_public: bool,
        owner_upn: str | None = None,
    ) -> int:
        owner = (owner_upn or "").strip() or None
        if is_public and owner:
            raise ValueError("is_public requires a global skill (owner_upn must be empty).")
        key = skill_key_of(skill_name, owner)
        with self._lock:
            row = self._skills.get(key)
            if row is None:
                return 0
            row.is_public = is_public
            row.updated_at = self._now()
            return 1

    def delete_skill(self, skill_name: str, owner_upn: str | None = None) -> None:
        key = skill_key_of(skill_name, owner_upn)
        with self._lock:
            self._skills.pop(key, None)
            for grant_key in [candidate for candidate in self._grants if candidate[1] == key]:
                self._grants.pop(grant_key, None)

    def list_grants(self, skill_name: str, owner_upn: str | None = None) -> list[GrantRow]:
        key = skill_key_of(skill_name, owner_upn)
        with self._lock:
            rows = [grant for (_, grant_key), grant in self._grants.items() if grant_key == key]
            return sorted(
                rows,
                key=lambda grant: grant.granted_at or datetime.min.replace(tzinfo=timezone.utc),
            )

    def list_grants_for_user(self, user_upn: str) -> set[str]:
        return {row.skill_name for row in self.list_skills_for_user(user_upn)}

    def add_grant(
        self,
        skill_name: str,
        user_upn: str,
        granted_by: str | None = None,
        expires_at: datetime | None = None,
        owner_upn: str | None = None,
    ) -> bool:
        skill_key = skill_key_of(skill_name, owner_upn)
        key = (user_upn, skill_key)
        with self._lock:
            existing = self._grants.get(key)
            self._grants[key] = GrantRow(
                user_upn=user_upn,
                skill_name=skill_name,
                skill_key=skill_key,
                granted_at=existing.granted_at if existing else self._now(),
                granted_by=granted_by if granted_by is not None else (existing.granted_by if existing else None),
                expires_at=expires_at,
            )
            return existing is None

    def remove_grant(
        self,
        skill_name: str,
        user_upn: str,
        owner_upn: str | None = None,
    ) -> int:
        with self._lock:
            key = (user_upn, skill_key_of(skill_name, owner_upn))
            return 1 if self._grants.pop(key, None) is not None else 0

    def user_has_access(
        self,
        user_upn: str,
        skill_name: str,
        owner_upn: str | None = None,
    ) -> bool:
        if not user_upn or not skill_name:
            return False
        with self._lock:
            row = self._skills.get(skill_key_of(skill_name, owner_upn))
            return bool(row and self._is_visible(user_upn, row, self._now()))


_local_repo: LocalSkillsRepo | None = None


def make_skills_repo() -> LocalSkillsRepo | None:
    """Select SQL or a fresh process-local repository from SGV2_SKILL_STORE."""
    global _local_repo
    mode = os.getenv("SGV2_SKILL_STORE", "azure").strip().lower()
    _local_repo = LocalSkillsRepo() if mode == "local" else None
    log_event("skills_repo.select.local" if _local_repo else "skills_repo.select.sql")
    return _local_repo


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

_SKILL_COLUMNS = """
        s.skill_name, s.skill_key, s.owner_upn, s.is_public, s.is_internal,
        s.enabled, s.created_at, s.updated_at
"""


def list_skills_for_user(user_upn: str) -> list[SkillRow]:
    """Skills visible to the user: a live grant OR is_public, enabled-only.

    Reproduces the WHERE clause of dbo.v_my_skills (see module docstring).
    """
    if _local_repo is not None:
        return _local_repo.list_skills_for_user(user_upn)
    if not user_upn:
        return []
    sql = f"""
        SELECT {_SKILL_COLUMNS}
        FROM dbo.skills s
        LEFT JOIN dbo.user_skill_grants g
               ON g.skill_key = s.skill_key
              AND g.user_upn = ?
              AND (g.expires_at IS NULL OR g.expires_at > SYSUTCDATETIME())
        WHERE s.enabled = 1
          AND (g.user_upn IS NOT NULL OR s.is_public = 1)
        ORDER BY s.updated_at DESC, s.skill_name ASC
    """
    rows = db.fetchall(sql, (user_upn,))
    return [SkillRow(*r) for r in rows]


def get_skill(skill_name: str, owner_upn: str | None = None) -> SkillRow | None:
    if _local_repo is not None:
        return _local_repo.get_skill(skill_name, owner_upn)
    row = db.fetchone(
        f"SELECT {_SKILL_COLUMNS} FROM dbo.skills s WHERE s.skill_key = ?",
        (skill_key_of(skill_name, owner_upn),),
    )
    return SkillRow(*row) if row else None


def upsert_skill(
    skill_name: str,
    *,
    owner_upn: str | None = None,
    is_public: bool = False,
    enabled: bool = True,
    is_internal: bool | None = None,
) -> bool:
    """MERGE INTO dbo.skills. Returns True if a new row was inserted.

    Never touches skill_key / blob_path / blob_prefix (PERSISTED computed
    columns -- SQL Server rejects explicit assignment). ``is_internal=None``
    preserves an existing value and inserts the schema default of false.
    """
    if _local_repo is not None:
        return _local_repo.upsert_skill(
            skill_name,
            owner_upn=owner_upn,
            is_public=is_public,
            enabled=enabled,
            is_internal=is_internal,
        )
    owner = (owner_upn or "").strip() or None
    if is_public and owner:
        raise ValueError("is_public requires a global skill (owner_upn must be empty).")
    sql = """
        MERGE dbo.skills AS target
        USING (
            SELECT CAST(? AS VARCHAR(321)) AS skill_key,
                   CAST(? AS VARCHAR(64))  AS skill_name,
                   CAST(? AS VARCHAR(256)) AS owner_upn,
                   CAST(? AS BIT)          AS is_public,
                   CAST(? AS BIT)          AS enabled,
                   CAST(? AS BIT)          AS is_internal
        ) AS source
        ON target.skill_key = source.skill_key
        WHEN MATCHED THEN
            UPDATE SET is_public = source.is_public,
                       enabled = source.enabled,
                       is_internal = COALESCE(source.is_internal, target.is_internal),
                       updated_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (skill_name, owner_upn, is_public, enabled, is_internal)
            VALUES (source.skill_name, source.owner_upn, source.is_public, source.enabled,
                    COALESCE(source.is_internal, 0))
        OUTPUT $action;
    """
    params = (
        skill_key_of(skill_name, owner),
        skill_name,
        owner,
        1 if is_public else 0,
        1 if enabled else 0,
        None if is_internal is None else (1 if is_internal else 0),
    )
    def _op(cur) -> bool:
        cur.execute(sql, params)
        try:
            action_row = cur.fetchone()
        except Exception:  # noqa: BLE001
            action_row = None
        return bool(action_row and str(action_row[0]).upper() == "INSERT")

    was_insert = db.run(_op)
    log_event(
        "skills_repo.upsert",
        skill_name=skill_name,
        is_public=is_public,
        action="insert" if was_insert else "update",
    )
    return was_insert


def set_skill_visibility(skill_name: str, is_public: bool, owner_upn: str | None = None) -> int:
    """Flip is_public on an existing skill. Returns rows affected."""
    if _local_repo is not None:
        return _local_repo.set_skill_visibility(skill_name, is_public, owner_upn)
    owner = (owner_upn or "").strip() or None
    if is_public and owner:
        raise ValueError("is_public requires a global skill (owner_upn must be empty).")
    n = db.execute(
        "UPDATE dbo.skills SET is_public = ?, updated_at = SYSUTCDATETIME() WHERE skill_key = ?",
        (1 if is_public else 0, skill_key_of(skill_name, owner)),
    )
    log_event("skills_repo.visibility", skill_name=skill_name, is_public=is_public, rows=n)
    return n


def delete_skill(skill_name: str, owner_upn: str | None = None) -> None:
    """Delete the skill row (cascade clears grants)."""
    if _local_repo is not None:
        _local_repo.delete_skill(skill_name, owner_upn)
        return
    db.execute(
        "DELETE FROM dbo.skills WHERE skill_key = ?",
        (skill_key_of(skill_name, owner_upn),),
    )
    log_event("skills_repo.delete", skill_name=skill_name)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def list_grants(skill_name: str, owner_upn: str | None = None) -> list[GrantRow]:
    if _local_repo is not None:
        return _local_repo.list_grants(skill_name, owner_upn)
    key = skill_key_of(skill_name, owner_upn)
    sql = """
        SELECT user_upn, granted_at, granted_by, expires_at
        FROM dbo.user_skill_grants
        WHERE skill_key = ?
        ORDER BY granted_at ASC
    """
    return [
        GrantRow(r[0], skill_name, key, r[1], r[2], r[3])
        for r in db.fetchall(sql, (key,))
    ]


def list_grants_for_user(user_upn: str) -> set[str]:
    """Skill names the user can currently reach: live grant OR is_public."""
    if _local_repo is not None:
        return _local_repo.list_grants_for_user(user_upn)
    if not user_upn:
        return set()
    sql = """
        SELECT s.skill_name
        FROM dbo.skills s
        LEFT JOIN dbo.user_skill_grants g
               ON g.skill_key = s.skill_key
              AND g.user_upn = ?
              AND (g.expires_at IS NULL OR g.expires_at > SYSUTCDATETIME())
        WHERE s.enabled = 1
          AND (g.user_upn IS NOT NULL OR s.is_public = 1)
    """
    return {r[0] for r in db.fetchall(sql, (user_upn,))}


def add_grant(
    skill_name: str,
    user_upn: str,
    granted_by: str | None = None,
    expires_at: datetime | None = None,
    owner_upn: str | None = None,
) -> bool:
    """Insert (or refresh) a grant. Returns True if a row was inserted."""
    if _local_repo is not None:
        return _local_repo.add_grant(
            skill_name,
            user_upn,
            granted_by=granted_by,
            expires_at=expires_at,
            owner_upn=owner_upn,
        )
    key = skill_key_of(skill_name, owner_upn)
    sql = """
        MERGE dbo.user_skill_grants AS target
        USING (
            SELECT CAST(? AS VARCHAR(256)) AS user_upn,
                   CAST(? AS VARCHAR(321)) AS skill_key
        ) AS source
        ON target.user_upn = source.user_upn AND target.skill_key = source.skill_key
        WHEN MATCHED THEN
            UPDATE SET granted_by = COALESCE(?, target.granted_by),
                       expires_at = ?
        WHEN NOT MATCHED THEN
            INSERT (user_upn, skill_key, granted_by, expires_at)
            VALUES (source.user_upn, source.skill_key, ?, ?)
        OUTPUT $action;
    """
    def _op(cur) -> bool:
        cur.execute(sql, (user_upn, key, granted_by, expires_at, granted_by, expires_at))
        try:
            row = cur.fetchone()
        except Exception:  # noqa: BLE001
            row = None
        return bool(row and str(row[0]).upper() == "INSERT")

    was_insert = db.run(_op)
    log_event(
        "skills_repo.grant_add",
        skill_name=skill_name,
        user_upn=user_upn,
        granted_by=granted_by or "",
        action="insert" if was_insert else "update",
    )
    return was_insert


def remove_grant(skill_name: str, user_upn: str, owner_upn: str | None = None) -> int:
    if _local_repo is not None:
        return _local_repo.remove_grant(skill_name, user_upn, owner_upn)
    n = db.execute(
        "DELETE FROM dbo.user_skill_grants WHERE skill_key = ? AND user_upn = ?",
        (skill_key_of(skill_name, owner_upn), user_upn),
    )
    log_event(
        "skills_repo.grant_remove",
        skill_name=skill_name,
        user_upn=user_upn,
        rows=n,
    )
    return n


def user_has_access(user_upn: str, skill_name: str, owner_upn: str | None = None) -> bool:
    if _local_repo is not None:
        return _local_repo.user_has_access(user_upn, skill_name, owner_upn)
    if not user_upn or not skill_name:
        return False
    sql = """
        SELECT 1
        FROM dbo.skills s
        LEFT JOIN dbo.user_skill_grants g
               ON g.skill_key = s.skill_key
              AND g.user_upn = ?
              AND (g.expires_at IS NULL OR g.expires_at > SYSUTCDATETIME())
        WHERE s.skill_key = ?
          AND s.enabled = 1
          AND (g.user_upn IS NOT NULL OR s.is_public = 1)
    """
    row = db.fetchone(sql, (user_upn, skill_key_of(skill_name, owner_upn)))
    return row is not None
