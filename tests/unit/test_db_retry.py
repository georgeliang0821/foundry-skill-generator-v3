"""SQL connection settings and retries preserve explicit failures."""
from __future__ import annotations

import pytest

from backend import db, skills_repo

LINK_FAILURE = (
    "Driver Error: Communication link failure; DDBC Error: [Microsoft]"
    "TCP Provider: An existing connection was forcibly closed by the remote host."
)


@pytest.fixture()
def sql_environment(monkeypatch):
    monkeypatch.setenv("AZURE_SQL_SERVER", "test.database.windows.net")
    monkeypatch.setenv("AZURE_SQL_DATABASE", "test-db")
    monkeypatch.setenv("AZURE_TENANT_ID", "test-tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("AZURE_SQL_PORT", raising=False)


@pytest.mark.parametrize(
    ("port", "expected"),
    [(None, "1433"), ("1433", "1433"), ("3342", "3342"),
     ("1", "1"), ("65535", "65535"), (" 3342 ", "3342")],
)
def test_sql_port_reaches_driver(sql_environment, monkeypatch, port, expected):
    if port is not None:
        monkeypatch.setenv("AZURE_SQL_PORT", port)
    received = []
    connection = FakeConnection()

    def connect(connection_string):
        received.append(connection_string)
        return connection

    monkeypatch.setattr(db.mssql_python, "connect", connect)
    assert db._new_connection() is connection
    assert received == [
        f"Server=tcp:test.database.windows.net,{expected};"
        "Database=test-db;"
        "Encrypt=yes;TrustServerCertificate=no;"
        "UID=test-client;PWD=test-secret;"
        "Authentication=ActiveDirectoryServicePrincipal;"
    ]
    assert connection.autocommit is False


@pytest.mark.parametrize(
    "port",
    ["", " ", "0", "-1", "65536", "1433.0", "abc", "+1433",
     "1_433", "1433;Encrypt=no", "9" * 5000, "\u0661\u0664\u0663\u0663"],
)
def test_invalid_sql_port_fails_before_connect(sql_environment, monkeypatch, port):
    monkeypatch.setenv("AZURE_SQL_PORT", port)

    def unexpected_connect(*args, **kwargs):
        pytest.fail("Invalid SQL port must not reach the driver.")

    monkeypatch.setattr(db.mssql_python, "connect", unexpected_connect)
    with pytest.raises(
        RuntimeError, match="AZURE_SQL_PORT must be an integer between 1 and 65535"
    ):
        db._new_connection()


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


def test_nested_lookup_keeps_outer_transaction_and_locks(connections):
    def outer(cursor):
        cursor.execute("SELECT id WITH (UPDLOCK, HOLDLOCK)")
        assert db.fetchone("SELECT current_creator") == (1,)
        assert connections[0].commits == 0
        cursor.execute("UPDATE notice SET status = 'accepted'")

    db.run(outer)
    assert len(connections) == 1
    assert connections[0].commits == 1
    assert connections[0].closed_cursors == 2


def test_nested_transient_failure_retries_whole_transaction(connections):
    attempts = []

    def outer(cursor):
        attempts.append(cursor.connection)
        if len(attempts) == 1:
            cursor.connection.raise_on_execute = Exception(LINK_FAILURE)
        db.fetchone("SELECT current_creator")
        cursor.execute("UPDATE notice")

    db.run(outer)
    assert len(connections) == 2
    assert connections[0].rollbacks == 1
    assert connections[0].commits == 0
    assert connections[1].commits == 1


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
