---
name: hr-leave-request-v2
description: "查詢員工本人的 HR 假期餘額與請假紀錄，或在行事曆衝突已檢查並取得必要確認後提交本人假單時使用。適用於「剩餘假期」、「請假紀錄」、「申請病假／年假」等中文或 English leave balance, leave history, submit leave request 意圖；不適用於將已擷取的 Teams 會議 follow-up action items 批次寫入 Azure SQL。"
metadata:
  author: "George Liang"
  version: "1.0"
  tags:
    - "hr"
    - "leave"
    - "azure-sql"
    - "calendar-conflict"
  uses_obo: true
---

## Overview
查詢目前登入員工本人的假期餘額與請假紀錄，並在完成行事曆衝突確認後提交本人請假申請。

## When NOT to Use This Skill
將已從 Teams 會議擷取、整理完成的 follow-up action items 批次寫入 Azure SQL -> use `teams-meeting-followup-to-azure-sql`.

## Required Inputs
- `HR_LEAVE_JSON` — 必填。透過 `credentials` 注入的序列化 JSON 請求；呼叫端應將此 JSON 字串放在 `credentials` 的 `hr_leave_json` 或 `HR_LEAVE_JSON` 鍵。必須包含 `operation`，可用值為：
  - `get_balance`：可選 `leave_year`（整數年度）；未提供時使用目前年度。
  - `list_requests`：可選 `status`（字串），用於只列出該狀態的本人請假紀錄。
  - `submit_request`：必填 `leave_type`（字串）、`start_date`（`YYYY-MM-DD`）、`end_date`（`YYYY-MM-DD`）、`days`（數字）；可選 `reason`（字串）；必填 `calendar_events`（陣列）。每個 `calendar_events` 項目必須包含 `event_id`（真實 Microsoft Graph event ID）、`response_status`、`start`（ISO 8601 日期時間）、`end`（ISO 8601 日期時間）、`subject`、`organizer_name`。提交申請前，呼叫端必須以 Work IQ Calendar MCP occurrences 工具讀取使用者行事曆；空陣列表示確實沒有行程，屬有效值。
- `LEAVE_CONFLICT_ACK` — 條件式必填。只有 `submit_request` 發現 accepted、tentatively accepted 或 organizer 狀態的重疊行程時才需要；取得使用者明確同意仍要送出後，透過 `credentials` 的 `LEAVE_CONFLICT_ACK` 鍵提供值 `yes`。

Ground Rule：任何必填 runtime input、operation 必填欄位或陣列項目欄位不足時，必須先向使用者補問，未補齊前不得呼叫外部系統。

## `[NEEDS_INFO]` 契約
- `HR_LEAVE_JSON` — 呼叫端未提供 JSON、JSON 無法解析、缺少 `operation`、`operation` 不是支援值，或 operation 所需欄位／型別不完整時使用。呼叫端必須提供可解析的請求 JSON，並補齊對應 operation 的欄位。
- `CALENDAR_EVENTS` — `submit_request` 未提供 `calendar_events`、其值不是陣列、陣列項目缺少必要欄位，或 `event_id` 不像真實 Microsoft Graph event ID 時使用。呼叫端必須使用 Work IQ Calendar MCP occurrences 工具重新讀取指定日期範圍內的行事曆，並以相同 `session_id` 重送請求。
- `LEAVE_CONFLICT_ACK` — `submit_request` 偵測到阻擋性會議衝突而尚未取得明確同意時使用。呼叫端必須呈現衝突內容、詢問使用者是否仍要送出；只有使用者明確同意後，才以 `yes` 重送。

每個需要補件的路徑都必須先輸出唯一一行 `[NEEDS_INFO] missing=CODE`，下一行輸出人類可讀說明，然後以 `raise SystemExit(0)` 結束。

## Environment Variables
- `AZURE_SQL_SERVER` — 必填。HR 請假資料庫所在的 Azure SQL Server hostname。程式以 `os.environ["AZURE_SQL_SERVER"]` 讀取；缺少代表部署設定錯誤，應非零結束。
- `AZURE_SQL_DATABASE` — 必填。包含 HR 假期餘額與請假申請資料的 Azure SQL Database 名稱。程式以 `os.environ["AZURE_SQL_DATABASE"]` 讀取；缺少代表部署設定錯誤，應非零結束。

## OBO Token Scopes
- `AZURE_SQL_ACCESS_TOKEN` — 必填。由 OBO exchange 注入、供 Azure SQL 使用的 delegated access token，scope 為 `https://database.windows.net/.default`。SQL Server 透過此權杖的 `SUSER_SNAME()` 與 row-level security 判定目前使用者；缺少代表 OBO chain 損壞，應非零結束。

