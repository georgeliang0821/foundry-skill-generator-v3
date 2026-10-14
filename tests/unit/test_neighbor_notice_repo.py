from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import neighbor_notice_repo as repo


@pytest.fixture(autouse=True)
def local_repo(monkeypatch):
    monkeypatch.setattr(repo, "_local_repo", repo.LocalNeighborNoticeRepo())


def test_merge_concurrent_sources_and_recipient_isolation():
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda number: repo.upsert_open_notice("target", "OWNER@example.com", "AUTHOR@example.com", [f"source-{number}"]), range(20)))
    notices = repo.list_open_notices("owner@example.com")
    assert len(notices) == 1
    assert repo.count_open_notices("OWNER@example.com") == 1
    assert repo.count_open_notices("author@example.com") == 0
    assert len(notices[0].source_skill_keys) == 20
    assert repo.list_open_notices("author@example.com") == []
    assert repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["source-0"]).id == notices[0].id


def test_accept_is_idempotent_and_new_relationship_can_reopen():
    notice = repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["source"])
    calls = []

    def create_session(row):
        calls.append(row.id)
        return "session-id"

    assert repo.resolve_notice(notice.id, "owner@example.com", "accepted", create_session).modify_session_id == "session-id"
    assert repo.resolve_notice(notice.id, "owner@example.com", "accepted", create_session).modify_session_id == "session-id"
    assert calls == [notice.id]
    assert repo.list_open_notices("owner@example.com") == []
    assert repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["next-source"]).id != notice.id


def test_creation_failure_keeps_open_and_actions_require_recipient():
    notice = repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["source"])

    def fail(row):
        raise RuntimeError("Persistence unavailable")

    with pytest.raises(RuntimeError):
        repo.resolve_notice(notice.id, "owner@example.com", "accepted", fail)
    assert len(repo.list_open_notices("owner@example.com")) == 1
    with pytest.raises(LookupError):
        repo.resolve_notice(notice.id, "other@example.com", "dismissed")
    repo.resolve_notice(notice.id, "owner@example.com", "dismissed")
    with pytest.raises(ValueError):
        repo.resolve_notice(notice.id, "owner@example.com", "accepted", fail)


def test_concurrent_processing_creates_only_one_session():
    notice = repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["source"])
    calls = []

    def create(row):
        calls.append(row.id)
        return "session"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda number: repo.resolve_notice(notice.id, "owner@example.com", "accepted", create), range(20)))
    assert calls == [notice.id]
    assert all(result.modify_session_id == "session" for result in results)


class ScriptedCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.executed = []

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return next(self.responses)


@pytest.mark.parametrize("existing", [False, True])
def test_sql_upsert_locks_and_merges_json_in_one_transaction(monkeypatch, existing):
    monkeypatch.setattr(repo, "_local_repo", None)
    old = (7, "target", "owner@example.com", "author@example.com", '["source-a"]', "open", None)
    cursor = ScriptedCursor([old] if existing else [None, (7,)])
    monkeypatch.setattr(repo.db, "run", lambda operation: operation(cursor))
    notice = repo.upsert_open_notice("target", "owner@example.com", "author@example.com", ["source-b"])
    assert "UPDLOCK, HOLDLOCK" in cursor.executed[0][0]
    assert len(cursor.executed) == 2
    assert notice.source_skill_keys == (["source-a", "source-b"] if existing else ["source-b"])
    assert "UPDATE" in cursor.executed[1][0] if existing else "INSERT" in cursor.executed[1][0]


def test_sql_failed_session_creation_never_closes_notice(monkeypatch):
    monkeypatch.setattr(repo, "_local_repo", None)
    row = (7, "target", "owner@example.com", "author@example.com", '["source"]', "open", None)
    cursor = ScriptedCursor([row])
    monkeypatch.setattr(repo.db, "run", lambda operation: operation(cursor))

    def fail(notice):
        raise RuntimeError("Could not persist")

    with pytest.raises(RuntimeError):
        repo.resolve_notice(7, "owner@example.com", "accepted", fail)
    assert len(cursor.executed) == 1
    assert "target_user = ?" in cursor.executed[0][0]


def test_sql_count_uses_open_recipient_index_without_loading_rows(monkeypatch):
    monkeypatch.setattr(repo, "_local_repo", None)
    captured = {}

    def fetchone(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return (3,)

    monkeypatch.setattr(repo.db, "fetchone", fetchone)
    assert repo.count_open_notices("OWNER@example.com") == 3
    assert "COUNT_BIG(*)" in captured["sql"]
    assert "target_user = ? AND status = 'open'" in captured["sql"]
    assert captured["params"] == ("owner@example.com",)