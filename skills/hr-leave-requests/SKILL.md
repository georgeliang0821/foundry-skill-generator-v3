---
name: hr-leave-requests
description: "Use when a user wants to check their own HR leave balance, review personal leave requests, or submit an official leave request; common triggers include 請假、假期餘額、假單、leave balance, leave history, and submit leave request. This skill works with the user's Azure SQL HR leave records and formal leave workflow, not Microsoft 365 calendar viewing, free/busy checks, meeting scheduling, or event management."
metadata:
  author: admin@mngenvmcap352952.onmicrosoft.com
  tags:
    - hr
    - leave
    - azure-sql
  uses_obo: true
---

## Overview
查詢使用者本人的 HR 假期餘額與請假紀錄，並在外部行事曆檢查及必要確認完成後送出正式假單。

## When NOT to Use This Skill
需要檢視、建立、更新 Microsoft 365 行事曆事件，查詢空檔或安排會議 -> use `ms-graph-calendar`

## Required Inputs

- `HR_LEAVE_JSON`（必填）：每次請求的 JSON payload，必須包含 `operation`；可用操作為 `get_balance`、`list_requests`、`submit_request`。送出假單時還必須提供 `leave_type`、`start_date`、`end_date`、`days`，以及由外部流程取得的 `calendar_events`。
- `LEAVE_CONFLICT_ACK`（選填）：只有 `submit_request` 的外部行事曆資料顯示衝突、且使用者明確仍要送出時才提供，值必須為 `yes`。

**Ground Rule：** 呼叫任何外部系統前，必須向使用者取得所有缺少的必填 runtime input。當 caller 提供的 runtime input 缺失時，程式必須先輸出單一行 `[NEEDS_INFO] missing=VAR1,VAR2`，再於另一行說明需要什麼資料，最後以 `raise SystemExit(0)` 結束；這不是部署錯誤。

`submit_request` 的 `calendar_events` 必須先由外部流程以 Work IQ Calendar MCP occurrences 工具讀取。它必須包含實際的 Microsoft Graph event ID；若有衝突，必須先取得使用者明確確認，才可提供 `LEAVE_CONFLICT_ACK=yes` 重新送件。

## Environment Variables

以下 ACA 環境變數用於建立 HR 資料庫連線，程式以 `os.environ["NAME"]` 讀取。若缺少任一變數，代表部署設定錯誤，應以非零狀態失敗。

- `AZURE_SQL_SERVER`（必填）：Azure SQL Server 主機名稱。
- `AZURE_SQL_DATABASE`（必填）：Azure SQL HR 資料庫名稱。

## OBO Token Scopes

以下權杖由 OBO exchange 自動注入，並代表目前使用者的身分。若缺少權杖，表示 OBO 鏈中斷，應以非零狀態失敗。

- `AZURE_SQL_ACCESS_TOKEN`（必填）：Azure SQL 使用者 OBO access token，scope 為 `https://database.windows.net/.default`。

資料庫連線身分即為終端使用者；資料列權限會自動套用。不得以 payload 指定他人身分，也不得用 `USER_NAME()` 取代 `SUSER_SNAME()`。

## API Reference / Sample Code

