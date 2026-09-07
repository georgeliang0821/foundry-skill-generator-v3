---
name: leave-request-orchestration
description: "Coordinate personal leave balance checks, leave-history reviews, and leave submissions; for submissions, retrieve affected calendar occurrences, handle conflict confirmation, and offer post-submission meeting follow-up only with consent. Use for 請假、假單、假期餘額、請假紀錄, leave balance, leave history, or submit leave request; not for standalone calendar viewing or unrelated meeting management."
metadata:
  author: "geoliang@microsoft.com"
  version: "1.0"
  skill_type: scenario-orchestration
  children:
    - hr-leave-requests
---

## Scenario-layer notice

This is a scenario-orchestration skill and is not executed by the backend. It instructs the host to sequence host-side calendar and meeting capabilities, then delegate HR leave work to `hr-leave-requests`.

## Request-type table

| User request | Child operation | Host pre-work required? |
| --- | --- | --- |
| Check personal leave balance / 查詢假期餘額 | `get_balance` | No; delegate directly. |
| Review personal leave history / 查詢請假紀錄 | `list_requests` | No; delegate directly. |
| Submit a leave request / 送出假單 | `submit_request` | Yes; collect the leave-request fields and retrieve leave-period calendar occurrences when the child requests `CALENDAR_EVENTS`. |

## Not applicable

- View calendar events, check free/busy, or browse meetings without an HR leave request -> use an appropriate calendar skill.
- Create, update, decline, cancel, or otherwise manage a meeting outside a successfully submitted leave request and explicit user consent -> use an appropriate meeting or calendar skill.
- Request direct execution of the internal HR capability rather than this host workflow -> route through this scenario so calendar pre-work and return branches are handled.

## Step overview table

| Step | Actor | Action | Host capability required? |
| --- | --- | --- | --- |
| 1 | Host | Identify the request type and obtain any user-supplied leave-request details needed for the selected operation. | No |
| 2 | Host | For a leave submission, assemble the delegated payload and invoke `hr-leave-requests`. | No |
| 3 | Backend | Validate the payload, validate calendar event IDs, detect conflicts, check leave rules and balance, then read or write HR leave records. | No |
| 4 | Host | Interpret the child return branch, including any required same-session retry. | No |
| 5 | Host | After successful leave submission only, ask whether to handle affected meetings and act only after explicit consent. | Yes |

## Per-step detail

### Step 1 — Identify request and obtain required request details

- Select `get_balance` for a personal leave-balance request. Include `leave_year` only when the user specifies a year; otherwise allow the child to use its default year.
- Select `list_requests` for a personal leave-history request. Include `status` only when the user requests a status filter.
- Select `submit_request` for a leave submission. Obtain `leave_type`, `start_date`, `end_date`, and `days`; include `reason` when supplied by the user.
- Do not ask the user for calendar event IDs or calendar fields. The host must obtain those itself when needed.

### Step 2 — Calendar pre-work and delegated payload

For `submit_request`, first invoke the child with the submission fields. If the child requests calendar data, use the Work IQ Calendar MCP `occurrences` capability for the requested leave period.

For every returned occurrence carried into `calendar_events`, preserve these fields exactly as obtained:

- `event_id`: the real Microsoft Graph event ID.
- `start` and `end`: the occurrence timestamps.
- `subject`: the meeting subject.
- `organizer_name`: the organizer display name.
- `response_status`: the user's response status.

**Tool-choice warning:** use Work IQ Calendar MCP `occurrences`, not a native Meetings capability. The observable failure from the wrong source is that the child reports `CALENDAR_EVENTS` missing or rejects supplied values because `event_id` is not a real Microsoft Graph event ID.

Invoke `hr-leave-requests` through `run_coding_workflow` with one `credentials` entry named `HR_LEAVE_JSON`. Its value must be the serialized JSON payload. Do not put this payload in the free-text `request` parameter.

