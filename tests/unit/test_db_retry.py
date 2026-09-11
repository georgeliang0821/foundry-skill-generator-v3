"""Retry behaviour of backend.db: transient failures must retry, and the caller
must see the original driver exception -- never RuntimeError from contextlib."""
from __future__ import annotations

import pytest

from backend import db, skills_repo

LINK_FAILURE = (
    "Driver Error: Communication link failure; DDBC Error: [Microsoft]"
    "TCP Provider: An existing connection was forcibly closed by the remote host."
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self._rows: list = []

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        exc = self.connection.raise_on_execute
        if exc is not None:
            raise exc
        self._rows = list(self.connection.rows)
        self.rowcount = len(self._rows)
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self):
        self.executed: list = []
        self.rows: list = [(1,)]
        self.raise_on_execute: Exception | None = None
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture()
def connections(monkeypatch):
    """Hand out a fresh FakeConnection per reconnect; expose the whole history."""
    made: list[FakeConnection] = []

    def _new_connection():
        cn = FakeConnection()
        made.append(cn)
        return cn

    monkeypatch.setattr(db, "_new_connection", _new_connection)
    monkeypatch.setattr(db, "_thread_local", type(db._thread_local)())
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    yield made


def test_transient_failure_retries_on_a_fresh_connection(connections):
    def _op(cur):
        cur.execute("SELECT 1")
        return cur.fetchall()

    # Arm the first connection only; the retry gets a brand new one.
    first_seen: list[FakeConnection] = []

    original = db._thread_connection

    def _thread_connection():
        cn = original()
        if not first_seen:
            first_seen.append(cn)
            cn.raise_on_execute = Exception(LINK_FAILURE)
        return cn

    db._thread_connection = _thread_connection
    try:
        assert db.run(_op) == [(1,)]
    finally:
        db._thread_connection = original

    assert len(connections) == 2, "the dead connection must be dropped and rebuilt"
    assert connections[0].closed is True
    assert connections[0].rollbacks == 1
    assert connections[1].commits == 1


def test_exhausted_retries_raise_the_original_driver_error(connections):
    def _op(cur):
        cur.execute("SELECT 1")

    original = db._thread_connection

    def _thread_connection():
        cn = original()
        cn.raise_on_execute = Exception(LINK_FAILURE)
        return cn

    db._thread_connection = _thread_connection
    try:
        with pytest.raises(Exception) as excinfo:
            db.run(_op)
    finally:
        db._thread_connection = original

    assert "Communication link failure" in str(excinfo.value)
    assert "generator didn't stop" not in str(excinfo.value)
    assert not isinstance(excinfo.value, RuntimeError)
    assert len(connections) == 3


def test_non_transient_error_is_not_retried(connections):
    original = db._thread_connection

    def _thread_connection():
        cn = original()
        cn.raise_on_execute = ValueError("invalid column name 'nope'")
        return cn

    db._thread_connection = _thread_connection
    try:
        with pytest.raises(ValueError):
            db.run(lambda cur: cur.execute("SELECT nope"))
    finally:
        db._thread_connection = original

    assert len(connections) == 1


def test_cursor_is_closed_even_when_the_body_raises(connections):
    with pytest.raises(ValueError):
        with db.get_cursor() as cur:
            cur.execute("SELECT 1")
            raise ValueError("boom")

    assert connections[0].closed_cursors == 1
    assert connections[0].rollbacks == 1
    assert connections[0].commits == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda: skills_repo.upsert_skill("retry-skill"),
        lambda: skills_repo.add_grant("retry-skill", "user@example.com"),
    ],
    ids=["upsert_skill", "add_grant"],
)
def test_merge_call_sites_go_through_the_retry_path(connections, monkeypatch, call):
    monkeypatch.setenv("SGV2_SKILL_STORE", "azure")
    skills_repo.make_skills_repo()

    original = db._thread_connection
    first_seen: list[FakeConnection] = []

    def _thread_connection():
        cn = original()
        cn.rows = [("INSERT",)]
        if not first_seen:
            first_seen.append(cn)
            cn.raise_on_execute = Exception(LINK_FAILURE)
        return cn

    db._thread_connection = _thread_connection
    try:
        assert call() is True
    finally:
        db._thread_connection = original
        skills_repo.make_skills_repo()

    assert len(connections) == 2