```python
import json
import os
import struct
from datetime import date, datetime
from typing import Any


def parse_payload(raw_input: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "hr_leave_json must be valid JSON serialized into the environment variable."
        ) from exc

    operation = payload.get("operation")
    if operation not in {"get_balance", "submit_request", "list_requests"}:
        raise ValueError(
            f"operation must be one of get_balance/submit_request/list_requests, got {operation!r}"
        )

    if operation == "submit_request":
        required = ["leave_type", "start_date", "end_date", "days"]
        missing = [f for f in required if payload.get(f) in (None, "")]
        if missing:
            raise ValueError(f"submit_request missing required fields: {', '.join(missing)}")
        # Presence, not truthiness — [] is a valid and meaningful value.
        if "calendar_events" not in payload:
            print("[NEEDS_INFO] missing=CALENDAR_EVENTS")
            print(
                f"submit_request requires the user's calendar for "
                f"{payload['start_date']}..{payload['end_date']}. Retrieve it with the Work IQ "
                f"Calendar MCP occurrences tool (not the native Meetings capability), then "
                f"re-invoke with the same session_id and the calendar_events array populated. "
                f"An empty array is acceptable if the user genuinely has no events that day."
            )
            raise SystemExit(0)
        if not isinstance(payload["calendar_events"], list):
            raise ValueError("calendar_events must be an array.")

    return payload


def get_hr_connection(obo_token: str):
    """
    pyodbc connection using the user's OBO token packed for
    SQL_COPT_SS_ACCESS_TOKEN (1256). The connection identity IS the end user,
    so SUSER_SNAME() returns their UPN and RLS applies automatically.

    Do NOT use USER_NAME() to identify the user. USER_NAME() returns the
    database principal name, which is 'dbo' when the Entra account is mapped
    to db_owner, and the group name when the contained user was created for an
    Entra group. hr.fn_rls_self matches on SUSER_SNAME(), so all code here must
    use SUSER_SNAME() to stay consistent with the RLS predicate.
    """
    import pyodbc

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{os.environ['AZURE_SQL_SERVER']},1433;"
        f"Database={os.environ['AZURE_SQL_DATABASE']};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )
    token_bytes = obo_token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    return pyodbc.connect(conn_str, attrs_before={1256: token_struct})


BLOCKING_STATUSES = {"accepted", "tentativelyaccepted", "organizer"}

# Graph event ids are long base64url-ish strings (typically 120+ chars).
# Short or semantic-looking ids indicate the caller fabricated them or pulled
# them from a search-index citation token (e.g. "turn1search86") rather than
# from a real calendar read. Reject rather than silently treat as valid.
EVENT_ID_MIN_LEN = 60
EVENT_ID_CHARSET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=+/"
)


def validate_event_ids(calendar_events: list[dict[str, Any]]) -> list[str]:
    """Return a list of human-readable problems; empty means the ids look real."""
    problems = []
    for i, ev in enumerate(calendar_events):
        eid = str(ev.get("event_id") or "")
        if not eid:
            problems.append(f"calendar_events[{i}] has no event_id")
        elif len(eid) < EVENT_ID_MIN_LEN or not set(eid) <= EVENT_ID_CHARSET:
            problems.append(
                f"calendar_events[{i}].event_id={eid!r} is not a Microsoft Graph event id"
            )
    return problems


def normalize_response_status(raw: Any) -> str:
    """
    Accept the documented enum plus common display-string variants
    ('Not Responded', 'Tentatively Accepted'). Anything unrecognized is
    returned as-is so it will fail the blocking check loudly rather than
    being silently treated as 'no conflict'.
    """
    return "".join(str(raw or "").split()).lower()


def detect_conflicts(
    calendar_events: list[dict[str, Any]], start_date: str, end_date: str
) -> list[dict[str, Any]]:
    """
    Return blocking conflicts only. Declined and notResponded events are not
    conflicts. Overlap is evaluated on the date portion of start/end.
    """
    lo = date.fromisoformat(start_date)
    hi = date.fromisoformat(end_date)
    conflicts = []

    for ev in calendar_events:
        if normalize_response_status(ev.get("response_status")) not in BLOCKING_STATUSES:
            continue
        ev_start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).date()
        ev_end = datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).date()
        if ev_start <= hi and ev_end >= lo:
            conflicts.append(ev)

    return conflicts


def get_leave_balance(conn, leave_year: int) -> list[dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT leave_type, leave_type_zh, entitled_days, used_days,
               pending_days, remaining_days, expire_date
        FROM hr.v_leave_balance
        WHERE leave_year = ?
        ORDER BY CASE leave_type
                     WHEN 'annual'  THEN 1
                     WHEN 'welfare' THEN 2
                     WHEN 'sick'    THEN 3
                     ELSE 9
                 END
        """,
        (leave_year,),
    )
    return [
        {
            "leave_type": r.leave_type,
            "leave_type_zh": r.leave_type_zh,
            "entitled_days": float(r.entitled_days),
            "used_days": float(r.used_days),
            "pending_days": float(r.pending_days),
            "remaining_days": float(r.remaining_days),
            "expire_date": str(r.expire_date) if r.expire_date else None,
        }
        for r in cursor.fetchall()
    ]


def _safe_fetch_first_result(cursor):
    """
    Advance past empty/non-data result sets (e.g. 'rows affected' messages
    from DML statements ahead of a final SELECT) before fetching a row.

    cursor.description is None whenever the current result set has no
    columns — i.e. it isn't a data result set. cursor.nextset() moves to the
    next result set and returns False when there are no more. This must be
    used instead of a bare fetchone() after any multi-statement batch that
    mixes DML with a trailing SELECT.
    """
    while cursor.description is None:
        if not cursor.nextset():
            return None
    return cursor.fetchone()


def submit_leave_request(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Pre-flight checks and the write share one connection and one transaction.
    upn is written as SUSER_SNAME(), never from the payload — the skill has no
    ability to file leave on someone else's behalf even if asked to.
    """
    leave_type = payload["leave_type"]
    start_date = payload["start_date"]
    end_date = payload["end_date"]
    days = float(payload["days"])
    reason = payload.get("reason")
    year = date.fromisoformat(start_date).year

    if date.fromisoformat(start_date) < date.today():
        return {"ok": False, "error": "past_date"}
    if date.fromisoformat(end_date) < date.fromisoformat(start_date):
        return {"ok": False, "error": "invalid_range"}

    conn.autocommit = False
    cursor = conn.cursor()

    cursor.execute(
        "SELECT remaining_days FROM hr.v_leave_balance WHERE leave_year = ? AND leave_type = ?",
        (year, leave_type),
    )
    row = cursor.fetchone()
    if row is None:
        conn.rollback()
        return {"ok": False, "error": "no_entitlement"}
    if float(row.remaining_days) < days:
        conn.rollback()
        return {
            "ok": False,
            "error": "insufficient_balance",
            "remaining_days": float(row.remaining_days),
        }

    cursor.execute(
        """
        SELECT request_id, start_date, end_date FROM hr.leave_request
        WHERE status IN ('pending','approved')
          AND start_date <= ? AND end_date >= ?
        """,
        (end_date, start_date),
    )
    clash = cursor.fetchone()
    if clash is not None:
        conn.rollback()
        return {
            "ok": False,
            "error": "overlapping_request",
            "request_id": clash.request_id,
            "start_date": str(clash.start_date),
            "end_date": str(clash.end_date),
        }

    cursor.execute(
        """
        DECLARE @rid NVARCHAR(30) =
            'LR-' + FORMAT(SYSDATETIME(),'yyyy-MM') + '-' +
            RIGHT('0000' + CAST(NEXT VALUE FOR hr.seq_leave_request AS NVARCHAR(10)), 4);
        SET NOCOUNT ON;

        INSERT INTO hr.leave_request
            (request_id, upn, leave_type, start_date, end_date, days, reason,
             status, approver_upn)
        SELECT @rid, SUSER_SNAME(), ?, ?, ?, ?, ?, 'pending', e.manager_upn
        FROM hr.employee e WHERE e.upn = SUSER_SNAME();

        -- INSERT ... SELECT inserts zero rows and raises nothing when the
        -- employee lookup misses, while @rid below is returned regardless.
        -- Without this guard the caller commits an empty transaction, believes
        -- it has a request_id, and only fails later on the re-read.
        IF @@ROWCOUNT <> 1
            THROW 51001, 'employee not found in hr.employee', 1;

        UPDATE hr.leave_balance
           SET pending_days = pending_days + ?
         WHERE upn = SUSER_SNAME() AND leave_year = ? AND leave_type = ?;

        INSERT INTO hr.access_log (operation, detail) VALUES ('submit_leave_request', @rid);

        SELECT @rid AS request_id;
        """,
        (leave_type, start_date, end_date, days, reason, days, year, leave_type),
    )
    # Multi-statement batch: DML precedes the final SELECT, so fetch
    # defensively rather than assuming the first result set holds the row.
    result_row = _safe_fetch_first_result(cursor)
    if result_row is None:
        conn.rollback()
        return {"ok": False, "error": "no_result_row"}
    request_id = result_row.request_id
    conn.commit()

    cursor.execute(
        """
        SELECT r.request_id, r.status, r.approver_upn, e.display_name AS approver_name
        FROM hr.leave_request r
        LEFT JOIN hr.employee e ON e.upn = r.approver_upn
        WHERE r.request_id = ?
        """,
        (request_id,),
    )
    d = cursor.fetchone()
    return {
        "ok": True,
        "request_id": d.request_id,
        "status": d.status,
        "approver_upn": d.approver_upn,
        "approver_name": d.approver_name,
    }


def list_leave_requests(conn, status: str | None = None) -> list[dict[str, Any]]:
    cursor = conn.cursor()
    sql = """
        SELECT r.request_id, r.upn, r.leave_type, r.start_date, r.end_date,
               r.days, r.status, r.submitted_at, e.display_name AS approver_name
        FROM hr.leave_request r
        LEFT JOIN hr.employee e ON e.upn = r.approver_upn
    """
    params: list[Any] = []
    if status:
        sql += " WHERE r.status = ?"
        params.append(status)
    sql += " ORDER BY r.submitted_at DESC"

    cursor.execute(sql, params)
    return [
        {
            "request_id": r.request_id,
            "upn": r.upn,
            "leave_type": r.leave_type,
            "start_date": str(r.start_date),
            "end_date": str(r.end_date),
            "days": float(r.days),
            "status": r.status,
            "approver_name": r.approver_name,
            "submitted_at": str(r.submitted_at),
        }
        for r in cursor.fetchall()
    ]


def main() -> None:
    raw = os.environ.get("hr_leave_json") or os.environ.get("HR_LEAVE_JSON")
    if not raw:
        print("[NEEDS_INFO] missing=HR_LEAVE_JSON")
        print("請提供 operation 及對應欄位（見 hr-leave-requests 的 payload 契約）。")
        raise SystemExit(0)

    payload = parse_payload(raw)
    operation = payload["operation"]
    obo_token = os.environ["AZURE_SQL_ACCESS_TOKEN"]

    STATUS_ZH = {
        "pending": "待簽核",
        "approved": "已核准",
        "rejected": "已駁回",
        "cancelled": "已取消",
    }

    # ---- Conflict gate: runs BEFORE any database connection is opened ----
    conflicts: list[dict[str, Any]] = []
    if operation == "submit_request":
        events = payload["calendar_events"]

        # Reject fabricated / search-citation ids before trusting anything else
        # in this array. A bad id means the calendar was not actually read, and
        # response_status is therefore not trustworthy either.
        id_problems = validate_event_ids(events)
        if id_problems:
            print("[NEEDS_INFO] missing=CALENDAR_EVENTS")
            print(
                "calendar_events was supplied but the event_id values are not real "
                "Microsoft Graph event ids, which means the calendar was not actually "
                "read and response_status cannot be trusted either. Re-read the calendar "
                "with the Work IQ Calendar MCP occurrences tool and re-invoke with the "
                "same session_id."
            )
            for p in id_problems:
                print(f"  - {p}")
            raise SystemExit(0)

        conflicts = detect_conflicts(
            events, payload["start_date"], payload["end_date"]
        )
        ack = (os.environ.get("LEAVE_CONFLICT_ACK") or "").strip().lower()

        if conflicts and ack != "yes":
            print("[NEEDS_INFO] missing=LEAVE_CONFLICT_ACK")
            lines = [f"送出假單前發現 {payload['start_date']} 有 {len(conflicts)} 場行程衝突："]
            for c in conflicts:
                lines.append(
                    f"・{c['start'][11:16]}–{c['end'][11:16]} 「{c['subject']}」"
                    f"｜主辦人：{c['organizer_name']}｜您的回覆：{c['response_status']}"
                )
            lines.append("")
            lines.append("要照樣送出假單嗎？")
            print("\n".join(lines))
            raise SystemExit(0)

    conn = get_hr_connection(obo_token)
    try:
        if operation == "get_balance":
            year = int(payload.get("leave_year") or date.today().year)
            balances = get_leave_balance(conn, year)
            if not balances:
                final = f"查不到您 {year} 年度的假期額度資料，請與 HR 確認。"
            else:
                lines = [f"您 {year} 年度的假期餘額："]
                for b in balances:
                    line = f"・{b['leave_type_zh']}：{b['remaining_days']:g} 天"
                    if b["pending_days"] > 0:
                        line += f"（另有 {b['pending_days']:g} 天已送出待簽核）"
                    if b["expire_date"]:
                        line += f"，{b['expire_date']} 到期"
                    lines.append(line)
                final = "\n".join(lines)

        elif operation == "submit_request":
            result = submit_leave_request(conn, payload)
            if result["ok"]:
                final = (
                    f"假單已送出。\n"
                    f"單號：{result['request_id']}\n"
                    f"期間：{payload['start_date']}~{payload['end_date']}"
                    f"｜{payload['leave_type']} {float(payload['days']):g} 天\n"
                    f"狀態：{STATUS_ZH.get(result['status'], result['status'])}"
                    f"（{result['approver_name']}）"
                )
                if conflicts:
                    subj_list = "、".join(c["subject"] for c in conflicts)
                    final += (
                        f"\n\n備註：本次送出時，caller 提供的行事曆資料顯示當天有 "
                        f"{len(conflicts)} 場需留意的會議（{subj_list}）。"
                    )
            else:
                ERR_ZH = {
                    "past_date": "起始日期已過，無法補送假單，請洽 HR 人工處理。",
                    "invalid_range": "結束日期早於起始日期，請確認日期。",
                    "no_entitlement": "您今年沒有此假別的額度。",
                    "insufficient_balance": (
                        f"剩餘 {result.get('remaining_days')} 天，"
                        f"不足以請 {float(payload['days']):g} 天。"
                    ),
                    "overlapping_request": (
                        f"{result.get('start_date')}~{result.get('end_date')} 已有假單 "
                        f"{result.get('request_id')}，請先取消再重送。"
                    ),
                    "no_result_row": (
                        "系統寫入後未取得單號，請聯繫管理員確認假單是否成功建立，"
                        "切勿重複送出以免重複佔用假別額度。"
                    ),
                }
                final = "假單未送出：" + ERR_ZH.get(result["error"], result["error"])

        else:  # list_requests
            reqs = list_leave_requests(conn, payload.get("status"))
            if not reqs:
                final = "查不到您的請假紀錄。"
            else:
                lines = ["您的請假紀錄："]
                for r in reqs:
                    lines.append(
                        f"・{r['request_id']}｜{r['start_date']}~{r['end_date']}"
                        f"｜{r['days']:g} 天｜{STATUS_ZH.get(r['status'], r['status'])}"
                    )
                lines.append("")
                lines.append(
                    "以上僅為您本人的紀錄。資料庫的資料列權限限制我只能讀取您自己的資料，"
                    "無法代您查詢其他同事。"
                )
                final = "\n".join(lines)

        print(final)
    finally:
        conn.close()


if __name__ == "__main__":
    # HR_LEAVE_JSON 與 LEAVE_CONFLICT_ACK 皆由 run_coding_workflow 的
    # credentials 參數注入為環境變數，main() 內部直接讀取。
    main()
```