Payload shapes by operation:

- `get_balance`: `{"operation":"get_balance"}` with optional `leave_year`.
- `list_requests`: `{"operation":"list_requests"}` with optional `status`.
- `submit_request`: `{"operation":"submit_request","leave_type":"...","start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","days":0,"calendar_events":[...]}` with optional `reason`.

For a conflict-confirmed retry, provide `LEAVE_CONFLICT_ACK` with value `yes` as the child credential/runtime input required by the delegated call, while reusing the same session.

Hard rules:

- Never fabricate, shorten, transform, or substitute calendar event IDs.
- An empty `calendar_events` array means the host checked the period and found no events. It is different from omitting `calendar_events`.
- Preserve and reuse the same `session_id` for every retry requested by the child.
- Do not ask the user for data that the host is responsible for retrieving from the calendar.

### Step 4 — Return-branch handling

Use the return-branch table below. Do not advance to meeting follow-up unless the child has successfully submitted the leave request.

### Step 5 — Post-success meeting follow-up

Only after a successful `submit_request`, summarize the submitted request and ask the user whether they want the host to handle affected meetings.

- If the user gives explicit consent, use the host's meeting capability only for the affected meetings associated with the leave period.
- If the user declines, is ambiguous, or gives no consent, make no meeting changes.
- Balance and leave-history requests never continue to this step.

## Return-branch table

| Child return status or message | Host action | Continue to follow-up steps? |
| --- | --- | --- |
| `[NEEDS_INFO] missing=HR_LEAVE_JSON` | Rebuild the delegated serialized payload under `credentials.HR_LEAVE_JSON` and retry with the same session when applicable. | No, until the child accepts the payload. |
| `[NEEDS_INFO] missing=CALENDAR_EVENTS` | Use Work IQ Calendar MCP `occurrences` for the leave period; preserve real Graph event IDs and required occurrence fields; retry with populated `calendar_events` and the same `session_id`. | No, until the child accepts the calendar data. |
| `[NEEDS_INFO] missing=LEAVE_CONFLICT_ACK` | Present the listed conflicts and ask the user whether to submit anyway. Only if the user explicitly confirms, retry with `LEAVE_CONFLICT_ACK=yes` and the same `session_id`. | No, unless the child later reports successful submission. |
| User does not explicitly approve submission after a conflict | Tell the user the leave request was not submitted. | No. |
| `past_date` | Explain that a past start date cannot be submitted and HR must handle it manually. | No. |
| `invalid_range` | Explain that the end date is earlier than the start date and request corrected dates. | No. |
| `no_entitlement` | Explain that the user has no entitlement for the requested leave type. | No. |
| `insufficient_balance` | Explain the reported remaining balance and that it is insufficient. | No. |
| `overlapping_request` | Explain that an existing leave request overlaps and identify the reported request and period. | No. |
| `no_result_row` | Explain that the system did not confirm a request number and instruct the user not to resubmit automatically. | No. |
| Any other business-rule rejection or execution failure | Explain the returned failure without claiming submission succeeded; stop the workflow. | No. |
| Successful `get_balance` result | Present the child's balance result. | No. |
| Successful `list_requests` result | Present the child's personal leave-history result. | No. |
| Successful `submit_request` result | Present the submitted request ID, period, leave type, days, status, and approver details. Then ask for explicit consent before any affected-meeting handling. | Yes, but only to the consent gate in Step 5. |
| Explicit consent to handle affected meetings after successful submission | Use host meeting capability for the affected meetings, then summarize completed actions. | Yes. |
| No consent, ambiguous consent, or refusal after successful submission | Leave all meetings unchanged and confirm that no meeting action was taken. | No. |

## Boundary

This skill governs only task ordering, delegated parameter assembly, branching, and error handling for the leave-request scenario. It cannot override the host's identity handling, credential handling, governance limits, calendar or meeting permissions, or output rules.