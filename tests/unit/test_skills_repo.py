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


def test_catalog_skill_names_sql_is_not_acl_filtered(fake_db) -> None:
    seen = []
    fake_db["execute"] = lambda sql, params: seen.append((sql, params)) or [("hidden-skill",), ("disabled-skill",)]
    assert skills_repo.catalog_skill_names() == {"hidden-skill", "disabled-skill"}
    assert seen == [("SELECT DISTINCT skill_name FROM dbo.skills", ())]


def test_catalog_skill_scopes_sql_preserves_private_namespace(fake_db):
    seen = []
    fake_db["execute"] = lambda sql, params: seen.append((sql, params)) or [("same-name", None), ("same-name", "other@x")]
    assert skills_repo.catalog_skill_scopes() == {("same-name", None), ("same-name", "other@x")}
    assert seen == [("SELECT skill_name, owner_upn FROM dbo.skills", ())]


def test_catalog_skill_scopes_local_includes_disabled_private_skills(local_repo):
    local_repo.upsert_skill("global")
    local_repo.upsert_skill("private", owner_upn="other@x", enabled=False)
    assert skills_repo.catalog_skill_scopes() == {("global", None), ("private", "other@x")}


def test_database_principal_search_is_parameterized_and_preserves_names(fake_db):
    captured = {}

    def behavior(sql, params):
        captured.update(sql=sql, params=params)
        return [("EAA", "X")]

    fake_db["execute"] = behavior
    query = "E%' OR 1=1 --"
    assert skills_repo.list_database_principals(query, "X") == [{"name": "EAA", "type": "X"}]
    assert captured["params"] == ("X", "X", query)
    assert query not in captured["sql"]
    assert "TOP (100)" in captured["sql"]
    assert "type IN ('E', 'X')" in captured["sql"]
    assert skills_repo.get_database_principal("EAA") == {"name": "EAA", "type": "X"}
    assert captured["params"] == ("EAA",)


def test_permission_skills_sql_filters_creator_and_global_scope(fake_db):
    captured = {}

    def behavior(sql, params):
        captured.update(sql=sql, params=params)
        return []

    fake_db["execute"] = behavior
    assert skills_repo.list_permission_skills(" AUTHOR@X ") == []
    assert captured["params"] == ("author@x",)
    assert "s.owner_upn IS NULL" in captured["sql"]
    assert "s.created_by = ?" in captured["sql"]
    assert "s.enabled = 1" not in captured["sql"]


def test_permission_skills_local_includes_disabled_but_not_private(local_repo):
    skills_repo.upsert_skill("mine", created_by="author@x", enabled=False)
    skills_repo.upsert_skill("private", created_by="author@x", owner_upn="author@x")
    skills_repo.upsert_skill("others", created_by="other@x", is_public=True)
    assert [row.skill_name for row in skills_repo.list_permission_skills("author@x")] == ["mine"]
    assert skills_repo.list_permission_skills("") == []
    assert skills_repo.list_database_principals() == []
    assert skills_repo.get_database_principal("EAA") is None


def test_catalog_skill_names_local_includes_hidden_disabled_and_deduplicates(local_repo) -> None:
    local_repo.upsert_skill("hidden-skill")
    local_repo.upsert_skill("hidden-skill", owner_upn="other@example.com")
    local_repo.upsert_skill("disabled-skill", enabled=False)
    assert skills_repo.catalog_skill_names() == {"hidden-skill", "disabled-skill"}
    assert not local_repo.list_skills_for_user("test@example.com")


def _row(*values):
    """Return a tuple-like row."""
    return tuple(values)


def test_list_skills_for_user_returns_empty_for_blank_upn(fake_db):
    assert skills_repo.list_skills_for_user("") == []


