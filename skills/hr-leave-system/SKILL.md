---
name: "hr-leave-system"
description: >
  Queries leave balance, files leave requests, and lists leave request status
  in the enterprise HR leave system on Azure SQL, under the signed-in user's
  own identity via OBO. Use when the user asks how many leave days they have
  left, asks to file/submit a leave request, or asks about the approval status
  of their own leave requests. For submit_request this skill does NOT retrieve
  the user's calendar itself — the caller must supply that day's calendar
  events so this skill can perform the schedule-conflict check before writing.
  Capability-layer skill: deliberately hidden from list_skills. Hosts reach it
  through the leave-workflow scenario skill, which owns the host-side
  prerequisites, the choice of calendar tool, and the multi-turn choreography.
  Common triggers: 我今年還有幾天特休、還剩幾天福利假、幫我安排 X 月 X 日請假、
  幫我送假單、我的假單簽核到哪了、查我的請假紀錄。
metadata:
  author: "George Liang"
  version: "1.5"
  uses_obo: true
---

## Overview

Read and write employee leave records held in the enterprise HR system on Azure SQL, under the end user's own identity. Three operations: check remaining balance, file a leave request (with a caller-supplied schedule-conflict check), and list the user's own requests.

## When NOT to Use This Skill

- Do not use to decline, accept, or modify a calendar event. This skill never touches the calendar; those are caller-side Work IQ Calendar actions.
- Do not use to send Teams messages or email. Those are caller-side Work IQ Teams / Mail actions.
- Do not use to approve or reject someone else's leave request. Row Level Security blocks it and there is no approval path here.

## Required Inputs

- `hr_leave_json` — Serialized payload JSON provided by the caller, injected as an environment variable. Required: yes.
- `LEAVE_CONFLICT_ACK` — Injected the same way, only on an acknowledgement round. Required: no.

The payload schema and the host-side choreography that produces it both live in the `leave-workflow` scenario body. This skill validates what arrives; it does not restate the caller's contract.

Operations and their fields:

- `get_balance` — `operation`; `leave_year` optional, defaults to the current year.
- `list_requests` — `operation`; `status` optional (`pending`, `approved`, `rejected`, `cancelled`; omit for all).
- `submit_request` — `operation`, `leave_type` (`annual`, `welfare`, `sick`, `personal`), `start_date`, `end_date`, `days`, `calendar_events`; `reason` optional.

Ground Rule: Ask the user for any missing required runtime input before calling external systems. But `calendar_events` is NOT a user-supplied input — it is a caller-supplied input the host must retrieve itself. Do not ask the user to paste their calendar.

If `hr_leave_json` is missing entirely, print exactly one line:
`[NEEDS_INFO] missing=HR_LEAVE_JSON`
Then print a human-readable explanation on a separate line and `raise SystemExit(0)`.

If `operation=submit_request` and `calendar_events` is absent (as opposed to an empty array), print exactly one line:
`[NEEDS_INFO] missing=CALENDAR_EVENTS`
Then print, on separate lines, an explanation directed at the **caller**, not the user:
`submit_request requires the user's calendar for {start_date}..{end_date}. Retrieve it with the Work IQ Calendar MCP occurrences tool (not the native Meetings capability — it does not reliably expose event_id, organizer_email, or response_status), then re-invoke with the same session_id and the calendar_events array populated. An empty array is acceptable if the user genuinely has no events that day.`
Then `raise SystemExit(0)`.

> This is the safety net, not the happy path. A well-formed caller reads the `leave-workflow` body, fetches the calendar, and supplies it on the first call. The `needs_input` round trip exists so that a caller which skipped that step fails loudly and recoverably rather than silently filing a leave request over a meeting the user already accepted.

## Environment Variables

- `HR_SQL_SERVER` — Azure SQL Server hostname for the HR leave database. Required: yes.
- `HR_SQL_DATABASE` — Azure SQL Database name. Required: yes.

Missing environment variables are deployment errors and should cause a non-zero failure.

## OBO Token Scopes

- `AZURE_SQL_ACCESS_TOKEN` — OBO access token for Azure SQL with scope `https://database.windows.net/.default`. Required: yes.

Missing OBO tokens indicate a broken OBO chain and should cause a non-zero failure. Never fall back to a service principal or managed identity — the connection identity IS the user, and Row Level Security depends on it.

## Table Schema

```
hr.v_leave_balance                          -- VIEW, read-only, RLS active
├── upn                NVARCHAR(128)
├── leave_year         INT
├── leave_type         NVARCHAR(20)          -- annual | welfare | sick | personal
├── leave_type_zh      NVARCHAR(20)          -- 特別休假 | 福利假 | 病假 | 事假
├── entitled_days      DECIMAL(4,1)
├── used_days          DECIMAL(4,1)
├── pending_days       DECIMAL(4,1)          -- 已送出未簽核，已先行佔用
├── remaining_days     DECIMAL(4,1)          -- entitled - used - pending
└── expire_date        DATE           NULL

hr.leave_request                             -- RLS active (FILTER + BLOCK AFTER INSERT)
├── request_id         NVARCHAR(30)   PK     -- LR-YYYY-MM-NNNN
├── upn                NVARCHAR(128)         -- written as USER_NAME(), never from payload
├── leave_type         NVARCHAR(20)
├── start_date / end_date  DATE
├── days               DECIMAL(4,1)
├── reason             NVARCHAR(500)  NULL
├── status             NVARCHAR(20)          -- pending | approved | rejected | cancelled
├── approver_upn       NVARCHAR(128)  NULL   -- from hr.employee.manager_upn
├── submitted_at       DATETIME2
└── decided_at         DATETIME2      NULL

hr.employee            -- no RLS, used to resolve manager
hr.access_log          -- append-only audit, db_identity DEFAULT USER_NAME()
```