## API Reference / Sample Code
```python
import json
import os
import struct
from datetime import date, datetime
from typing import Any


BLOCKING_STATUSES = {"accepted", "tentativelyaccepted", "organizer"}
EVENT_ID_MIN_LEN = 60
EVENT_ID_CHARSET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=+/"
)


def needs_info(code: str, explanation: str) -> None:
    print(f"[NEEDS_INFO] missing={code}")
    print(explanation)
    raise SystemExit(0)


def parse_payload(raw_input: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError:
        needs_info(
            "HR_LEAVE_JSON",
            "HR_LEAVE_JSON 必須是可解析的 JSON，且包含支援的 operation 與對應欄位。",
        )

    if not isinstance(payload, dict):
        needs_info("HR_LEAVE_JSON", "HR_LEAVE_JSON 必須是 JSON 物件。")

    operation = payload.get("operation")
    if operation not in {"get_balance", "submit_request", "list_requests"}:
        needs_info(
            "HR_LEAVE_JSON",
            "operation 必須是 get_balance、submit_request 或 list_requests。",
        )

    if operation == "submit_request":
        required = ["leave_type", "start_date", "end_date", "days"]
        missing = [f for f in required if payload.get(f) in (None, "")]
        if missing:
            needs_info(
                "HR_LEAVE_JSON",
                "submit_request 缺少必填欄位：" + ", ".join(missing) + "。",
            )
        # Presence, not truthiness — [] is a valid and meaningful value.
        if "calendar_events" not in payload:
            needs_info(
                "CALENDAR_EVENTS",
                f"submit_request 需要 {payload['start_date']}..{payload['end_date']} 的使用者行事曆。"
                "請以 Work IQ Calendar MCP occurrences 工具取得資料，再以相同 session_id 重送，"
                "並填入 calendar_events 陣列；若當日確實沒有行程，空陣列可接受。",
            )
        if not isinstance(payload["calendar_events"], list):
            needs_info("CALENDAR_EVENTS", "calendar_events 必須是陣列。")
        for index, event in enumerate(payload["calendar_events"]):
            if not isinstance(event, dict):
                needs_info("CALENDAR_EVENTS", f"calendar_events[{index}] 必須是物件。")
            required_event_fields = [
                "event_id", "response_status", "start", "end", "subject", "organizer_name"
            ]
            missing_event_fields = [
                field for field in required_event_fields if event.get(field) in (None, "")
            ]
            if missing_event_fields:
                needs_info(
                    "CALENDAR_EVENTS",
                    f"calendar_events[{index}] 缺少欄位：{', '.join(missing_event_fields)}。",
                )

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

    try:
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
    except Exception as exc:
        conn.rollback()
        if "employee not found in hr.employee" in str(exc):
            return {"ok": False, "error": "employee_not_found"}
        raise
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
        needs_info("HR_LEAVE_JSON", "請提供 operation 及對應欄位（見本技能的 Required Inputs）。")

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
        # in this array. A bad id means the calendar was not actually read.
        id_problems = validate_event_ids(events)
        if id_problems:
            needs_info(
                "CALENDAR_EVENTS",
                "calendar_events 的 event_id 不是有效的 Microsoft Graph event ID。"
                "請以 Work IQ Calendar MCP occurrences 工具重新讀取行事曆，並以相同 session_id 重送。",
            )

        conflicts = detect_conflicts(events, payload["start_date"], payload["end_date"])
        ack = (os.environ.get("LEAVE_CONFLICT_ACK") or "").strip().lower()

        if conflicts and ack != "yes":
            lines = [f"送出假單前發現 {payload['start_date']} 有 {len(conflicts)} 場行程衝突："]
            for c in conflicts:
                lines.append(
                    f"・{c['start'][11:16]}–{c['end'][11:16]} 「{c['subject']}」"
                    f"｜主辦人：{c['organizer_name']}｜您的回覆：{c['response_status']}"
                )
            lines.append("")
            lines.append("要照樣送出假單嗎？")
            needs_info("LEAVE_CONFLICT_ACK", "\n".join(lines))

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
                    "employee_not_found": "找不到您的員工資料，請洽 HR 確認人事資料是否已建立。",
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
    # hr_leave_json 與 LEAVE_CONFLICT_ACK 皆由 run_coding_workflow 的
    # credentials 參數注入為環境變數，main() 內部直接讀取。
    main()
```