@pytest.mark.parametrize("query_name", [
    "list_skills_for_user", "list_skills_created_by", "list_grants_for_user",
])
def test_generator_skill_lists_sql_filter_global_scope(fake_db, query_name):
    captured = {}

    def behavior(sql, params):
        captured.update(sql=sql, params=params)
        return []

    fake_db["execute"] = behavior
    getattr(skills_repo, query_name)("user@x")
    assert "s.owner_upn IS NULL" in captured["sql"]
    assert "s.enabled = 1" in captured["sql"]
    assert captured["params"] == ("user@x",)


def test_generator_skill_lists_local_exclude_granted_private_skills(local_repo):
    skills_repo.upsert_skill("mine", created_by="user@x")
    skills_repo.add_grant("mine", "user@x")
    skills_repo.upsert_skill("public", created_by="other@x", is_public=True)
    skills_repo.upsert_skill("same-name", created_by="other@x")
    for name in ("private-only", "same-name"):
        skills_repo.upsert_skill(name, owner_upn="user@x", created_by="user@x")
        skills_repo.add_grant(name, "user@x", owner_upn="user@x")

    readable = skills_repo.list_skills_for_user("user@x")
    assert {row.skill_name for row in readable} == {"mine", "public"}
    assert all(row.owner_upn is None for row in readable)
    assert [row.skill_name for row in skills_repo.list_skills_created_by("user@x")] == ["mine"]
    assert skills_repo.list_grants_for_user("user@x") == {"mine", "public"}
    assert not skills_repo.user_has_access("user@x", "same-name")
    assert skills_repo.user_has_access("user@x", "same-name", owner_upn="user@x")

    skills_repo.add_grant("same-name", "user@x")
    readable = skills_repo.list_skills_for_user("user@x")
    assert {row.skill_name for row in readable} == {"mine", "public", "same-name"}
    assert all(row.owner_upn is None for row in readable)
    assert next(row for row in readable if row.skill_name == "same-name").created_by == "other@x"


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


def test_creator_is_normalized_and_immutable(local_repo):
    skills_repo.upsert_skill("mine", created_by=" Author@X ")
    skills_repo.upsert_skill("mine", created_by="other@x", is_public=True)
    assert skills_repo.get_skill("mine").created_by == "author@x"
    skills_repo.upsert_skill("legacy")
    skills_repo.upsert_skill("legacy", created_by="author@x")
    assert skills_repo.get_skill("legacy").created_by is None


def test_creator_list_is_independent_of_grants_and_public(local_repo):
    skills_repo.upsert_skill("mine", created_by="author@x")
    skills_repo.upsert_skill("public", created_by="other@x", is_public=True)
    skills_repo.upsert_skill("granted", created_by="other@x")
    skills_repo.add_grant("granted", "author@x")
    skills_repo.upsert_skill("disabled", created_by="author@x", enabled=False)
    assert [row.skill_name for row in skills_repo.list_skills_created_by(" Author@X ")] == ["mine"]
    assert skills_repo.list_skills_created_by("") == []


def test_sql_creator_is_insert_only(fake_db):
    captured = {}

    def behavior(sql, params):
        captured.update(sql=sql, params=params)
        return [_row("INSERT")]

    fake_db["execute"] = behavior
    skills_repo.upsert_skill("mine", created_by=" Author@X ")
    update = captured["sql"].split("WHEN MATCHED THEN")[1].split("WHEN NOT MATCHED THEN")[0]
    assert "created_by" not in update
    assert "source.created_by" in captured["sql"]
    assert captured["params"][3] == "author@x"


def test_sql_creator_list_does_not_require_grants(fake_db):
    captured = {}

    def behavior(sql, params):
        captured.update(sql=sql, params=params)
        return [_row("mine", "mine", None, False, False, True, None, None, "author@x")]

    fake_db["execute"] = behavior
    assert skills_repo.list_skills_created_by("author@x")[0].created_by == "author@x"
    assert "s.created_by = ?" in captured["sql"]
    assert "s.enabled = 1" in captured["sql"]
    assert "user_skill_grants" not in captured["sql"]
    assert captured["params"] == ("author@x",)