RLS is active. Never add a `WHERE upn = ?` ownership filter — a redundant filter hides RLS failures and makes them undiagnosable.

## Conflict Detection Contract

For `operation=submit_request`, before any write, evaluate `calendar_events` against the requested date range.

### Step 0 — Validate the ids before trusting anything in the array

Microsoft Graph event ids are long base64url-style strings (typically 120+ characters). If any `event_id` is short, semantic-looking, or matches an orchestrator search-citation token pattern (e.g. `turn1search86`), the calendar was **not actually read** — the caller assembled the array from a prose summary or a search index. In that case `response_status` is not trustworthy either, and the most likely failure is that every event silently reports `notResponded`, which would let the leave request through with zero conflicts detected.

Reject with `[NEEDS_INFO] missing=CALENDAR_EVENTS` and an explanation. Do not proceed on a partially-valid array.

### Step 1 — Classify

`response_status` is normalized by stripping whitespace and lowercasing, so both the documented enum (`notResponded`) and display-string variants (`Not Responded`) are accepted. Unrecognized values are left as-is so they fail the blocking check loudly rather than defaulting to "no conflict".

An event is a **blocking conflict** when the normalized status is `accepted`, `tentativelyaccepted`, or `organizer`, and it overlaps the requested range.
An event is **not** a conflict when the status is `declined` or `notresponded`.

### Step 2 — Gate

If one or more blocking conflicts exist AND `LEAVE_CONFLICT_ACK` is not present in the environment:

- Do NOT write. Do not open a write transaction. Do not even open the database connection.
- Print exactly one line: `[NEEDS_INFO] missing=LEAVE_CONFLICT_ACK`
- Then print a Traditional Chinese question listing each conflicting event (time, subject, organizer, response status) and asking whether to file the leave request anyway.
- Then `raise SystemExit(0)`.

If `LEAVE_CONFLICT_ACK` is present and equals `yes` (case-insensitive), proceed to write regardless of conflicts.

The conflict list is printed for the user to read, not for the caller to parse. The caller already holds the `calendar_events` array it sent one turn earlier, so this skill never echoes `event_id` or `organizer_email` back.

If `calendar_events` is an empty array, that is a valid assertion of "no events" — proceed directly to the pre-flight checks.

**This skill never decides what to do about the conflict.** It only detects it, reports it, and waits. Declining the meeting and notifying the organizer are caller-side Work IQ actions and are outside this skill's boundary.

## Pre-flight Checks (submit_request)

Run all three inside the same connection as the write. Do not split across connections — that opens a race window between the balance read and the balance decrement.

1. `start_date` not in the past → error `past_date`
2. `remaining_days` for that leave type >= `days` → error `insufficient_balance`
3. no existing `pending` or `approved` request overlapping the range → error `overlapping_request`

On any failure: do not write, report the reason, stop.

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
    so USER_NAME() returns their UPN and RLS applies automatically.
    """
    import pyodbc

    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{os.environ['HR_SQL_SERVER']},1433;"
        f"Database={os.environ['HR_SQL_DATABASE']};"
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


def submit_leave_request(conn, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Pre-flight checks and the write share one connection and one transaction.
    upn is written as USER_NAME(), never from the payload — the skill has no
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

        INSERT INTO hr.leave_request
            (request_id, upn, leave_type, start_date, end_date, days, reason,
             status, approver_upn)
        SELECT @rid, USER_NAME(), ?, ?, ?, ?, ?, 'pending', e.manager_upn
        FROM hr.employee e WHERE e.upn = USER_NAME();

        UPDATE hr.leave_balance
           SET pending_days = pending_days + ?
         WHERE upn = USER_NAME() AND leave_year = ? AND leave_type = ?;

        INSERT INTO hr.access_log (operation, detail) VALUES ('submit_leave_request', @rid);

        SELECT @rid AS request_id;
        """,
        (leave_type, start_date, end_date, days, reason, days, year, leave_type),
    )
    request_id = cursor.fetchone().request_id
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
        print("請提供 operation 及對應欄位（見 leave-workflow 正文的 payload 契約）。")
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

## Output Contract Notes

- **Stdout is the user-facing reply and nothing else.** Everything a successful run prints is shown to the user verbatim, so never print progress notes, echoes of the input payload, or machine-readable side channels. `[NEEDS_INFO]` is the only marker this skill emits.
- The caller keeps its own copy of the `calendar_events` array it sent, including `event_id` and `organizer_email`, and uses that copy to decline meetings in a later turn. Echoing those fields back would only duplicate the caller's own input, so this skill does not do it.
- `submit_request` never reports success without a `request_id`. If the write raised, the run fails non-zero; do not report a partial success.
- When a leave request is filed while blocking conflicts were acknowledged, the reply states only what this skill actually knows: that the caller-supplied calendar snapshot showed N accepted meetings at submission time. It must NOT claim those meetings are "unhandled", "still pending", or "not yet declined" — this skill cannot see the calendar and has no way to know whether they were already resolved. Any such judgement belongs to the caller, which has the full conversation context.
- The reply is always Traditional Chinese.

## Security Notes

- NEVER use a service principal or managed identity. The connection identity is the end user; RLS depends on it.
- NEVER write `upn` from the payload. It is always `USER_NAME()`, resolved by SQL Server from the delegated token.
- NEVER add ownership filters — RLS enforces this server-side, and a redundant filter would mask RLS failures.
- NEVER fabricate `calendar_events`. An absent array is a caller error, not something to guess around.
- NEVER open a database connection before the conflict gate has passed.

## Tags

hr, leave, vacation, absence, balance, approval, azure-sql, obo, rls, conflict-check
