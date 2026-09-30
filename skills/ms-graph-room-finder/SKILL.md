--- 
name: ms-graph-room-finder
description: Discover Microsoft 365 conference rooms and room lists from Microsoft Graph Places endpoints, then filter candidates by building, capacity, equipment, and availability-related constraints before handing results to a booking skill. Use this skill when a user wants to find suitable rooms or explore room lists, including bilingual prompts such as "find a room in CLS" or "找 CLS 大樓的會議室", but not when they want to reserve or change a booking.
metadata:
  author: owner
  version: "3.0"
  tags:
    - microsoft-graph
    - rooms
    - places
    - office365
  uses_obo: true
---

## When to Use This Skill

Use this skill when the user wants to discover Microsoft 365 rooms or room lists and narrow candidates before booking.

Typical requests include:
- Find conference rooms in CLS.
- Show rooms in CLS that fit 8 people.
- Find a room in CLS with a Teams device and whiteboard.
- Which rooms are available tomorrow afternoon in CLS?
- List room lists and rooms in our building.
- 找 CLS 的會議室。
- 找可容納 8 人的會議室。
- 找明天下午有空的會議室。

## When NOT to Use This Skill

Do not use this skill when the user wants to reserve, create, update, move, or cancel a booking. Use an adjacent calendar or booking skill instead, such as ms-graph-calendar, for requests like:
- Book a room in CLS for 3 PM.
- Reserve a conference room for tomorrow.
- Cancel my room booking.
- 幫我預約會議室。
- 取消我的會議室預約。

Do not use this skill for non-room Microsoft Graph tasks such as mail, chat, files, or general people lookup.

## How to Run

This skill is script-based. Always execute its script with `run_skill_script`; do not write your own code to call Microsoft Graph for room discovery. Pass the arguments from **Required Inputs** as a list of strings, e.g. `["--building", "CLS", "--capacity", "8"]`.

## Required Inputs

All arguments are optional; pass only the filters the user actually gave.

| Argument | Value | Meaning | Default |
|---|---|---|---|
| `--building` | building code or city | Location: matched case-insensitively against the room's `building` code **or** its address city (e.g. `CLS` or `Taipei`). `*` = all locations. | `DEFAULT_BUILDING` |
| `--room-list` | room list name or email | Restrict to the rooms of one room list (exact name/email, or a unique partial name). When given without `--building`, the default building is not applied. | none |
| `--capacity` | positive integer | Minimum seats. Rooms with unknown capacity are excluded. | none |
| `--features` | comma-separated keywords | Each keyword must appear (substring, case-insensitive) in the room's tags or audio/video/display device names. | none |
| `--start` | local ISO time, e.g. `2026-09-28T14:00` | Start of the free/busy window, without offset. Must be given together with `--end`. | none |
| `--end` | local ISO time | End of the free/busy window; must be later than `--start`. | none |
| `--timezone` | Windows time zone name | Time zone of `--start` / `--end`. | `Taipei Standard Time` |
| `--limit` | positive integer (max 50) | How many candidates to return. `omitted_candidates` tells how many more matched. | 10 |
| `--list-room-lists` | flag, no value | Only list the tenant's room lists. | off |

Building the arguments:
- Resolve relative dates ("tomorrow afternoon", "明天下午") into explicit `--start` / `--end` before calling. If the user names a time of day without a range, ask or use a reasonable window and say which one you used.
- Translate equipment wording into short keywords (`teams`, `whiteboard`, `hub`, `tv`); if a feature filter removes everything, retry once without it and tell the user.
- Pass the place the user named as `--building` as-is (building code or city).

Retry before asking the user (at most one retry per filter):
- If the location filter matched nothing, its `filter_report` entry contains `known_locations` (building codes and cities that exist). If one of them clearly corresponds to what the user said (same place, different spelling or language, e.g. 台北 → `Taipei`), run again with that value. If none corresponds, run once with `--list-room-lists` and look for a matching room list.
- Only ask the user when the retry also finds nothing; then show them the `known_locations` values to choose from.

