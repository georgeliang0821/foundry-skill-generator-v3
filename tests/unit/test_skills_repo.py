"""Tests for backend.skills_repo using a fake pyodbc cursor."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from backend import db, skills_repo


class FakeCursor:
    def __init__(self, behavior):
        self.behavior = behavior
        self.rowcount = 0
        self._next_rows: list = []

    def execute(self, sql, params=()):
        action = self.behavior.get("execute", lambda s, p: None)
        result = action(sql, params)
        if isinstance(result, list):
            self._next_rows = list(result)
            self.rowcount = len(result)
        elif isinstance(result, int):
            self.rowcount = result
        else:
            self._next_rows = []
            self.rowcount = 0
        return self

    def fetchall(self):
        rows = self._next_rows
        self._next_rows = []
        return rows

    def fetchone(self):
        return self._next_rows.pop(0) if self._next_rows else None

    def close(self):
        pass


@pytest.fixture()
def fake_db(monkeypatch):
    """Replace db.get_cursor / fetchall / fetchone / execute with controllable fakes."""
    monkeypatch.setenv("SGV2_SKILL_STORE", "azure")
    skills_repo.make_skills_repo()
    behavior = {"execute": lambda s, p: None}

    @contextmanager
    def fake_get_cursor():
        yield FakeCursor(behavior)

    monkeypatch.setattr(db, "get_cursor", fake_get_cursor)
    monkeypatch.setattr(db, "fetchall", lambda sql, params=(): FakeCursor(behavior).execute(sql, params).fetchall())
    monkeypatch.setattr(db, "fetchone", lambda sql, params=(): FakeCursor(behavior).execute(sql, params).fetchone())
    monkeypatch.setattr(db, "execute", lambda sql, params=(): FakeCursor(behavior).execute(sql, params).rowcount)
    return behavior


@pytest.fixture()
def local_repo(monkeypatch):
    monkeypatch.setenv("SGV2_SKILL_STORE", "local")
    repo = skills_repo.make_skills_repo()
    assert repo is not None
    yield repo
    monkeypatch.setenv("SGV2_SKILL_STORE", "azure")
    skills_repo.make_skills_repo()


def _row(*values):
    """Return a tuple-like row."""
    return tuple(values)


def test_list_skills_for_user_returns_empty_for_blank_upn(fake_db):
    assert skills_repo.list_skills_for_user("") == []


def test_list_skills_for_user_returns_rows(fake_db):
    from datetime import datetime
    sentinel_at = datetime(2025, 1, 1)
    captured = {}

    def behavior(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [_row("alpha", "alpha", None, False, False, True, sentinel_at, sentinel_at)]

    fake_db["execute"] = behavior
    rows = skills_repo.list_skills_for_user("u@x")
    assert len(rows) == 1
    assert rows[0].skill_name == "alpha"
    assert rows[0].skill_scope == "global"
    # Mirrors v_my_skills: a live grant OR is_public, never an INNER JOIN.
    assert "LEFT JOIN" in captured["sql"]
    assert "s.is_public = 1" in captured["sql"]
    assert captured["params"] == ("u@x",)


def test_user_has_access_false_when_no_row(fake_db):
    fake_db["execute"] = lambda s, p: []
    assert skills_repo.user_has_access("u@x", "alpha") is False


def test_user_has_access_true_when_row_returned(fake_db):
    fake_db["execute"] = lambda s, p: [_row(1)]
    assert skills_repo.user_has_access("u@x", "alpha") is True


def test_upsert_skill_inserts(fake_db):
    fake_db["execute"] = lambda s, p: [_row("INSERT")]
    assert skills_repo.upsert_skill("new") is True


def test_upsert_skill_updates(fake_db):
    fake_db["execute"] = lambda s, p: [_row("UPDATE")]
    assert skills_repo.upsert_skill("existing") is False


def test_upsert_skill_never_writes_computed_or_dropped_columns(fake_db):
    captured = {}

    def behavior(sql, params):
        captured["sql"] = sql
        return [_row("INSERT")]

    fake_db["execute"] = behavior
    skills_repo.upsert_skill("new", is_public=True)
    sql = captured["sql"]
    for column in ("blob_path", "blob_prefix", "version", "description"):
        assert column not in sql
    assert "target.skill_key = source.skill_key" in sql


def test_upsert_skill_writes_is_internal_only_when_provided(fake_db):
    captured = {}

    def behavior(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [_row("UPDATE")]

    fake_db["execute"] = behavior
    skills_repo.upsert_skill("child", is_internal=True)
    assert "is_internal = COALESCE(source.is_internal, target.is_internal)" in captured["sql"]
    assert captured["params"][-1] == 1

    skills_repo.upsert_skill("parent")
    assert captured["params"][-1] is None


def test_upsert_skill_rejects_public_private_skill(fake_db):
    with pytest.raises(ValueError):
        skills_repo.upsert_skill("mine", owner_upn="a@x", is_public=True)


def test_skill_key_and_blob_paths_mirror_the_db_formulas():
    assert skills_repo.skill_key_of("alpha") == "alpha"
    assert skills_repo.skill_key_of("alpha", "a@x") == "alpha_a@x"
    assert skills_repo.blob_path_of("alpha") == "skills/alpha/SKILL.md"
    assert skills_repo.blob_prefix_of("alpha", "a@x") == "skills/_private/a@x/alpha/"


def test_add_grant_inserts(fake_db):
    fake_db["execute"] = lambda s, p: [_row("INSERT")]
    assert skills_repo.add_grant("alpha", "u@x", granted_by="u@x") is True


def test_add_grant_uses_skill_key(fake_db):
    captured = {}

    def behavior(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [_row("INSERT")]

    fake_db["execute"] = behavior
    skills_repo.add_grant("alpha", "u@x", owner_upn="a@x")
    assert "skill_key" in captured["sql"]
    assert "skill_name" not in captured["sql"]
    assert captured["params"][1] == "alpha_a@x"


def test_remove_grant_returns_rowcount(fake_db):
    fake_db["execute"] = lambda s, p: 1
    assert skills_repo.remove_grant("alpha", "u@x") == 1


def test_list_grants_returns_rows(fake_db):
    from datetime import datetime
    at = datetime(2025, 1, 1)
    fake_db["execute"] = lambda s, p: [_row("a@x", at, "admin@x", None)]
    grants = skills_repo.list_grants("alpha")
    assert grants[0].user_upn == "a@x"
    assert grants[0].skill_key == "alpha"
    assert grants[0].granted_by == "admin@x"


def test_local_repo_respects_visibility_enabled_and_expiry(local_repo):
    assert skills_repo.upsert_skill("private") is True
    assert skills_repo.upsert_skill("public", is_public=True) is True
    assert skills_repo.list_grants_for_user("user@x") == {"public"}

    skills_repo.add_grant("private", "user@x", expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert skills_repo.list_grants_for_user("user@x") == {"private", "public"}

    skills_repo.add_grant("private", "user@x", expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    skills_repo.upsert_skill("public", is_public=True, enabled=False)
    assert skills_repo.list_skills_for_user("user@x") == []


def test_local_repo_preserves_or_updates_is_internal(local_repo):
    skills_repo.upsert_skill("child", is_internal=True)
    assert skills_repo.get_skill("child").is_internal is True

    skills_repo.upsert_skill("child", is_public=True)
    assert skills_repo.get_skill("child").is_internal is True

    skills_repo.upsert_skill("child", is_public=True, is_internal=False)
    assert skills_repo.get_skill("child").is_internal is False


def test_local_repo_delete_cascades_grants(local_repo):
    skills_repo.upsert_skill("child")
    skills_repo.add_grant("child", "user@x")
    skills_repo.delete_skill("child")
    assert skills_repo.get_skill("child") is None
    assert skills_repo.list_grants("child") == []