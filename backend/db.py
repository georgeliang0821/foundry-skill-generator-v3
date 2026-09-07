"""Azure SQL connection helper using mssql-python (pure-Python, no ODBC).

mssql-python is Microsoft's official pure-Python driver that supports AAD auth
natively. It does NOT require the msodbcsql18 system package, which makes Linux
deployment much simpler (no apt-get install at startup).

Auth strategy (service principal ONLY):
  - Requires AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET
    -> Authentication=ActiveDirectoryServicePrincipal
  - There is no managed-identity / az-login fallback: if any of those three
    variables is missing, opening a connection raises RuntimeError.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import mssql_python

from .diagnostics import log_exception

_thread_local = threading.local()


def _connection_string() -> str:
    server = os.getenv("AZURE_SQL_SERVER", "").strip()
    database = os.getenv("AZURE_SQL_DATABASE", "").strip()
    if not server or not database:
        raise RuntimeError(
            "AZURE_SQL_SERVER and AZURE_SQL_DATABASE must be set for SQL access."
        )

    tenant = os.getenv("AZURE_TENANT_ID", "").strip()
    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    client_secret = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    if not (tenant and client_id and client_secret):
        raise RuntimeError(
            "SQL access requires service-principal auth: set AZURE_TENANT_ID, "
            "AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET."
        )

    return (
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;"
        f"UID={client_id};PWD={client_secret};"
        "Authentication=ActiveDirectoryServicePrincipal;"
    )


def _new_connection():
    cn = mssql_python.connect(_connection_string())
    cn.autocommit = False
    return cn


def _thread_connection():
    cn = getattr(_thread_local, "cn", None)
    if cn is None:
        cn = _new_connection()
        _thread_local.cn = cn
    return cn


def _drop_thread_connection() -> None:
    cn = getattr(_thread_local, "cn", None)
    if cn is not None:
        try:
            cn.close()
        except Exception:  # noqa: BLE001
            pass
    _thread_local.cn = None


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    transient_markers = (
        "communication link failure",
        "timeout expired",
        "transient",
        "deadlock",
        "the wait operation timed out",
        "08s01",
        "40001",
        "40613",
        "10054",
        "10060",
    )
    return any(m in msg for m in transient_markers)


@contextmanager
def get_cursor() -> Iterator[Any]:
    """Yield a cursor with retry-on-transient + connection recycling on failure."""
    last_exc: Exception | None = None
    for attempt in range(3):
        cn = _thread_connection()
        try:
            cursor = cn.cursor()
            try:
                yield cursor
                cn.commit()
                return
            finally:
                cursor.close()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            try:
                cn.rollback()
            except Exception:  # noqa: BLE001
                pass
            transient = _is_transient(exc)
            log_exception(
                "azure_sql.query.failed",
                exc,
                attempt=attempt + 1,
                transient=transient,
            )
            if not transient or attempt == 2:
                raise
            _drop_thread_connection()
            time.sleep(0.5 * (2 ** attempt))
    if last_exc:
        raise last_exc


def fetchall(sql: str, params: tuple | list = ()) -> list:
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetchone(sql: str, params: tuple | list = ()) -> Any | None:
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple | list = ()) -> int:
    with get_cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def ping() -> bool:
    try:
        row = fetchone("SELECT 1")
        return bool(row and row[0] == 1)
    except Exception as exc:  # noqa: BLE001
        log_exception("azure_sql.ping.failed", exc)
        return False
