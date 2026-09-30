#!/usr/bin/env python3
"""Find Microsoft 365 conference rooms and filter them into booking candidates.

EAA script-mode skill (args format argv-v1). Read-only: never creates or
changes a booking.

Usage:
  ms-graph-room-finder.py [--building CODE_OR_CITY] [--room-list NAME_OR_EMAIL]
                          [--capacity N] [--features a,b,...]
                          [--start ISO --end ISO [--timezone TZ]]
                          [--limit N] [--list-room-lists]

Environment:
  GRAPH_ACCESS_TOKEN  required  Graph OBO token (Place.Read.All; Calendars.ReadBasic
                                for the optional availability check)
  DEFAULT_BUILDING    optional  building used when --building is not given
                                ("*" or empty = no building filter)

Exit codes:
  0  success (including zero matches - see filter_report), or [NEEDS_INFO]:
     stdout starts with "[NEEDS_INFO] missing=<field>" followed by one JSON object
  1  configuration error (GRAPH_ACCESS_TOKEN not set) or unexpected script error
  3  downstream failure: Graph HTTP error (incl. 401 / 403), network, malformed response

Output:
  stdout  one JSON object with the top candidates (kept small: the EAA runner
          truncates stdout). No files are written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
HTTP_TIMEOUT = 30
MAX_PAGES = 50
SCHEDULE_BATCH = 20
MAX_AVAILABILITY_ROOMS = 100
SUMMARY_ROOMS = 10
DEFAULT_TIMEZONE = "Taipei Standard Time"

EXIT_OK, EXIT_DOWNSTREAM = 0, 3


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str, url: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.url = status, code, message, url


class NeedsInfo(Exception):
    def __init__(self, missing: str, message: str):
        super().__init__(message)
        self.missing = missing


def _graph_failure(e: GraphError) -> str:
    # No "<4xx|5xx> <reason phrase>" wording: exit-0 output is scanned for HTTP error signals.
    return f"{e.code or 'Graph request failed'}, http {e.status}"


# ---------------------------------------------------------------- HTTP

def _http(method: str, url: str, token: str, body: dict | None = None,
          headers: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        code, message = "", ""
        try:
            err = json.loads(e.read() or b"{}").get("error", {})
            code, message = err.get("code", ""), err.get("message", "")
        except Exception:
            pass
        raise GraphError(e.code, code, message or e.reason or "", url) from None


def get_all(path: str, token: str) -> list[dict]:
    """GET a collection and follow @odata.nextLink."""
    url = f"{GRAPH_BASE}{path}"
    items: list[dict] = []
    for _ in range(MAX_PAGES):
        page = _http("GET", url, token)
        items.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
        if not url:
            return items
    raise GraphError(0, "TooManyPages", f"more than {MAX_PAGES} pages", path)


# ---------------------------------------------------------------- normalize

def _email(value) -> str:
    if isinstance(value, dict):
        value = value.get("address")
    return (value or "").strip()


def _int(value) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def normalize_room(room: dict) -> dict:
    features = [t for t in (room.get("tags") or []) if isinstance(t, str) and t.strip()]
    for key in ("audioDeviceName", "videoDeviceName", "displayDeviceName"):
        if room.get(key):
            features.append(str(room[key]))
    if room.get("isWheelChairAccessible"):
        features.append("wheelchair accessible")
    address = room.get("address") if isinstance(room.get("address"), dict) else {}
    return {
        "name": room.get("displayName") or "",
        "email": _email(room.get("emailAddress")),
        "building": (room.get("building") or "").strip(),
        "city": (address.get("city") or "").strip(),
        "floor": room.get("floorNumber"),
        "capacity": _int(room.get("capacity")),
        "features": features,
        "booking_type": room.get("bookingType"),
    }


def normalize_room_list(rl: dict) -> dict:
    return {"name": rl.get("displayName") or "", "email": _email(rl.get("emailAddress"))}


# ---------------------------------------------------------------- filters

def _match_room_list(query: str, room_lists: list[dict]) -> dict | None:
    q = query.strip().lower()
    for rl in room_lists:
        if q in (rl["email"].lower(), rl["name"].lower()):
            return rl
    hits = [rl for rl in room_lists if q in rl["name"].lower()]
    return hits[0] if len(hits) == 1 else None


def known_locations(rooms: list[dict], cap: int = 30) -> dict:
    """Distinct building codes and cities, so the caller can retry with a valid value."""
    return {
        "buildings": sorted({r["building"] for r in rooms if r["building"]})[:cap],
        "cities": sorted({r["city"] for r in rooms if r["city"]})[:cap],
    }


def apply_filters(rooms: list[dict], building: str | None, capacity: int | None,
                  features: list[str]) -> tuple[list[dict], list[dict]]:
    report: list[dict] = []
    current = rooms

    if building:
        # A location may be a building code ("CLS") or a city ("Taipei").
        b = building.lower()
        kept = [r for r in current if b in (r["building"].lower(), r["city"].lower())]
        no_field = sum(1 for r in current if not r["building"] and not r["city"])
        entry = {"filter": f"location={building}", "matched_on": "building or city",
                 "before": len(current), "after": len(kept),
                 "rooms_without_building_or_city": no_field}
        if not kept:
            entry["known_locations"] = known_locations(current)
        report.append(entry)
        current = kept

    if capacity is not None:
        kept = [r for r in current if r["capacity"] is not None and r["capacity"] >= capacity]
        unknown = sum(1 for r in current if r["capacity"] is None)
        report.append({"filter": f"capacity>={capacity}", "before": len(current),
                       "after": len(kept), "rooms_with_unknown_capacity": unknown})
        current = kept

    for feat in features:
        f = feat.lower()
        kept = [r for r in current if any(f in x.lower() for x in r["features"])]
        report.append({"filter": f"feature~{feat}", "before": len(current), "after": len(kept)})
        current = kept

    current.sort(key=lambda r: (r["capacity"] if r["capacity"] is not None else 10**6, r["name"]))
    return current, report


# ---------------------------------------------------------------- availability

def check_availability(rooms: list[dict], start: str, end: str, tz: str,
                       token: str) -> tuple[str, str | None]:
    """Annotate rooms with availability. Returns (status, note)."""
    targets = [r for r in rooms if r["email"]][:MAX_AVAILABILITY_ROOMS]
    for r in rooms:
        r["availability"] = "unknown"
    note = None
    if len(rooms) > len(targets):
        note = f"availability checked for the first {len(targets)} of {len(rooms)} rooms"

    by_email = {r["email"].lower(): r for r in targets}
    batches = [targets[i:i + SCHEDULE_BATCH] for i in range(0, len(targets), SCHEDULE_BATCH)]

    def one(batch):
        body = {
            "schedules": [r["email"] for r in batch],
            "startTime": {"dateTime": start, "timeZone": tz},
            "endTime": {"dateTime": end, "timeZone": tz},
            "availabilityViewInterval": 15,
        }
        return _http("POST", f"{GRAPH_BASE}/me/calendar/getSchedule", token, body,
                     {"Prefer": f'outlook.timezone="{tz}"'})

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(one, batches))
    except GraphError as e:
        if e.status in (401, 403):
            return "unchecked", ("availability not checked: token lacks Calendars.ReadBasic "
                                 "(or equivalent); confirm availability when booking")
        return "unchecked", f"availability not checked: {_graph_failure(e)}"

    for res in results:
        for info in res.get("value", []):
            room = by_email.get((info.get("scheduleId") or "").lower())
            if not room:
                continue
            view = info.get("availabilityView")
            if info.get("error") or view is None or view == "":
                room["availability"] = "unknown"
            else:
                room["availability"] = "free" if set(view) <= {"0"} else "busy"
    return "checked", note


# ---------------------------------------------------------------- main

def parse_args(argv: list[str]) -> argparse.Namespace:
    # add_help=False: --help would print usage to stdout, which must carry JSON only.
    p = argparse.ArgumentParser(prog="ms-graph-room-finder", add_help=False)
    p.add_argument("--building")
    p.add_argument("--room-list")
    p.add_argument("--capacity")
    p.add_argument("--features", default="")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    p.add_argument("--limit", default=str(SUMMARY_ROOMS))
    p.add_argument("--list-room-lists", action="store_true")

    try:
        ns = p.parse_args(argv)
    except SystemExit:
        # argparse exits 2, which EAA reports as a failure; ask for the argument instead.
        raise NeedsInfo("arguments", "unrecognized or malformed arguments") from None

    if ns.capacity is not None:
        cap = _int(ns.capacity)
        if cap is None or cap < 1:
            raise NeedsInfo("capacity", "--capacity must be a positive integer")
        ns.capacity = cap
    limit = _int(ns.limit)
    if limit is None or limit < 1:
        raise NeedsInfo("limit", "--limit must be a positive integer")
    ns.limit = min(limit, 50)

    if bool(ns.start) != bool(ns.end):
        raise NeedsInfo("end" if ns.start else "start",
                        "--start and --end must be given together")
    if ns.start:
        try:
            s = datetime.fromisoformat(ns.start)
            e = datetime.fromisoformat(ns.end)
        except ValueError:
            raise NeedsInfo("start,end", "--start/--end must be ISO 8601 local times, "
                            "e.g. 2026-09-28T14:00") from None
        if s.tzinfo or e.tzinfo:
            raise NeedsInfo("start,end", "give local times without offset; use --timezone")
        if e <= s:
            raise NeedsInfo("end", "--end must be later than --start")
        ns.start, ns.end = s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")

    ns.features = [f.strip() for f in ns.features.split(",") if f.strip()]
    return ns


def run(ns: argparse.Namespace) -> dict:
    # Missing token = deployment error: the uncaught KeyError exits 1.
    token = os.environ["GRAPH_ACCESS_TOKEN"]

    building = ns.building if ns.building is not None else os.environ.get("DEFAULT_BUILDING", "")
    building_source = "argument" if ns.building is not None else "DEFAULT_BUILDING"
    if building.strip() in ("", "*"):
        building = None

    # Discovery: the two calls are independent -> run concurrently.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_lists = pool.submit(get_all, "/places/microsoft.graph.roomlist", token)
        f_rooms = (None if ns.list_room_lists
                   else pool.submit(get_all, "/places/microsoft.graph.room", token))
        warnings: list[str] = []
        try:
            room_lists = [normalize_room_list(x) for x in f_lists.result()]
        except GraphError as e:
            if e.status in (401, 403):
                raise
            room_lists = []
            warnings.append(f"room lists unavailable ({_graph_failure(e)}); "
                            "continuing with the tenant-wide room listing")
        all_rooms = [normalize_room(x) for x in f_rooms.result()] if f_rooms else []

    if ns.list_room_lists:
        return {"status": "ok", "room_lists": room_lists, "warnings": warnings}

    scope = {"building": building, "building_source": building_source if building else None}
    rooms = all_rooms
    if ns.room_list:
        rl = _match_room_list(ns.room_list, room_lists)
        if rl is None:
            return {
                "status": "needs_info", "needs_info": "room_list",
                "reason": f"no unique room list matches '{ns.room_list}'",
                "room_lists": room_lists}
        members = get_all(f"/places/{urllib.parse.quote(rl['email'])}"
                          "/microsoft.graph.roomlist/rooms", token)
        rooms = [normalize_room(x) for x in members]
        scope["room_list"] = rl
        if ns.building is None:  # room list replaces the default building
            building, scope["building"], scope["building_source"] = None, None, None

    candidates, report = apply_filters(rooms, building, ns.capacity, ns.features)

    availability = {"status": "not_requested"}
    if ns.start:
        status, note = check_availability(candidates, ns.start, ns.end, ns.timezone, token)
        availability = {"status": status, "start": ns.start, "end": ns.end,
                        "timezone": ns.timezone}
        if note:
            availability["note"] = note
        if status == "checked":
            order = {"free": 0, "unknown": 1, "busy": 2}
            candidates.sort(key=lambda r: order[r["availability"]])

    result = {
        "status": "ok",
        "scope": scope,
        "filters": {"capacity": ns.capacity, "features": ns.features},
        "total_rooms_scanned": len(rooms),
        "candidate_count": len(candidates),
        "availability": availability,
        "candidates": candidates,
        "filter_report": report,
        "warnings": warnings,
        "booking_note": "Discovery only. Final availability must be confirmed by the "
                        "booking skill at reservation time.",
    }
    return result


def summarize(result: dict, limit: int) -> dict:
    s = {k: v for k, v in result.items() if k not in ("candidates",)}
    if "candidates" in result:
        keep = ("name", "email", "building", "city", "floor", "capacity", "features", "availability")
        s["top_candidates"] = [{k: r[k] for k in keep if k in r}
                               for r in result["candidates"][:limit]]
        omitted = len(result["candidates"]) - len(s["top_candidates"])
        if omitted > 0:
            s["omitted_candidates"] = omitted
    return s


def main(argv: list[str]) -> int:
    limit = SUMMARY_ROOMS
    try:
        ns = parse_args(argv)
        limit = ns.limit
        result = run(ns)
    except NeedsInfo as e:
        result = {"status": "needs_info", "needs_info": e.missing, "reason": str(e)}
    except GraphError as e:
        hint = {401: "token invalid or expired",
                403: "token lacks Place.Read.All (delegated, admin consent required) "
                     "or Places is not enabled in this tenant",
                404: "endpoint or resource not found in this tenant"}.get(e.status, "")
        print(json.dumps({"status": "error", "http_status": e.status, "graph_code": e.code,
                          "message": e.message[:500], "hint": hint}, ensure_ascii=False))
        return EXIT_DOWNSTREAM
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "error", "error": f"{type(e).__name__}: {e}"[:800]},
                         ensure_ascii=False))
        return EXIT_DOWNSTREAM

    if result["status"] == "needs_info":
        print(f"[NEEDS_INFO] missing={result['needs_info']}")
    print(json.dumps(summarize(result, limit), ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