## Reading the Result

stdout is one JSON object. The script writes no files.

| Exit code | Meaning | What to do |
|---|---|---|
| 0, `status` = `ok` | Success, possibly with zero candidates | Present `top_candidates` (or `room_lists`). If `candidate_count` is 0, apply the retry rules above first; if still empty, use `filter_report` to say which filter removed the rooms and suggest relaxing it. |
| 0, `status` = `needs_info` | An argument is missing, invalid, or ambiguous. The first stdout line is `[NEEDS_INFO] missing=<field>`, followed by the JSON object. | If you built the argument wrongly, fix it and run again once. Otherwise ask the user for the field named in `needs_info`, using `reason` (and `room_lists` when `needs_info` is `room_list`). |
| 1 | Configuration error: `GRAPH_ACCESS_TOKEN` is not set (stderr shows `KeyError`), or an unexpected script error | Report it. Do not retry and do not work around it; an administrator must configure the Graph OBO scope. |
| 3 | Graph or network failure | If `http_status` is 401 or 403, report `hint` and do not retry; an administrator must fix the token or consent. Otherwise report the failure and retry at most once. |

Output fields (`status` = `ok`):

| Field | Meaning |
|---|---|
| `status` | `ok` |
| `scope` | Building / room list actually applied, and `building_source` (`argument` or `DEFAULT_BUILDING`). Tell the user when the default building was used. |
| `filters` | `capacity` and `features` actually applied. |
| `total_rooms_scanned` | Rooms considered before filtering. |
| `candidate_count` | Rooms that passed all filters. |
| `availability` | `status` = `checked`, `unchecked` (token lacks calendar permission; the reason is in `note`) or `not_requested`, plus `start` / `end` / `timezone` when requested. |
| `top_candidates` | Up to `--limit` rooms: `name`, `email`, `building`, `city`, `floor`, `capacity`, `features`, and `availability` (`free`, `busy` or `unknown`) when checked; free rooms first. |
| `omitted_candidates` | Present when more rooms matched than were returned; if large, ask the user to narrow the filters rather than listing everything. |
| `filter_report` | Per-filter `before` / `after` counts, how many rooms had no location or capacity data, and `known_locations` when the location filter matched nothing. |
| `warnings` | E.g. room lists unavailable in this tenant; mention them briefly. |
| `booking_note` | Always tell the user that final availability must be confirmed at booking time. |
| `room_lists` | Only with `--list-room-lists`: `name` and `email` of each room list. |

Output fields (`status` = `needs_info`): `needs_info` (the missing field), `reason`, and `room_lists` when the room list could not be matched.

Output fields (exit 3): `status` = `error`, then `http_status`, `graph_code`, `message`, `hint` for Graph errors, or `error` for network / malformed-response failures.

Return a candidate room list suitable for a downstream booking skill: room name, email address, building/floor, capacity, feature hints, and availability when checked. Always state that final availability must be confirmed at booking time.

## Composability

- **Downstream dependents**: ms-graph-calendar (booking) needs the room email addresses from this skill, so booking must wait for this skill's result.
- **Independent of**: skills with no data dependency on room candidates (mail, Teams, to-do, etc.).
- The script already issues its independent Graph calls concurrently; no orchestration is needed.

## Prerequisites

- `GRAPH_ACCESS_TOKEN` — Microsoft Graph OBO token (mapped from `https://graph.microsoft.com/.default` in `OBO_SCOPE_REGISTRY`). Missing → exit 1. Required delegated permissions:
  - `Place.Read.All` (admin consent) — room and room-list discovery. Required.
  - `Calendars.ReadBasic` — free/busy check. Optional; without it the script still returns candidates with `availability.status = unchecked`.
- `DEFAULT_BUILDING` — optional ACA variable; building used when the user does not name one (initial value `CLS`).
- The tenant must have Places configured and room mailboxes carrying building and capacity data; rooms missing these fields are reported in `filter_report`.
