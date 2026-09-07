"""FastAPI entrypoint for Skill Generator v2."""

from __future__ import annotations

from pydantic import BaseModel


import json
import re
from datetime import datetime, timezone
import os
import base64
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import FOUNDRY_OUTPUT_RULES, FOUNDRY_OUTPUT_SHAPE, TOOL_SCHEMAS, OrchestratorAgent
from .auth_store import make_auth_store
from .blob_store import (
    MAX_SKILL_NAME_LEN,
    CachedSkillStore,
    LocalSkillStore,
    compute_hash,
    make_skill_store,
    parse_frontmatter,
    parse_frontmatter_meta,
    replace_frontmatter_name,
    safe_skill_name,
)
from .diagnostics import elapsed_ms, env_flag, log_event, log_exception, now_ms
from .e2e import (
    FakeE2EAgent,
    e2e_enabled,
    e2e_skill_files,
    fake_agent_enabled,
    fake_run_selection_tests,
    fake_test_runner_enabled,
    get_scenario,
    set_scenario,
)
from .material_fidelity import fidelity_warning_count, scan_material_fidelity
from .skill_lint import lint_skill, lint_warning_count
from .models import (
    ApplyPatchRequest,
    ApplyPatchResponse,
    ChecklistUpdateRequest,
    ChatMessage,
    ChatRequest,
    ChildrenUpdateRequest,
    CreateSessionRequest,
    Material,
    MaterialUpsertRequest,
    MessageRole,
    Mode,
    PatchRecord,
    NeighborEdit,
    NeighborProposeRequest,
    NeighborSelectRequest,
    NeighborsUpdateRequest,
    NeighborVersion,
    RunSessionTestRequest,
    SamplesUpdateRequest,
    SCENARIO_SKILL_TYPE,
    SaveSessionSkillRequest,
    Session,
    SessionSummary,
    SkillDraft,
    SkillFiles,
    SkillKind,
    SkillTestRequest,
    Stage,
    ToolResultRequest,
    VariablesUpdateRequest,
    delegation_children,
)
from .patch import PatchError, apply_v4a_to_content, version_hash
from .session_store import LocalSessionStore, make_session_store
from .topology import Severity, TopologyIssue, validate_topology
from . import acl as acl_mod
from . import skills_repo
from . import db
from .state_machine import (
    TRANSITION_TABLE,
    QualityGateError,
    register_post_transition_hook,
    transition,
)
# ---------------------------------------------------------------------------
# v6 PREPARE-entry background tasks (skills_index overlap is fast & local;
# research and ACA env are best-effort and run in a daemon thread).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Peer-skill boundary digest (parallel load of the user's accessible skills)
# ---------------------------------------------------------------------------

def _extract_section(skill_md: str, heading_keywords: tuple[str, ...], limit: int = 500) -> str:
    """Return the body text under the first \"## <heading>\" whose title contains
    any of ``heading_keywords`` (case-insensitive), trimmed to ``limit`` chars."""
    lines = (skill_md or "").splitlines()
    capture: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip().lower()
            if capturing:
                break
            if title.startswith("#"):
                title = title.lstrip("#").strip()
            if any(kw in title for kw in heading_keywords):
                capturing = True
            continue
        if capturing:
            capture.append(line)
    text = "\n".join(capture).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit].strip()


def _blob_access_hint(error_text: str) -> str:
    """Turn a Blob data-plane failure into an actionable cause + fix, or ""."""
    text = error_text or ""
    if any(
        marker in text
        for marker in (
            "AuthorizationPermissionMismatch",
            "AuthorizationFailure",
            "not authorized",
            "403",
        )
    ):
        return (
            "Likely cause: the Blob identity has no data-plane role, or the storage "
            "firewall blocks this IP. Fix: Storage account -> IAM -> assign "
            "'Storage Blob Data Contributor' to the DefaultAzureCredential identity "
            "(Blob auth deliberately ignores AZURE_CLIENT_*), AND Networking -> allow "
            "this client IP."
        )
    if any(
        marker in text
        for marker in ("CredentialUnavailable", "ManagedIdentityCredential", "DefaultAzureCredential")
    ):
        return "Likely cause: no usable Azure credential -- run `az login` and restart the backend."
    return ""


def _peer_skill_digest(name: str, skill_md: str) -> dict[str, Any]:
    # Schema v2 dropped dbo.skills.description, so the frontmatter on Blob is
    # the only source for a peer's description.
    _fm_name, fm_desc = parse_frontmatter(skill_md or "")
    description = (fm_desc or "").strip()
    return {
        "name": name,
        "description": description[:500],
        "when_to_use": _extract_section(skill_md, ("when to use",), 500),
        "when_not_to_use": _extract_section(skill_md, ("when not to use", "do not use", "not to use"), 400),
        "granted": True,
    }


def _load_peer_skill_boundaries(session, *, max_skills: int = 30, max_workers: int = 8) -> None:
    """Best-effort, parallel load of the boundaries of every skill the user can
    access (SQL grant intersect Blob content). Mirrors the ACA env load: runs in
    the PREPARE-entry daemon thread, never raises, and populates
    ``session.prepare_brief.research.peer_skills``.
    """
    from concurrent.futures import ThreadPoolExecutor

    research = session.prepare_brief.research
    upn = getattr(session, "owner_upn", "") or ""
    if not upn:
        research.peer_skills_status = "skipped"
        research.peer_skills_error = ""
        return
    try:
        rows = skills_repo.list_skills_for_user(upn)
    except Exception as exc:  # noqa: BLE001
        log_exception("prepare_entry.peer_skills.sql_failed", exc, session_id=session.id)
        research.peer_skills_status = "failed"
        research.peer_skills_error = (
            "Azure SQL is unreachable, so the granted-skill list is unknown: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        return
    self_name = (session.remote_skill_id or session.target_skill_id or "").strip()
    # A declared child is delegated to, not routed against, so it is not a peer.
    excluded = {self_name} | {c for c in (safe_skill_name(x) for x in (session.children or [])) if c}
    names = [r.skill_name for r in rows if r.skill_name and r.skill_name not in excluded][:max_skills]
    if not names:
        research.peer_skills = []
        research.peer_skills_status = "ok"
        research.peer_skills_error = ""
        return

    failures: list[str] = []

    def _load_one(name: str) -> dict[str, Any] | None:
        try:
            files = store.load_skill(name)
            return _peer_skill_digest(name, files.skill_md)
        except Exception as exc:  # noqa: BLE001 - one missing blob must not fail the batch.
            log_event("prepare_entry.peer_skills.load_missing", level="warning", session_id=session.id, skill_name=name, error=str(exc)[:200])
            failures.append(f"`{name}` -> {type(exc).__name__}: {str(exc)[:200]}")
            # Without the blob there is no description anywhere, so drop it.
            return None

    digests: list[dict[str, Any]] = []
    started = now_ms()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(_load_one, names):
            if result is not None:
                digests.append(result)
    digests.sort(key=lambda d: d["name"])
    research.peer_skills = digests
    if failures:
        hint = _blob_access_hint(failures[0])
        research.peer_skills_status = "partial" if digests else "failed"
        research.peer_skills_error = (
            f"{len(failures)} of {len(names)} granted skill(s) could not be read from Blob. "
            f"First failure: {failures[0]}." + (f" {hint}" if hint else "")
        )
    else:
        research.peer_skills_status = "ok"
        research.peer_skills_error = ""
    log_event(
        "prepare_entry.peer_skills.done",
        level="warning" if failures else "info",
        session_id=session.id,
        requested=len(names),
        loaded=len(digests),
        status=research.peer_skills_status,
        error=research.peer_skills_error[:300],
        duration_ms=elapsed_ms(started),
    )


def _load_selected_neighbor_full_md(session) -> None:
    """Best-effort: load the FULL current SKILL.md of each SELECTED neighbor skill
    (prepare_brief.understanding.neighbor_skills) so the agent can edit them with a
    V4A patch without asking the user for the text. Stored on the transient
    session.neighbor_full_md (excluded from persistence). Never raises.
    """
    try:
        neighbors = session.prepare_brief.understanding.neighbor_skills or []
    except Exception:  # noqa: BLE001
        return
    names: list[str] = []
    for nb in neighbors:
        name = safe_skill_name(str(getattr(nb, "skill", "") or "").strip())
        if name and name not in names:
            names.append(name)
    # Prefer the edit card's currently SELECTED version so the agent sees exactly
    # the text the next V4A patch will be applied to. Otherwise the agent patches
    # against the Blob original while _apply_neighbor_edit_proposal applies the
    # patch to an already-edited version -> anchor mismatch. Neighbors not yet
    # edited fall back to the Blob original.
    edits_by_name = {ne.skill_name: ne for ne in (session.neighbor_edits or [])}
    loaded: dict[str, str] = {}
    for name in names[:5]:
        ne = edits_by_name.get(name)
        if ne and ne.versions:
            sel = next((v for v in ne.versions if v.version_id == ne.selected_version_id), None)
            md = (sel.skill_md if sel else ne.versions[-1].skill_md) or ""
            if md.strip():
                loaded[name] = md
                continue
        try:
            files = store.load_skill(name)
        except Exception as exc:  # noqa: BLE001 - one missing neighbor must not fail the batch.
            log_event(
                "neighbor_full_md.load_missing",
                level="warning",
                session_id=session.id,
                skill_name=name,
                error=str(exc)[:200],
            )
            continue
        if files.skill_md.strip():
            loaded[name] = files.skill_md
    session.neighbor_full_md = loaded
    log_event("neighbor_full_md.loaded", session_id=session.id, requested=len(names), loaded=len(loaded))


def _load_child_full_md(session) -> None:
    """Best-effort: load the FULL current SKILL.md of each declared child skill.

    Same contract as :func:`_load_selected_neighbor_full_md` -- stored on the
    transient ``session.child_full_md`` and never raises. A child that is edited
    in this session is served from its selected edit version, so the agent
    patches against the exact text the next apply will target.

    ``children`` is a whitelist of every skill the scenario may use, which is
    far wider than the set it delegates to, so the cap below is applied to a
    delegation-first ordering. Otherwise the one child whose payload contract
    actually matters could be cut by an alphabetically unlucky dependency.
    """
    delegated = delegation_children(session)
    ordered = [safe_skill_name(c) for c in delegated + list(session.children or [])]
    names = [n for n in dict.fromkeys(ordered) if n]
    edits_by_name = {ne.skill_name: ne for ne in (session.neighbor_edits or [])}
    loaded: dict[str, str] = {}
    for name in names[:5]:
        ne = edits_by_name.get(name)
        if ne and ne.versions:
            sel = next((v for v in ne.versions if v.version_id == ne.selected_version_id), None)
            md = (sel.skill_md if sel else ne.versions[-1].skill_md) or ""
            if md.strip():
                loaded[name] = md
                continue
        try:
            files = store.load_skill(name)
        except Exception as exc:  # noqa: BLE001 - one missing child must not fail the batch.
            log_event(
                "child_full_md.load_missing",
                level="warning",
                session_id=session.id,
                skill_name=name,
                error=str(exc)[:200],
            )
            continue
        if files.skill_md.strip():
            loaded[name] = files.skill_md
    session.child_full_md = loaded
    log_event("child_full_md.loaded", session_id=session.id, requested=len(names), loaded=len(loaded))
    _load_child_sections(session)


def _load_child_sections(session) -> None:
    """Best-effort: index the ``##`` headings of EVERY declared child.

    Full bodies are capped and loaded only for the children this scenario
    delegates to, but a pointer can name a section of any whitelisted skill, so
    the heading index has to cover all of them. Headings only, because the index
    is republished into the prompt on every turn.
    """
    from .sections import h2_titles

    names = [n for n in (dict.fromkeys(safe_skill_name(c) for c in (session.children or []))) if n]
    index: dict[str, list[str]] = {}
    for name in names:
        cached = (session.child_full_md or {}).get(name)
        if cached:
            index[name] = h2_titles(cached)
            continue
        try:
            md = store.load_skill(name).skill_md
        except Exception as exc:  # noqa: BLE001 - one missing child must not fail the batch.
            log_event(
                "child_sections.load_missing",
                level="warning",
                session_id=session.id,
                skill_name=name,
                error=str(exc)[:200],
            )
            continue
        titles = h2_titles(md)
        if titles:
            index[name] = titles
    session.child_sections = index
    log_event(
        "child_sections.loaded",
        session_id=session.id,
        requested=len(names),
        loaded=len(index),
    )


def _drop_declared_children_from_peers(session) -> list[str]:
    """Remove newly declared children from the already-loaded peer skill list.

    ``_load_peer_skill_boundaries`` applies the same exclusion, but it only runs
    on PREPARE entry, so children declared afterwards would otherwise stay in the
    list and be compared against as routing rivals. Filtering in place costs no
    SQL or Blob round trip; the one thing it cannot do is pull in a skill that
    the ``max_skills`` cut dropped, which needs the peer-skills endpoint.
    """
    research = session.prepare_brief.research
    declared = {n for n in (safe_skill_name(c) for c in (session.children or [])) if n}
    if not declared or not research.peer_skills:
        return []
    removed = [p.get("name") for p in research.peer_skills if p.get("name") in declared]
    if removed:
        research.peer_skills = [p for p in research.peer_skills if p.get("name") not in declared]
        log_event(
            "session.peer_skills.child_excluded",
            session_id=session.id,
            skills=",".join(removed),
        )
    return removed


def _load_existing_skill_overlaps(session) -> None:
    """Recompute the overlap table against the skills this session competes with.

    Declared children are delegated to rather than competed with, so they are
    excluded; the table is therefore recomputed whenever the children change.
    """
    from backend.skills_index import get_skills_index
    from backend.models import ExistingSkillOverlap

    understanding = session.prepare_brief.understanding
    topic = (understanding.skill_goal or "").strip()
    capabilities = list(understanding.key_capabilities or [])
    if not topic and not capabilities:
        return
    index = get_skills_index()
    # Attach blob store so the index can intersect SQL+Blob.
    try:
        index.attach_blob_store(store)
    except Exception:  # noqa: BLE001
        pass
    owner_upn = getattr(session, "owner_upn", "") or ""
    candidates = index.keyword_topn(
        topic,
        capabilities,
        n=10,
        upn=owner_upn,
        kind=SkillKind(session.skill_kind),
        exclude={c for c in (safe_skill_name(x) for x in (session.children or [])) if c},
    )
    overlaps = [
        ExistingSkillOverlap(
            name=card.name,
            path=card.path,
            similarity=float(score),
            overlap_areas=list(capabilities)[:3],
            differentiation_required=score >= 0.6,
        )
        for card, score in candidates
        if score > 0
    ]
    session.prepare_brief.research.existing_skills_overlap = overlaps
    session.touch()
    log_event(
        "prepare_entry.overlaps.done",
        session_id=session.id,
        count=len(overlaps),
    )


# Started PREPARE-entry threads, kept so tests can await the background work
# instead of racing it (and so nothing logs into a closed stdout at shutdown).
_PREPARE_ENTRY_THREADS: list = []


def await_prepare_entry(timeout: float = 10.0) -> None:
    """Block until every PREPARE-entry background thread has finished."""
    for thread in list(_PREPARE_ENTRY_THREADS):
        thread.join(timeout)
    _PREPARE_ENTRY_THREADS[:] = [t for t in _PREPARE_ENTRY_THREADS if t.is_alive()]


def _prepare_entry_background(session) -> None:
    """Populate Prepare Brief artifacts when entering PREPARE.

    Runs in a daemon thread. Never raises.
    """
    import threading

    def _run() -> None:
        try:
            # Best-effort: load ACA environment variables up-front so later
            # env-var confirmation knows which existing vars can be reused.
            try:
                load_aca_env_for_session(session, reason="prepare_entry")
                persist_session(session)
            except Exception:  # noqa: BLE001
                pass
            # Parallel-load the boundaries of every skill the user can access so
            # differentiation, the When NOT to Use router, and old-skill advice
            # have real content to reason over (best-effort, never raises).
            try:
                _load_peer_skill_boundaries(session)
                persist_session(session)
            except Exception:  # noqa: BLE001
                pass
            # Scenario sessions read the payload contract and failure messages
            # out of the child's own SKILL.md, so it must be loaded too.
            try:
                _load_child_full_md(session)
            except Exception:  # noqa: BLE001
                pass
            _load_existing_skill_overlaps(session)
            try:
                persist_session(session)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            log_exception("prepare_entry.failed", exc, session_id=getattr(session, "id", None))

    thread = threading.Thread(target=_run, daemon=True, name="prepare-entry")
    _PREPARE_ENTRY_THREADS[:] = [t for t in _PREPARE_ENTRY_THREADS if t.is_alive()]
    _PREPARE_ENTRY_THREADS.append(thread)
    thread.start()


def _post_transition_hook(session, source, target) -> None:
    """Wire PREPARE-entry background work whenever we enter PREPARE."""
    try:
        from backend.models import Stage as _Stage
        if target == _Stage.PREPARE and source != _Stage.PREPARE:
            _prepare_entry_background(session)
    except Exception:  # noqa: BLE001
        pass


register_post_transition_hook(_post_transition_hook)

from .testing import ModeEchoError, run_selection_tests

load_dotenv(override=True)

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
NODE_MODULES_DIR = Path(__file__).resolve().parents[1] / "node_modules"


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

app = FastAPI(title="Skill Generator v2")
# Local same-origin dev: the frontend is served from the same origin as /api/*,
# so a permissive no-credentials CORS policy is enough.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = now_ms()
    log_event(
        "http.request.start",
        method=request.method,
        path=request.url.path,
        query=bool(request.url.query),
        client=request.client.host if request.client else "",
    )
    try:
        response = await call_next(request)
        duration_ms = elapsed_ms(started)
        log_event(
            "http.request.done",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
    except Exception as exc:
        duration_ms = elapsed_ms(started)
        log_exception(
            "http.request.failed",
            exc,
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
        )
        raise

log_event(
    "app.starting",
    cwd=str(Path.cwd()),
    frontend_dir=str(FRONTEND_DIR),
    foundry_endpoint=env_flag("FOUNDRY_PROJECT_ENDPOINT"),
    foundry_agent_name=os.getenv("FOUNDRY_AGENT_NAME", "") or "skill-generator-agent",
    foundry_agent_version=os.getenv("FOUNDRY_AGENT_VERSION", "") or "2",
    mcp_endpoint=env_flag("MCP_ENDPOINT"),
)
store = make_skill_store()
skills_repo.make_skills_repo()

def _active_store_id() -> str:
    """Stable id of the currently configured blob store (account/container/prefix)."""
    try:
        return store.store_id()
    except Exception:  # noqa: BLE001 - never let a logging/id helper break a request.
        return ""
agent = FakeE2EAgent() if fake_agent_enabled() else OrchestratorAgent()
session_store = make_session_store()
# v7 one-time migration: legacy local sessions have no owner_upn and are unreachable.
if (
    isinstance(session_store, LocalSessionStore)
    and os.getenv("SGV2_V7_KEEP_LEGACY_SESSIONS", "").strip().lower() not in {"1", "true", "yes"}
):
    for _legacy in session_store.root.glob("*.json"):
        try:
            _raw = json.loads(_legacy.read_text(encoding="utf-8"))
            if isinstance(_raw, dict) and _raw.get("owner_upn"):
                continue
            _legacy.unlink()
        except (OSError, json.JSONDecodeError):
            pass
sessions: dict[str, Session] = session_store.load_all()
auth_tokens: dict[str, dict[str, Any]] = {}
oauth_states: dict[str, dict[str, Any]] = {}
auth_store = make_auth_store(auth_tokens, oauth_states)
require_upn = acl_mod.make_upn_dependency(auth_tokens, auth_store)
optional_upn = acl_mod.make_optional_upn_dependency(auth_tokens, auth_store)
_session_store_location = str(getattr(session_store, "root", getattr(session_store, "id", "")))
log_event("app.ready", session_count=len(sessions), session_root=_session_store_location)

PASSIVE_TOOL_CALLS = {
    "stage_transition",
    "record_understanding",
    "record_research",
    "record_reflection",
    "record_delegation",
    "update_verify_checklist",
    "update_prepare_checklist",
    "request_materials",
    "show_test_results",
}


def _normalize_named_items(items: list[Any], *, default_field: str = "name") -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(dict(item))
        else:
            normalized.append({default_field: str(item)})
    return normalized


def get_session(session_id: str) -> Session:
    session = sessions.get(session_id)
    if not session:
        session = session_store.load(session_id)
        if session:
            sessions[session.id] = session
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _hydrate_current_skill_from_remote(session: Session) -> bool:
    """Restore current SKILL.md from the saved remote skill when the local draft is empty.

    A return-to-PREPARE intentionally clears the local draft, so we do not hydrate
    there. In later stages an empty local draft with a remote binding is stale
    session state; reloading the saved skill keeps the Files tab usable.
    """
    if (session.current_skill.skill_md or "").strip():
        return False
    if session.current_stage == Stage.PREPARE.value:
        return False
    remote_name = (session.remote_skill_id or session.target_skill_id or "").strip()
    if not remote_name:
        return False
    try:
        files = store.load_skill(remote_name)
    except Exception as exc:  # noqa: BLE001 - read path must stay best-effort.
        log_exception("session.hydrate_remote_failed", exc, session_id=session.id, remote_skill=remote_name)
        return False
    session.current_skill.skill_md = files.skill_md
    session.current_skill.version_hash = files.version_hash
    session.remote_skill_id = files.name
    session.remote_version_hash = files.version_hash
    session.target_skill_id = session.target_skill_id or files.name
    session.blob_store_id = _active_store_id()
    session.touch()
    log_event(
        "session.hydrate_remote.done",
        session_id=session.id,
        remote_skill=files.name,
        skill_md_bytes=len(files.skill_md.encode("utf-8")),
    )
    return True


def get_session_for_user(session_id: str, upn: str) -> Session:
    # Same as get_session but enforces ownership. Returns 404 (not 403) when
    # the session exists but is owned by someone else -- avoids leaking ids.
    session = sessions.get(session_id)
    if not session:
        session = session_store.load(session_id)
        if session:
            sessions[session.id] = session
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if upn and session.owner_upn and session.owner_upn != upn:
        raise HTTPException(status_code=404, detail="Session not found")
    if _hydrate_current_skill_from_remote(session):
        persist_session(session)
    return session


def load_aca_env_for_session(session, *, reason: str = "manual") -> None:
    """Populate ``session.aca_env_result`` / ``aca_env_error`` via the MCP tool.

    Best-effort and never raises. When the MCP / ACA environment variables are
    not configured this is a no-op so the rest of the flow is unaffected.
    """
    from .mcp_jsonrpc import list_aca_environment_variables_jsonrpc

    mcp_url = os.getenv("MCP_ENDPOINT", "").strip().rstrip("/")
    aca_app = os.getenv("ACA_APP_NAME", "").strip()
    aca_rg = os.getenv("ACA_RESOURCE_GROUP", "").strip()
    aca_sub = os.getenv("ACA_SUBSCRIPTION_ID", "").strip()
    if not all([mcp_url, aca_app, aca_rg, aca_sub]):
        log_event(
            "session.aca_env.skipped",
            session_id=session.id,
            reason="not_configured",
            has_mcp=bool(mcp_url),
            has_app=bool(aca_app),
            has_rg=bool(aca_rg),
            has_sub=bool(aca_sub),
        )
        return
    log_event("session.aca_env.start", session_id=session.id, reason=reason, app_name=aca_app)
    data, err = list_aca_environment_variables_jsonrpc(
        mcp_url,
        app_name=aca_app,
        resource_group=aca_rg,
        subscription_id=aca_sub,
    )
    if err:
        session.aca_env_result = None
        session.aca_env_error = err
        log_event("session.aca_env.failed", level="warning", session_id=session.id, error=err)
    else:
        session.aca_env_result = data
        session.aca_env_error = ""
        variables = data.get("variables", []) if isinstance(data, dict) else []
        obo = (data.get("architectural_config", {}) or {}).get("OBO_SCOPE_REGISTRY", {}) if isinstance(data, dict) else {}
        log_event(
            "session.aca_env.loaded",
            session_id=session.id,
            variable_count=len(variables),
            obo_token_count=len(obo),
        )
    session.touch()


def _extract_description(skill_md: str) -> str:
    # Pull description from frontmatter; fall back to first prose body line.
    _name, desc = parse_frontmatter(skill_md or "")
    if desc:
        return desc[:1000]
    # Body fallback: skip the frontmatter block entirely and never return a
    # frontmatter-style ``key: value`` line (that is how the SQL description
    # previously ended up holding ``name: <skill>``).
    body = skill_md or ""
    fm = re.match(r"^\ufeff?---\s*\n.*?\n---\s*\n", body, re.DOTALL)
    if fm:
        body = body[fm.end():]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if re.match(r"^[A-Za-z_][\w-]*\s*:(\s|$)", line):
            continue  # looks like a stray frontmatter/key line
        return line[:1000]
    return ""


def _user_can_modify_skill(user_upn: str, name: str) -> bool:
    """True only when ``name`` is in the user's modifiable set: granted in
    SQL (ACL) AND readable in the active blob store. Mirrors the SQL-grant
    intersect blob-presence rule used by the modify-skill list.
    """
    if not user_upn or not name:
        return False
    if not acl_mod.can_access(user_upn, name):
        return False
    try:
        store.load_skill(name)
    except Exception:  # noqa: BLE001 -- missing/unreadable blob == not modifiable.
        return False
    return True


def _skill_name_taken(name: str, sql_row: Any = None) -> bool:
    """Whether ``name`` is already claimed by an SQL row or a blob."""
    if sql_row is not None or skills_repo.get_skill(name) is not None:
        return True
    try:
        store.load_skill(name)
    except Exception:  # noqa: BLE001 -- missing/unreadable blob == free name.
        return False
    return True


def _warn_rename_cleanup(session: Session, old_name: str, new_name: str, why: str) -> None:
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                f"Renamed to `{new_name}`, but the old skill `{old_name}` could not be removed "
                f"({why}), so both names exist right now. Ask an admin to delete `{old_name}`."
            ),
        )
    )
    session.touch()


def _finalize_skill_rename(
    session: Session,
    user_upn: str,
    old_name: str,
    new_name: str,
    *,
    is_public: bool,
) -> None:
    """Rename = delete the old skill, keep only the new one (method B).

    Called only AFTER the new skill is fully committed (blob + SQL + grant).
    Strictly gated: the old skill is deleted only if it is in the current
    user's modifiable set (SQL grant intersect blob presence). Best-effort: a
    cleanup failure is logged but never breaks the just-saved new skill.
    """
    if not _user_can_modify_skill(user_upn, old_name):
        log_event(
            "skill.rename.skipped_no_access",
            level="warning",
            session_id=session.id,
            user_upn=user_upn,
            old_name=old_name,
            new_name=new_name,
        )
        _warn_rename_cleanup(session, old_name, new_name, "you no longer have access to it")
        return
    # Grants hang off skill_key, so deleting the old row cascades every share
    # away. Copy them first, and abort the delete if that fails -- losing the
    # old skill is worse than leaving both names around.
    try:
        for grant in skills_repo.list_grants(old_name):
            skills_repo.add_grant(
                new_name,
                grant.user_upn,
                granted_by=grant.granted_by,
                expires_at=grant.expires_at,
            )
        if not is_public:
            skills_repo.add_grant(new_name, user_upn, granted_by=user_upn)
    except Exception as exc:  # noqa: BLE001 -- keep the old skill reachable.
        log_exception(
            "skill.rename.grants_failed",
            exc,
            session_id=session.id,
            old_name=old_name,
            new_name=new_name,
        )
        _warn_rename_cleanup(session, old_name, new_name, f"its grants could not be moved: {exc}")
        return
    try:
        # Delete old SQL row (FK cascade clears its now-copied grants) then old blob.
        skills_repo.delete_skill(old_name)
        store.delete_skill(old_name)
        # Grants and the skill set changed -- drop caches so reads re-scan.
        acl_mod.get_cache().invalidate(None)
        from backend.skills_index import get_skills_index
        get_skills_index().invalidate()
        log_event(
            "skill.rename.done",
            session_id=session.id,
            user_upn=user_upn,
            old_name=old_name,
            new_name=new_name,
        )
    except Exception as exc:  # noqa: BLE001 -- new skill already saved; never fail the save.
        log_exception(
            "skill.rename.cleanup_failed",
            exc,
            session_id=session.id,
            old_name=old_name,
            new_name=new_name,
        )
        _warn_rename_cleanup(session, old_name, new_name, f"cleanup failed: {exc}")


def save_skill_dual_write(
    session: Session,
    user_upn: str,
    requested_name: str | None = None,
    orphan_action: str | None = None,
) -> SkillFiles:
    # Validate topology and every ACL before the first write, then commit Blob,
    # SQL metadata, and grants.
    started = now_ms()
    skill_name = infer_skill_name(session, requested_name)
    kind = SkillKind(session.skill_kind)
    issues = validate_topology(
        session.current_skill.skill_md,
        kind,
        mode=Mode(session.mode),
        expected_name=skill_name,
        child_resolver=_session_child_resolver(session),
    )
    errors = [issue for issue in issues if issue.severity is Severity.ERROR]
    if errors:
        detail = "\n".join(f"{issue.rule}: {issue.message}" for issue in errors)
        raise HTTPException(status_code=400, detail=f"Topology validation failed:\n{detail}")

    # The host executes the sample code verbatim, so a block that does not parse
    # is never shippable. Every other lint rule stays advisory.
    lint_errors = [issue for issue in _session_lint(session, session.current_skill.skill_md) if issue.severity == "error"]
    if lint_errors:
        detail = "\n".join(f"{issue.rule}: {issue.message}" for issue in lint_errors)
        raise HTTPException(status_code=400, detail=f"Skill lint failed:\n{detail}")

    declared_children: list[str] = []
    internal_children: list[str] = []
    if kind is SkillKind.SCENARIO:
        metadata = parse_frontmatter_meta(session.current_skill.skill_md)
        declared_children = [safe_skill_name(child) for child in metadata.get("children", [])]
        # Every whitelist entry must be loadable by this user, delegated or not.
        for child in declared_children:
            acl_mod.assert_can_access(user_upn, child)
        # But only the children this parent delegates to are ITS capability
        # skills. The whitelist also names pre-existing skills that other
        # parents and the catalog rely on, and hiding those is not ours to do.
        delegated = set(delegation_children(session))
        internal_children = [c for c in declared_children if c in delegated]
    previous_children = _infer_previous_children(session)
    orphaned_children = [
        child
        for child in previous_children
        if child not in declared_children and _is_internal_skill(child)
    ]
    if orphaned_children and orphan_action not in {"keep_internal", "make_capability"}:
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "orphaned_children",
                "recoverable": True,
                "children": orphaned_children,
                "message": (
                    "Removing these children leaves them marked internal and no longer referenced by this parent. "
                    "Choose whether to keep them internal or make them capability skills."
                ),
            },
        )

    old_name = safe_skill_name(session.remote_skill_id) if (session.remote_skill_id or "").strip() else ""
    is_rename = bool(old_name) and old_name != skill_name
    existing = skills_repo.get_skill(skill_name)
    if is_rename and _skill_name_taken(skill_name, existing):
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "rename_conflict",
                "recoverable": True,
                "old_name": old_name,
                "new_name": skill_name,
                "message": (
                    f"`{skill_name}` already exists. Renaming onto it would overwrite that skill, "
                    "so the rename was refused. Pick a different name."
                ),
            },
        )
    old_row = skills_repo.get_skill(old_name) if is_rename else None
    if existing is not None:
        acl_mod.assert_can_access(user_upn, skill_name)
    # Saving is a content action. Visibility is owned solely by
    # PATCH /api/skills/{name}/visibility, so it is only ever inherited here --
    # on a rename from the row being renamed, since the new name has none yet.
    visibility_row = old_row if is_rename else existing
    is_public = bool(visibility_row.is_public) if visibility_row is not None else False
    files = SkillFiles(
        name=skill_name,
        skill_md=session.current_skill.skill_md,
        version_hash=session.current_skill.version_hash,
    )
    log_event(
        "skill.save.start",
        session_id=session.id,
        skill_name=skill_name,
        user_upn=user_upn,
        existed_in_sql=existing is not None,
        is_public=is_public,
        skill_md_bytes=len(files.skill_md.encode("utf-8")),
    )
    saved = store.save_skill(files)
    try:
        # A rename must carry the old row's internal flag across, otherwise a
        # hidden capability child reappears in the host catalog under its new name.
        skills_repo.upsert_skill(
            saved.name,
            is_public=is_public,
            is_internal=old_row.is_internal if old_row is not None else None,
        )
        # A public skill needs no grant row; a private one must keep its author
        # reachable, including when it is being demoted back from public.
        if not is_public:
            skills_repo.add_grant(saved.name, user_upn, granted_by=user_upn)
        for child in internal_children:
            child_row = skills_repo.get_skill(child)
            skills_repo.upsert_skill(
                child,
                is_public=bool(child_row and child_row.is_public),
                enabled=bool(child_row is None or child_row.enabled),
                is_internal=True,
            )
        if orphan_action == "make_capability":
            for child in orphaned_children:
                child_row = skills_repo.get_skill(child)
                if child_row is not None:
                    skills_repo.upsert_skill(
                        child,
                        is_public=child_row.is_public,
                        enabled=child_row.enabled,
                        is_internal=False,
                    )
        acl_mod.get_cache().invalidate(None if is_public else user_upn)
        if is_rename:
            _finalize_skill_rename(session, user_upn, old_name, saved.name, is_public=is_public)
    except Exception as exc:  # noqa: BLE001
        log_exception(
            "skill.save.sql_failed",
            exc,
            session_id=session.id,
            skill_name=saved.name,
            orphan_blob=saved.name,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Skill content saved to blob but SQL update failed: {exc}",
        ) from exc
    session.target_skill_id = saved.name
    session.remote_skill_id = saved.name
    session.remote_version_hash = saved.version_hash
    session.current_skill.version_hash = saved.version_hash
    session.children = declared_children
    session.blob_store_id = _active_store_id()
    session.touch()
    log_event(
        "skill.save.done",
        blob_store_id=session.blob_store_id,
        session_id=session.id,
        skill_name=saved.name,
        version_hash=saved.version_hash,
        duration_ms=elapsed_ms(started),
    )
    return saved


def persist_session(session: Session) -> None:
    log_event("session.persist.start", session_id=session.id, stage=session.current_stage)
    session_store.save(session)


def get_auth_record(request: Request) -> dict[str, Any] | None:
    auth_id = request.cookies.get("sgv2_auth")
    if not auth_id:
        return None
    record = auth_store.get_auth_token(auth_id) or auth_tokens.get(auth_id)
    if not record:
        return None
    return record


def get_delegated_token(request: Request) -> str | None:
    record = get_auth_record(request)
    return str(record.get("access_token") or "") if record else None


def current_content(session: Session, target_file: str) -> str:
    if target_file == "SKILL.md":
        return session.current_skill.skill_md
    raise HTTPException(status_code=400, detail=f"Unknown target file: {target_file}")


def set_current_content(session: Session, target_file: str, content: str, new_hash: str) -> None:
    if target_file == "SKILL.md":
        session.current_skill.skill_md = content
    else:
        raise HTTPException(status_code=400, detail=f"Unknown target file: {target_file}")
    session.current_skill.version_hash = new_hash
    session.touch()


def infer_skill_name(session: Session, requested_name: str | None = None) -> str:
    if requested_name:
        return safe_skill_name(requested_name)
    # The live frontmatter name is the single source of truth in every mode, so
    # a rename really moves the blob folder. MODIFY does not fork the skill
    # because ``is_rename`` keys off remote_skill_id and deletes the old one.
    fm_name, _ = parse_frontmatter(session.current_skill.skill_md)
    if fm_name.strip():
        return safe_skill_name(fm_name)
    if session.target_skill_id:
        return safe_skill_name(session.target_skill_id)
    return "skill"


def _session_child_resolver(session: Session):
    """Resolve a declared child's SKILL.md for topology rules T6/T7.

    Prefers the copy already loaded into the session (which reflects any
    in-session child edit) and falls back to the store. Returns ``None`` when
    the child cannot be resolved, which is what T6 reports as unresolved.
    """

    def resolve(name: str) -> str | None:
        key = safe_skill_name(name)
        cached = (session.child_full_md or {}).get(key)
        if cached:
            return cached
        try:
            return store.load_skill(key).skill_md or None
        except Exception:  # noqa: BLE001 - an unresolvable child is a topology finding, not a crash.
            return None

    return resolve


def _is_internal_skill(name: str) -> bool:
    """Whether a skill is currently hidden from the host catalog.

    A child dropped from the whitelist is only an orphan if something actually
    marked it internal. Plain dependencies were never hidden, so removing one
    must stay silent instead of prompting the user about a state change that
    never happened.
    """
    try:
        row = skills_repo.get_skill(safe_skill_name(name))
    except Exception:  # noqa: BLE001 - an unreadable row is not an orphan prompt.
        return False
    return bool(row is not None and row.is_internal)


def _children_of_saved_skill(name: str) -> list[str]:
    """Children declared by the SKILL.md currently in Blob under ``name``."""
    skill_name = safe_skill_name(name or "")
    if not skill_name:
        return []
    try:
        metadata = parse_frontmatter_meta(store.load_skill(skill_name).skill_md)
    except Exception:  # noqa: BLE001 - a new or missing skill has no children.
        return []
    children = metadata.get("children")
    if not isinstance(children, list):
        return []
    return list(dict.fromkeys(
        safe_skill_name(child)
        for child in children
        if isinstance(child, str) and child.strip()
    ))


def _infer_previous_children(session: Session) -> list[str]:
    return _children_of_saved_skill(session.remote_skill_id or "")


def execute_selection_tests(
    skill_content: str,
    positive_samples: list[str],
    negative_samples: list[str],
    *,
    version_hash: str = "",
    delegated_token: str | None = None,
    session: Session | None = None,
):
    kind = SkillKind(session.skill_kind) if session else SkillKind.CAPABILITY
    try:
        return _dispatch_selection_tests(
            skill_content,
            positive_samples,
            negative_samples,
            version_hash=version_hash,
            delegated_token=delegated_token,
            session=session,
            kind=kind,
        )
    except ModeEchoError as exc:
        # Deliberately terminal. Surfaced as an upstream contract failure so the
        # missing/mismatched field is readable instead of collapsing into a 500.
        log_exception("selection_test.mode_echo_failed", exc, kind=kind.value)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _dispatch_selection_tests(
    skill_content: str,
    positive_samples: list[str],
    negative_samples: list[str],
    *,
    version_hash: str,
    delegated_token: str | None,
    session: Session | None,
    kind: SkillKind,
):
    if fake_test_runner_enabled():
        return fake_run_selection_tests(
            skill_content,
            positive_samples,
            negative_samples,
            version_hash=version_hash,
            delegated_token=delegated_token,
            kind=kind,
            delegation=session.prepare_brief.delegation if session else None,
        )
    if kind is not SkillKind.SCENARIO or session is None:
        return run_selection_tests(
            skill_content,
            positive_samples,
            negative_samples,
            version_hash=version_hash,
            delegated_token=delegated_token,
        )
    return run_selection_tests(
        skill_content,
        positive_samples,
        negative_samples,
        version_hash=version_hash,
        delegated_token=delegated_token,
        kind=kind,
        mode=Mode(session.mode),
        expected_name=session.remote_skill_id or session.target_skill_id or "",
        delegation=session.prepare_brief.delegation,
        child_resolver=_session_child_resolver(session),
    )


def session_binding_state(session: Session, *, allow_unsaved: bool = False) -> tuple[str, bool, str]:
    remote_name = session.remote_skill_id or session.target_skill_id or ""
    remote_version = session.remote_version_hash or ""
    local_version = session.current_skill.version_hash or ""
    if not remote_name:
        return "NEW", False, "This skill has not been saved to Blob yet. Save it before running selection tests."
    if not remote_version:
        return remote_name, False, f"Blob skill '{remote_name}' has no recorded remote version. Reload or save it before testing."
    if not allow_unsaved and local_version and local_version != remote_version:
        return remote_name, False, f"Local files differ from Blob skill '{remote_name}' (UNSAVED). Save before running selection tests."
    return remote_name, True, ""


def ensure_session_testable(session: Session, *, allow_unsaved: bool = False) -> str:
    remote_name, ok, reason = session_binding_state(session, allow_unsaved=allow_unsaved)
    if not ok:
        log_event(
            "session.test.rejected",
            level="warning",
            session_id=session.id,
            remote_skill=remote_name,
            remote_version_hash=session.remote_version_hash,
            local_version_hash=session.current_skill.version_hash,
            reason=reason,
        )
        raise HTTPException(status_code=409, detail=reason)
    return remote_name


def move_to_test_after_run(session: Session, summary: str) -> None:
    stage = Stage(session.current_stage)
    if stage == Stage.TEST:
        session.conversation.append(ChatMessage(role=MessageRole.SYSTEM, content=f"Manual test run completed. {summary}"))
        session.touch()
        return
    if stage in TRANSITION_TABLE and Stage.TEST in TRANSITION_TABLE[stage]:
        transition(session, Stage.TEST, summary)
    else:
        session.current_stage = Stage.TEST
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=f"Stage forced to TEST after test run: {stage} -> TEST. {summary}",
            )
        )
        session.touch()


def session_test_samples(session: Session) -> tuple[list[str], list[str]]:
    # Prefer the PrepareBrief samples (same source as the samples_confirmed
    # checkpoint). Fall back to the legacy VerifyChecklist.test_samples content
    # for sessions created before samples moved into the brief.
    brief = session.prepare_brief
    positive: list[str] = [str(q) for q in (brief.positive_samples or []) if str(q).strip()]
    negative: list[str] = [
        str(getattr(ns, "query", "")).strip()
        for ns in (brief.negative_samples or [])
        if str(getattr(ns, "query", "")).strip()
    ]
    if not (positive and negative):
        content = session.verify_checklist.test_samples.content
        if isinstance(content, dict):
            positive_raw = content.get("positive") or content.get("positive_samples") or []
            negative_raw = content.get("negative") or content.get("negative_samples") or []
            if not positive and isinstance(positive_raw, list):
                positive = [str(item) for item in positive_raw if str(item).strip()]
            if not negative and isinstance(negative_raw, list):
                negative = [str(item) for item in negative_raw if str(item).strip()]

    if positive and negative:
        return positive[:6], negative[:6]

    skill_name, _ = parse_frontmatter(session.current_skill.skill_md)
    skill_name = safe_skill_name(skill_name or session.target_skill_id or "this-skill")
    return (
        [
            f"Help me use {skill_name}",
            f"I need to run the {skill_name.replace('-', ' ')} workflow",
            f"請用 {skill_name} 幫我處理需求",
            f"Can you call the {skill_name} capability?",
            f"我要使用這個 skill 完成 {skill_name.replace('-', ' ')}",
        ],
        [
            "Tell me a joke",
            "Summarize an unrelated news article",
            "Create a UI mockup only",
            "Explain the weather forecast",
            "Write a generic project plan",
        ],
    )


def patch_error_detail(call, exc: PatchError) -> dict[str, Any]:
    message = str(exc)
    lowered = message.lower()
    if "ambiguous" in lowered:
        kind = "ambiguous_anchor"
        guidance = (
            "The patch anchor matched multiple places. Regenerate the patch with "
            "the nearest unique Markdown section heading and more unchanged context, "
            "or replace the whole containing section."
        )
    elif "not found" in lowered:
        kind = "anchor_not_found"
        guidance = (
            "The patch anchor was not found in the latest draft. Regenerate the "
            "patch against the current content and include a unique section heading."
        )
    elif "version hash" in lowered:
        kind = "version_mismatch"
        guidance = "The draft changed since the patch was proposed. Regenerate the patch against the latest version."
    else:
        kind = "patch_apply_failed"
        guidance = "Regenerate the patch with a wider, unique context."
    return {
        "error": message,
        "kind": kind,
        "recoverable": True,
        "tool": call.tool,
        "call_id": call.call_id,
        "target_file": call.args.get("target_file"),
        "guidance": guidance,
        "retry_prompt": (
            f"The previous patch for {call.args.get('target_file', 'the target file')} failed: {message}\n"
            f"{guidance}\n"
            "Please propose a corrected V4A patch. Do not repeat the same patch."
        ),
    }


# Tools that only make sense for one layer of the topology. Calling one from the
# wrong layer is a modelling mistake, not a transport failure, so it goes down
# the recoverable tool_effect_rejected path with guidance instead of crashing.
KIND_ONLY_TOOLS: dict[str, SkillKind] = {
    "record_variables": SkillKind.CAPABILITY,
    "record_delegation": SkillKind.SCENARIO,
    "propose_child_edit": SkillKind.SCENARIO,
}

# dbo.skills.CK_skill_name_format. safe_skill_name() would silently mangle a bad
# name into something the user never asked for, so a rename is rejected instead.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _apply_draft_name_override(session: Session, requested: str) -> None:
    """Honour a name the user typed on the draft accept card.

    Model-authored names (field untouched) keep the lenient safe_skill_name()
    path in save_skill_dual_write; a name the user typed is validated instead,
    because coercing it would store a name they never chose.
    """
    fm_name, _ = parse_frontmatter(session.current_skill.skill_md)
    if not requested or requested == fm_name.strip():
        return
    if not _SKILL_NAME_RE.match(requested) or len(requested) > MAX_SKILL_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "invalid_skill_name",
                "recoverable": True,
                "name": requested,
                "message": (
                    f"`{requested}` is not a valid skill name. Use lowercase kebab-case "
                    f"(a-z, 0-9 and '-', no leading or trailing hyphen), at most "
                    f"{MAX_SKILL_NAME_LEN} characters."
                ),
            },
        )
    own = safe_skill_name(session.remote_skill_id) if (session.remote_skill_id or "").strip() else ""
    if requested != own and _skill_name_taken(requested):
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "skill_name_conflict",
                "recoverable": True,
                "name": requested,
                "message": (
                    f"`{requested}` already exists. Saving under it would overwrite that skill, "
                    "so the draft was not accepted. Pick a different name."
                ),
            },
        )
    # Topology rule T6 compares the frontmatter against the name being saved.
    try:
        renamed = replace_frontmatter_name(session.current_skill.skill_md, requested)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"kind": "invalid_skill_name", "recoverable": True, "message": str(exc)},
        ) from exc
    set_current_content(session, "SKILL.md", renamed, compute_hash(renamed))

# The routing checkpoint compares this skill against its peers, and a declared
# child is excluded from that peer set. Confirming it before any child is named
# therefore compares against a list that still contains the delegation targets.
_ROUTING_CHECKPOINT = "routing_uniqueness_confirmed"


def _assert_children_before_routing(session: Session, item: str, confirmed: bool) -> None:
    if not confirmed or item != _ROUTING_CHECKPOINT:
        return
    if SkillKind(session.skill_kind) is not SkillKind.SCENARIO or session.children:
        return
    raise ValueError(
        f"{_ROUTING_CHECKPOINT} cannot be confirmed while this scenario skill declares "
        "no children. The peer skills this checkpoint compares against exclude every "
        "declared child, so confirming now compares this skill against the very skills "
        "it delegates to. Ask the user which skills this scenario delegates to and "
        "record them with record_delegation; they can also pick them in the Dependency "
        "whitelist panel at the top of the Checklist tab."
    )


# What decides which layer the file belongs to.
_LAYER_RULES = frozenset({"T4", "T10"})
# What decides whether the frontmatter parses at all.
_PARSEABILITY_RULES = frozenset({"T1", "T2", "T3"})


def _topology_errors(skill_md: str, kind: SkillKind) -> list[TopologyIssue]:
    # Mode.NEW so the MODIFY/IMPORT downgrade of T1-T3 to warnings never applies
    # to content this session just authored.
    return [
        issue
        for issue in validate_topology(skill_md, kind, mode=Mode.NEW)
        if issue.severity is Severity.ERROR
    ]


def _assert_topology_preserved(session: Session, skill_md: str, *, tool: str) -> None:
    """Reject a draft/patch result that breaks the skill's layer or its frontmatter.

    Both failures are silent otherwise: the file keeps working, it just stops
    being the kind of skill the session is authoring, or its frontmatter stops
    parsing while ``parse_frontmatter``'s regex fallback keeps the UI looking
    correct.
    """
    kind = SkillKind(session.skill_kind)
    issues = _topology_errors(skill_md, kind)

    layer = [issue for issue in issues if issue.rule in _LAYER_RULES]
    if layer:
        detail = " ".join(issue.message for issue in layer)
        raise ValueError(
            f"{tool} was rejected: the result no longer declares this skill as "
            f"{kind.value}. {detail} Re-send it with `metadata.children` and "
            "`metadata.skill_type` intact."
        )

    # Parseability is enforced as a REGRESSION only, so a file that was already
    # malformed when it was imported stays editable.
    before = session.current_skill.skill_md
    baseline = {issue.rule for issue in _topology_errors(before, kind)} if before.strip() else set()
    regressed = [
        issue
        for issue in issues
        if issue.rule in _PARSEABILITY_RULES and issue.rule not in baseline
    ]
    if regressed:
        detail = " ".join(issue.message for issue in regressed)
        raise ValueError(
            f"{tool} was rejected: the result's YAML frontmatter no longer parses. {detail} "
            "A common cause is a stray leading space in front of a top-level key such as "
            "`description`, which YAML reads as a continuation of the previous value."
        )


def _session_lint(session: Session, skill_md: str) -> list:
    """Run the artifact content lint with whatever cross-file context the session has."""
    return lint_skill(
        skill_md,
        SkillKind(session.skill_kind),
        child_full_md=getattr(session, "child_full_md", None),
        host_capabilities=[
            capability
            for entry in (session.prepare_brief.delegation or [])
            for capability in (entry.host_capabilities or [])
        ],
    )


def _note_skill_lint(session: Session, skill_md: str, *, tool: str) -> None:
    """Surface content defects the topology rules cannot see.

    Advisory for the same reason as the fidelity scan, with one exception: A1
    (the sample code does not parse) is enforced at save time instead.
    """
    try:
        issues = _session_lint(session, skill_md)
    except Exception as exc:  # noqa: BLE001 - a broken lint must never break a turn
        log_exception("skill_lint.scan_failed", exc, session_id=session.id, tool=tool)
        return
    count = lint_warning_count(issues)
    if not count:
        return
    rules = ", ".join(sorted({issue.rule for issue in issues}))
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                f"{count} skill-lint finding(s) after {tool} ({rules}). These are content "
                "defects the topology rules do not cover. Review them in the Topology tab and "
                "fix them before proposing the next stage."
            ),
            metadata={"skill_lint": [issue.to_dict() for issue in issues]},
        )
    )
    session.touch()
    log_event(
        "skill_lint.warnings",
        level="warning",
        session_id=session.id,
        tool=tool,
        count=count,
        rules=rules,
    )


def _note_material_fidelity(session: Session, skill_md: str, *, tool: str) -> None:
    """Surface sample code that drifted from the user's materials.

    Warning-only by design: drift is a judgement call a human has to make, and a
    check that can reject a draft would be worse than the drift it catches.
    """
    try:
        issues = scan_material_fidelity(skill_md, session.materials)
    except Exception as exc:  # noqa: BLE001 - a broken scan must never break a turn
        log_exception("material_fidelity.scan_failed", exc, session_id=session.id, tool=tool)
        return
    count = fidelity_warning_count(issues)
    if not count:
        return
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                f"{count} material-fidelity warning(s) after {tool}: the sample code differs "
                "from the attached materials. Open the Topology tab to review them."
            ),
            metadata={"material_fidelity": [issue.to_dict() for issue in issues]},
        )
    )
    session.touch()
    log_event(
        "material_fidelity.warnings",
        level="warning",
        session_id=session.id,
        tool=tool,
        count=count,
    )


def apply_tool_effect(session: Session, tool: str, args: dict[str, Any]) -> None:
    required_kind = KIND_ONLY_TOOLS.get(tool)
    if required_kind is not None and SkillKind(session.skill_kind) is not required_kind:
        raise ValueError(
            f"{tool} is only available for a {required_kind.value} skill, and this "
            f"session authors a {session.skill_kind} skill."
        )

    # v6: 5-stage state machine + invariants.
    if tool in {"stage_transition", "request_stage_transition"}:
        target = args.get("target") or args.get("target_stage")
        if not target:
            raise ValueError("stage transition requires target/target_stage")
        transition(session, Stage(str(target).lower()), args.get("reason") or args.get("summary", ""))
        return

    if tool == "update_verify_checklist":
        item = args.get("item")
        checklist_item = getattr(session.verify_checklist, item, None) if item else None
        if checklist_item is not None:
            checklist_item.status = args.get("status", "pending")
            checklist_item.content = args.get("content")
            session.touch()
        return

    if tool == "update_prepare_checklist":
        item = args.get("item")
        confirmed = bool(args.get("confirmed", False))
        evidence = (args.get("evidence") or "").strip()
        if item and item in session.prepare_brief.verify_checklist:
            _assert_children_before_routing(session, item, confirmed)
            session.prepare_brief.verify_checklist[item] = confirmed
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            session.prepare_brief.verify_updated_at[item] = ts
            if confirmed:
                if evidence:
                    session.prepare_brief.verify_evidence[item] = evidence
                # If user-mode confirmation arrived with no evidence, keep prior evidence (do not wipe).
            else:
                # Unconfirm: clear evidence so UI does not show stale rationale.
                session.prepare_brief.verify_evidence.pop(item, None)
            session.prepare_brief.last_updated = now_ms()
            session.touch()
        return

    if tool == "propose_skill_draft":
        if session.current_stage != Stage.DRAFT.value:
            raise ValueError(f"propose_skill_draft only allowed in DRAFT stage (current={session.current_stage})")
        if session.current_skill.skill_md.strip():
            raise ValueError("propose_skill_draft already used this session; use propose_patch in REFINE instead")
        proposed = args.get("skill_md", "")
        _assert_topology_preserved(session, proposed, tool="propose_skill_draft")
        session.current_skill.skill_md = proposed
        session.current_skill.version_hash = compute_hash(session.current_skill.skill_md)
        session.touch()
        return

    if tool == "propose_patch":
        if session.current_stage not in {Stage.REFINE.value, Stage.TEST.value}:
            raise ValueError(f"propose_patch only allowed in REFINE/TEST (current={session.current_stage})")
        return

    if tool == "rename_skill":
        if session.current_stage not in {Stage.REFINE.value, Stage.TEST.value}:
            raise ValueError(f"rename_skill only allowed in REFINE/TEST (current={session.current_stage})")
        new_name = str(args.get("new_name", "")).strip()
        if not _SKILL_NAME_RE.match(new_name) or len(new_name) > MAX_SKILL_NAME_LEN:
            raise ValueError(
                f"`{new_name}` is not a valid skill name. Use lowercase kebab-case "
                f"(a-z, 0-9 and '-', no leading or trailing hyphen), at most "
                f"{MAX_SKILL_NAME_LEN} characters."
            )
        current_name, _ = parse_frontmatter(session.current_skill.skill_md)
        if safe_skill_name(current_name) == new_name:
            raise ValueError(f"This skill is already named `{new_name}`; there is nothing to rename.")
        return

    if tool == "record_understanding":
        from backend.models import NeighborSkill
        ub = session.prepare_brief.understanding
        # Accept both the new skill_goal and the legacy user_goal arg name.
        goal = args.get("skill_goal")
        if goal is None:
            goal = args.get("user_goal")
        if goal is not None:
            ub.skill_goal = str(goal)
        if args.get("differentiation") is not None:
            ub.differentiation = str(args["differentiation"])
        for field in ("input_sources", "key_capabilities", "out_of_scope", "open_questions"):
            val = args.get(field)
            if isinstance(val, list):
                setattr(ub, field, [str(x) for x in val])
        neighbors = args.get("neighbor_skills")
        if isinstance(neighbors, list):
            parsed_nb: list[NeighborSkill] = []
            for item in neighbors:
                if isinstance(item, dict) and str(item.get("skill", "")).strip():
                    parsed_nb.append(
                        NeighborSkill(
                            skill=str(item.get("skill", "")).strip(),
                            axis=str(item.get("axis", "")),
                            scenario=str(item.get("scenario", "")),
                        )
                    )
            ub.neighbor_skills = parsed_nb
        session.prepare_brief.last_updated = now_ms()
        session.touch()
        return

    if tool == "record_research":
        rb = session.prepare_brief.research
        # Recording a research brief counts as "web research considered" for the
        # PREPARE quality gate (the agent may legitimately find nothing new).
        if rb.web_status == "skipped":
            rb.web_status = "ok"
        if args.get("summary") is not None:
            rb.summary = str(args["summary"])
        for field in ("pitfalls", "open_questions"):
            val = args.get(field)
            if isinstance(val, list):
                setattr(rb, field, [str(x) for x in val])
        for field in ("adjacent_skills", "recommended_apis"):
            val = args.get(field)
            if isinstance(val, list):
                setattr(rb, field, _normalize_named_items(val))
        session.prepare_brief.last_updated = now_ms()
        session.touch()
        return

    if tool == "update_test_samples":
        from backend.models import NegativeSample
        brief = session.prepare_brief
        pos = args.get("positive")
        if isinstance(pos, list):
            brief.positive_samples = [str(x).strip() for x in pos if str(x).strip()]
        neg = args.get("negative")
        if isinstance(neg, list):
            parsed_neg: list[NegativeSample] = []
            for item in neg:
                if isinstance(item, dict):
                    q = str(item.get("query", "")).strip()
                    if not q:
                        continue
                    parsed_neg.append(
                        NegativeSample(
                            query=q,
                            route_to_peer=str(item.get("route_to_peer", "")),
                            why_not_this=str(item.get("why_not_this", "")),
                        )
                    )
                elif str(item).strip():
                    parsed_neg.append(NegativeSample(query=str(item).strip()))
            brief.negative_samples = parsed_neg
        # Keep the legacy VerifyChecklist.test_samples in sync so the Tests tab
        # and any older readers stay consistent with the brief.
        session.verify_checklist.test_samples.content = {
            "positive": list(brief.positive_samples),
            "negative": [ns.query for ns in brief.negative_samples],
        }
        if brief.positive_samples or brief.negative_samples:
            session.verify_checklist.test_samples.status = "confirmed"
        brief.last_updated = now_ms()
        session.touch()
        return

    if tool == "record_variables":
        from backend.models import SkillVariable
        raw = args.get("variables")
        if isinstance(raw, list):
            parsed_vars: list[SkillVariable] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                # Accept both the new (kind/in_aca) and legacy (type) shapes; the
                # SkillVariable validator migrates legacy keys automatically.
                payload = dict(item)
                payload["name"] = name
                parsed_vars.append(SkillVariable.model_validate(payload))
            session.prepare_brief.variables = parsed_vars
        session.prepare_brief.last_updated = now_ms()
        session.touch()
        return

    if tool == "record_delegation":
        from backend.models import Delegation
        raw = args.get("delegation")
        if isinstance(raw, list):
            parsed_delegation: list[Delegation] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                child = safe_skill_name(str(item.get("child_skill", "")).strip())
                if not child:
                    continue
                payload = dict(item)
                payload["child_skill"] = child
                parsed_delegation.append(Delegation.model_validate(payload))
            session.prepare_brief.delegation = parsed_delegation
            # children is the whitelist of every skill the flow may use, so it
            # is the delegation set plus the dependencies the scenario merely
            # calls. Omitting one costs the host that capability silently.
            dependencies = [
                safe_skill_name(str(name).strip())
                for name in (args.get("dependency_skills") or [])
                if str(name).strip()
            ]
            session.children = list(
                dict.fromkeys(
                    [d.child_skill for d in parsed_delegation] + [d for d in dependencies if d]
                )
            )
            try:
                _load_child_full_md(session)
            except Exception:  # noqa: BLE001 - the record itself must still land.
                pass
            try:
                _drop_declared_children_from_peers(session)
            except Exception:  # noqa: BLE001 - the record itself must still land.
                pass
            try:
                _load_existing_skill_overlaps(session)
            except Exception:  # noqa: BLE001 - the record itself must still land.
                pass
        session.prepare_brief.last_updated = now_ms()
        session.touch()
        return

    if tool in {"propose_neighbor_edit", "propose_child_edit"}:
        # INTERACTIVE: do NOT apply here. The proposal stays a pending tool call
        # so the user accepts/rejects it on a chat card (like propose_patch). The
        # apply happens in tool_result on accept via _apply_neighbor_edit_proposal.
        return

    if tool == "record_reflection":
        from backend.models import IterationReflection
        reflection = IterationReflection(
            test_run_id=args.get("test_run_id"),
            what_went_wrong=[str(x) for x in (args.get("what_went_wrong") or [])],
            what_to_change=[str(x) for x in (args.get("what_to_change") or [])],
            confidence_delta=args.get("confidence_delta"),
            raw=str(args.get("raw", "")),
        )
        session.iteration_reflections.append(reflection)
        if session.current_stage == Stage.TEST.value:
            try:
                transition(session, Stage.REFINE, "Reflection recorded; returning to REFINE.")
            except QualityGateError:
                pass
        session.touch()
        return


def _tool_effect_recovery_guidance(session: Session, tool: str, args: dict[str, Any], exc: Exception) -> str:
    """Build a recovery message for a rejected (but non-fatal) tool effect.

    Invalid stage transitions used to crash the whole turn. Instead we hand
    the agent a clear description of what it may do next so it can either
    self-correct (pick a valid transition) or ask the user how to proceed.
    """
    if tool in {"stage_transition", "request_stage_transition"}:
        requested = str(args.get("target") or args.get("target_stage") or "?").lower()
        try:
            current = Stage(session.current_stage)
        except ValueError:
            current = None
        exits = TRANSITION_TABLE.get(current, {}) if current is not None else {}
        cur_name = current.value if current is not None else str(session.current_stage)
        if exits:
            options = "; ".join(f"{t.value} ({reason})" for t, reason in exits.items())
            allowed = ", ".join(t.value for t in exits)
            tail = (
                f"Either emit request_stage_transition with one of the allowed targets ({allowed}) "
                "when one genuinely fits the user's intent, or call ask_user_input to ask the user "
                "how they want to proceed. Do NOT repeat the rejected transition."
            )
        else:
            options = "(none -- this is a terminal stage)"
            tail = "Call ask_user_input to ask the user how they want to proceed."
        return (
            f"Stage transition rejected: you are in '{cur_name}' and cannot move directly to "
            f"'{requested}'. Valid transitions from '{cur_name}': {options}. {tail}"
        )
    if tool == "update_prepare_checklist" and not session.children:
        return (
            f"{exc} Call ask_user_input and ask which existing capability skill(s) this "
            "scenario delegates its work to, then record them with record_delegation once "
            "the user answers. Retry this confirmation only after they are declared. Do NOT "
            "confirm a different checkpoint instead."
        )
    return (
        f"The '{tool}' action could not be applied: {exc}. Do not repeat it unchanged. "
        "Adjust your approach or ask the user how to proceed with ask_user_input."
    )


@app.post("/api/sessions")
def create_session(req: CreateSessionRequest, upn: str = Depends(require_upn)) -> Session:
    started = now_ms()
    log_event(
        "session.create.start",
        mode=req.mode,
        target_skill_id=req.target_skill_id,
        skill_kind=req.skill_kind or SkillKind.CAPABILITY,
        material_count=len(req.materials),
    )
    session = Session(
        mode=req.mode,
        skill_kind=req.skill_kind or SkillKind.CAPABILITY,
        target_skill_id=req.target_skill_id,
        materials=req.materials,
        owner_upn=upn,
    )
    if req.mode == "modify" and req.target_skill_id:
        # ACL: cannot create a session targeting a skill the user has no grant on.
        # Convert 403 -> 404 so we do not leak the existence of skills the user
        # has no permission to see.
        try:
            acl_mod.assert_can_access(upn, req.target_skill_id)
        except HTTPException as exc:
            if exc.status_code == 403:
                raise HTTPException(status_code=404, detail="Skill not found") from exc
            raise
        log_event("session.create.load_target.start", session_id=session.id, target_skill_id=req.target_skill_id)
        try:
            files = store.load_skill(req.target_skill_id)
        except FileNotFoundError as exc:
            log_exception("session.create.load_target.not_found", exc, target_skill_id=req.target_skill_id)
            raise HTTPException(
                status_code=404,
                detail="Selected skill was not found in Blob. Refresh skill list or check Blob config.",
            ) from exc
        session.current_skill.skill_md = files.skill_md
        session.current_skill.version_hash = files.version_hash
        session.remote_skill_id = files.name
        session.remote_version_hash = files.version_hash
        session.blob_store_id = _active_store_id()
        metadata = parse_frontmatter_meta(files.skill_md)
        raw_children = metadata.get("children")
        children = (
            [safe_skill_name(child) for child in raw_children if isinstance(child, str) and child.strip()]
            if isinstance(raw_children, list)
            else []
        )
        has_children = bool(children)
        declares_scenario = metadata.get("skill_type") == SCENARIO_SKILL_TYPE
        if has_children != declares_scenario:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"metadata.skill_type must be `{SCENARIO_SKILL_TYPE}` exactly when "
                    "metadata.children is a non-empty YAML list."
                ),
            )
        inferred_kind = SkillKind.SCENARIO if has_children else SkillKind.CAPABILITY
        if req.skill_kind is not None and SkillKind(req.skill_kind) is not inferred_kind:
            raise HTTPException(
                status_code=400,
                detail=f"Requested skill_kind does not match the existing skill topology ({inferred_kind.value}).",
            )
        session.skill_kind = inferred_kind
        session.children = children
        session = Session.model_validate(session.model_dump(mode="json"))
        log_event(
            "session.create.load_target.done",
            session_id=session.id,
            target_skill_id=req.target_skill_id,
            blob_store_id=session.blob_store_id,
            version_hash=files.version_hash,
        )
    else:
        # Do not block new/import session creation on Blob list latency. The
        # existing skill index is helpful context, but the chat flow can start
        # without it and fetch specific skills later when needed.
        session.existing_skills_index = []
    sessions[session.id] = session
    persist_session(session)
    # New sessions start in PREPARE without a stage transition, so the
    # post-transition hook never fires. Kick off the same background load
    # (ACA env + peer-skill boundaries + overlaps) here so the catalog of the
    # user's accessible skills is ready when they describe their goal.
    try:
        _prepare_entry_background(session)
    except Exception:  # noqa: BLE001
        pass
    log_event("session.create.done", session_id=session.id, mode=req.mode, duration_ms=elapsed_ms(started))
    return session


@app.get("/api/sessions")
def list_sessions(upn: str = Depends(require_upn)) -> list[SessionSummary]:
    if not isinstance(session_store, LocalSessionStore):
        try:
            sessions.update(session_store.load_all())
        except Exception as exc:  # noqa: BLE001
            log_exception("session.list.refresh_failed", exc, user_upn=upn)
    owned = {sid: s for sid, s in sessions.items() if s.owner_upn == upn}
    return session_store.list(owned)


@app.get("/api/sessions/{session_id}")
def read_session(session_id: str, upn: str = Depends(require_upn)) -> Session:
    return get_session_for_user(session_id, upn)


@app.post("/api/sessions/{session_id}/children")
def refresh_session_children(
    session_id: str,
    req: ChildrenUpdateRequest | None = None,
    upn: str = Depends(require_upn),
) -> Session:
    session = get_session_for_user(session_id, upn)
    if req is not None:
        if SkillKind(session.skill_kind) is not SkillKind.SCENARIO:
            raise HTTPException(status_code=400, detail="Children can only be set on a scenario skill.")
        children = list(dict.fromkeys(
            safe_skill_name(child) for child in req.children if safe_skill_name(child)
        ))
        for child in children:
            acl_mod.assert_can_access(upn, child)
        session.children = children
    _load_child_full_md(session)
    if req is not None:
        try:
            removed = _drop_declared_children_from_peers(session)
        except Exception:  # noqa: BLE001 - the declaration itself must still land.
            removed = []
        if removed:
            session.conversation.append(
                ChatMessage(
                    role=MessageRole.SYSTEM,
                    content=(
                        "Removed from the routing comparison because they are now declared "
                        f"children: {', '.join(removed)}."
                    ),
                    metadata={"peer_skills_removed": removed},
                )
            )
        try:
            _load_existing_skill_overlaps(session)
        except Exception:  # noqa: BLE001 - the declaration itself must still land.
            pass
    session.touch()
    persist_session(session)
    return session


@app.get("/api/sessions/{session_id}/topology")
def inspect_session_topology(session_id: str, upn: str = Depends(require_upn)) -> dict[str, Any]:
    session = get_session_for_user(session_id, upn)
    issues = validate_topology(
        session.current_skill.skill_md,
        SkillKind(session.skill_kind),
        mode=Mode(session.mode),
        expected_name=infer_skill_name(session),
        child_resolver=_session_child_resolver(session),
    )
    by_rule: dict[str, list[dict[str, str]]] = {}
    for issue in issues:
        by_rule.setdefault(issue.rule, []).append({
            "severity": issue.severity.value,
            "message": issue.message,
        })
    return {
        "valid": not any(issue.severity is Severity.ERROR for issue in issues),
        "skill_kind": session.skill_kind,
        "results": [
            {
                **rule,
                "passed": not any(item["severity"] == "error" for item in by_rule.get(rule["rule"], [])),
                "issues": by_rule.get(rule["rule"], []),
            }
            for rule in TOPOLOGY_RULES
        ],
        # Warning-only, computed on demand: a draft that drifted from the user's
        # own code is still a valid skill, so this must never gate anything.
        "fidelity": [
            issue.to_dict()
            for issue in scan_material_fidelity(session.current_skill.skill_md, session.materials)
        ],
        "lint": [issue.to_dict() for issue in _session_lint(session, session.current_skill.skill_md)],
    }


@app.post("/api/sessions/{session_id}/peer-skills")
def refresh_session_peer_skills(session_id: str, upn: str = Depends(require_upn)) -> Session:
    """Re-fetch the user's accessible peer skills and their Blob boundaries.

    Useful after local credentials/RBAC were fixed or when an existing session
    still has a stale failed/skipped peer_skills_status.
    """
    started = now_ms()
    session = get_session_for_user(session_id, upn)
    _load_peer_skill_boundaries(session)
    session.touch()
    persist_session(session)
    log_event(
        "session.peer_skills.refresh.done",
        session_id=session.id,
        status=session.prepare_brief.research.peer_skills_status,
        count=len(session.prepare_brief.research.peer_skills),
        duration_ms=elapsed_ms(started),
    )
    return session


@app.post("/api/sessions/{session_id}/aca-env")
def refresh_session_aca_env(session_id: str, upn: str = Depends(require_upn)) -> Session:
    """Re-fetch the live ACA environment variables for this session via MCP.

    Updates ``aca_env_result`` / ``aca_env_error`` and returns the session so
    the UI can show which existing vars are reusable vs which must be added.
    """
    started = now_ms()
    session = get_session_for_user(session_id, upn)
    load_aca_env_for_session(session, reason="manual_refresh")
    persist_session(session)
    log_event(
        "session.aca_env.refresh.done",
        session_id=session.id,
        has_result=bool(session.aca_env_result),
        has_error=bool(session.aca_env_error),
        duration_ms=elapsed_ms(started),
    )
    return session


def require_e2e_mode() -> None:
    if not e2e_enabled():
        raise HTTPException(status_code=404, detail="E2E endpoints are disabled")


@app.post("/api/e2e/reset")
def e2e_reset() -> dict[str, Any]:
    require_e2e_mode()
    global store, session_store, sessions, auth_tokens, oauth_states, agent
    store = CachedSkillStore(LocalSkillStore())
    session_store = LocalSessionStore()
    for path in session_store.root.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            log_event("e2e.session_delete.failed", level="warning", path=str(path))
    sessions = {}
    auth_tokens = {}
    oauth_states = {}
    set_scenario("new_skill_happy_path")
    agent = FakeE2EAgent() if fake_agent_enabled() else agent
    log_event("e2e.reset.done", session_root=str(session_store.root))
    return {"ok": True, "scenario": get_scenario()}


@app.post("/api/e2e/scenario")
def e2e_set_scenario(payload: dict[str, Any]) -> dict[str, Any]:
    require_e2e_mode()
    scenario = set_scenario(str(payload.get("scenario") or "new_skill_happy_path"))
    log_event("e2e.scenario.set", scenario=scenario)
    return {"ok": True, "scenario": scenario}


@app.post("/api/e2e/seed-skill")
def e2e_seed_skill(payload: dict[str, Any]) -> SkillFiles:
    require_e2e_mode()
    files = e2e_skill_files(str(payload.get("name") or "e2e-existing-skill"))
    if payload.get("skill_md"):
        files.skill_md = str(payload["skill_md"])
    saved = store.save_skill(files)
    user_upn = str(payload.get("user_upn") or acl_mod.e2e_default_upn() or "e2e@example.com").strip().lower()
    skills_repo.upsert_skill(saved.name)
    skills_repo.add_grant(saved.name, user_upn, granted_by=user_upn)
    acl_mod.get_cache().invalidate(user_upn)
    log_event("e2e.seed_skill.done", skill_name=saved.name)
    return saved


@app.get("/api/e2e/session/{session_id}")
def e2e_read_session(session_id: str) -> Session:
    require_e2e_mode()
    return get_session(session_id)


@app.post("/api/sessions/{session_id}/materials")
def add_session_material(session_id: str, req: MaterialUpsertRequest, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    material = Material(kind=req.kind, content=req.content, metadata=req.metadata)
    session.materials.append(material)
    session.touch()
    persist_session(session)
    log_event(
        "session.material.add",
        session_id=session.id,
        material_id=material.id,
        kind=material.kind,
        content_chars=len(material.content),
        material_count=len(session.materials),
    )
    return session


@app.put("/api/sessions/{session_id}/materials/{material_id}")
def update_session_material(session_id: str, material_id: str, req: MaterialUpsertRequest, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    for index, existing in enumerate(session.materials):
        if existing.id == material_id:
            session.materials[index] = Material(
                id=existing.id,
                kind=req.kind,
                content=req.content,
                metadata=req.metadata,
                created_at=existing.created_at,
            )
            session.touch()
            persist_session(session)
            log_event(
                "session.material.update",
                session_id=session.id,
                material_id=material_id,
                kind=req.kind,
                content_chars=len(req.content),
            )
            return session
    log_event("session.material.update.not_found", level="warning", session_id=session.id, material_id=material_id)
    raise HTTPException(status_code=404, detail="Material not found")


@app.delete("/api/sessions/{session_id}/materials/{material_id}")
def delete_session_material(session_id: str, material_id: str, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    before = len(session.materials)
    session.materials = [m for m in session.materials if m.id != material_id]
    if len(session.materials) == before:
        log_event("session.material.delete.not_found", level="warning", session_id=session.id, material_id=material_id)
        raise HTTPException(status_code=404, detail="Material not found")
    session.touch()
    persist_session(session)
    log_event(
        "session.material.delete",
        session_id=session.id,
        material_id=material_id,
        material_count=len(session.materials),
    )
    return session


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is not configured: {name}")
    return value


def public_url_for(request: Request, route_name: str) -> str:
    """Build the absolute URL for an OAuth redirect route (local, same-origin)."""
    return str(request.url_for(route_name))

def oauth_config() -> dict[str, str]:
    tenant_id = required_env("MICROSOFT_TENANT_ID")
    client_id = required_env("MICROSOFT_CLIENT_ID")
    scope = required_env("MICROSOFT_OBO_SCOPE")
    authority = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0"
    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": required_env("MICROSOFT_CLIENT_SECRET"),
        "scope": scope,
        "authorize_url": f"{authority}/authorize",
        "token_url": f"{authority}/token",
    }


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 20:
        return f"{token[:4]}...{token[-4:]} (len={len(token)})"
    return f"{token[:10]}...{token[-10:]} (len={len(token)})"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def profile_from_claims(claims: dict[str, Any]) -> dict[str, Any]:
    # ``email`` is the SOURCE OF TRUTH for permissions (matches dbo.user_skill_grants.user_upn).
    email = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    )
    return {
        "name": claims.get("name") or claims.get("given_name") or "",
        "username": claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "",
        "email": (email or "").strip().lower(),
        "oid": claims.get("oid") or claims.get("sub") or "",
        "tenant_id": claims.get("tid") or "",
    }


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    config = oauth_config()
    record = get_auth_record(request)
    if not record:
        e2e_upn = acl_mod.e2e_default_upn()
        if e2e_upn:
            record = {"expires_at": None, "profile": {"email": e2e_upn, "name": e2e_upn}}
    log_event(
        "auth.status",
        authenticated=bool(record),
        client_id=config["client_id"],
        scope=config["scope"],
        profile=record.get("profile") if record else None,
    )
    return {
        "authenticated": bool(record),
        "expires_at": record.get("expires_at") if record else None,
        "scope": config["scope"],
        "client_id": config["client_id"],
        "profile": record.get("profile") if record else None,
    }


@app.get("/api/auth/login")
def auth_login(request: Request):
    config = oauth_config()
    if not config["client_secret"]:
        log_event("auth.login.misconfigured", level="error", missing="MICROSOFT_CLIENT_SECRET")
        raise HTTPException(status_code=500, detail="MICROSOFT_CLIENT_SECRET is not configured on the backend")
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = make_pkce_pair()
    state_record = {"expires_at": time.time() + 600, "code_verifier": code_verifier}
    auth_store.save_oauth_state(state, state_record)
    redirect_uri = public_url_for(request, "auth_callback")
    log_event(
        "auth.login.redirect",
        client_id=config["client_id"],
        scope=config["scope"],
        redirect_uri=redirect_uri,
        authorize_url=config["authorize_url"],
        state_expires_in_seconds=600,
    )
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": f"openid profile offline_access {config['scope']}",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    location = f"{config['authorize_url']}?{urlencode(params)}"
    return Response(status_code=302, headers={"Location": location, "Cache-Control": "no-store"})


@app.get("/api/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    started = now_ms()
    if error:
        log_event("auth.callback.provider_error", level="warning", error=error, error_description=error_description)
        raise HTTPException(status_code=400, detail=f"{error}: {error_description}")
    state_record = auth_store.pop_oauth_state(state)
    if not state or not state_record or float(state_record.get("expires_at", 0)) < time.time():
        log_event("auth.callback.invalid_state", level="warning", has_state=bool(state), has_code=bool(code))
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    if not code:
        log_event("auth.callback.missing_code", level="warning")
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")

    config = oauth_config()
    redirect_uri = public_url_for(request, "auth_callback")
    log_event(
        "auth.token_exchange.start",
        client_id=config["client_id"],
        scope=config["scope"],
        redirect_uri=redirect_uri,
        token_url=config["token_url"],
    )
    payload = urlencode(
        {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": f"openid profile offline_access {config['scope']}",
            "code_verifier": state_record["code_verifier"],
        }
    ).encode("utf-8")
    token_request = UrlRequest(
        config["token_url"],
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(token_request, timeout=30) as response:  # noqa: S310 - Microsoft token endpoint from config.
            token_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail_payload = json.loads(body)
        except json.JSONDecodeError:
            detail_payload = {"error_response": body}
        log_event(
            "auth.token_exchange.failed",
            level="error",
            status=exc.code,
            redirect_uri=redirect_uri,
            token_url=config["token_url"],
            response=detail_payload,
            duration_ms=elapsed_ms(started),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Token exchange failed",
                "status": exc.code,
                "redirect_uri": redirect_uri,
                "token_url": config["token_url"],
                "response": detail_payload,
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log_exception("auth.token_exchange.failed", exc, redirect_uri=redirect_uri, duration_ms=elapsed_ms(started))
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}") from exc

    access_token = token_payload.get("access_token")
    if not access_token:
        log_event("auth.token_exchange.missing_access_token", level="error", response=token_payload)
        raise HTTPException(status_code=502, detail=f"Token response did not include access_token: {token_payload}")
    claims = decode_jwt_claims(access_token)
    profile = profile_from_claims(claims)
    log_event(
        "auth.token_exchange.done",
        access_token=mask_token(access_token),
        expires_in=token_payload.get("expires_in", 3600),
        scope=token_payload.get("scope", config["scope"]),
        profile=profile,
        duration_ms=elapsed_ms(started),
    )
    auth_id = secrets.token_urlsafe(32)
    auth_record = {
        "access_token": access_token,
        "expires_at": time.time() + int(token_payload.get("expires_in", 3600)),
        "scope": token_payload.get("scope", config["scope"]),
        "profile": profile,
    }
    auth_store.save_auth_token(auth_id, auth_record)
    response = RedirectResponse("/")
    response.set_cookie(
        "sgv2_auth",
        auth_id,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=int(token_payload.get("expires_in", 3600)),
    )
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth_id = request.cookies.get("sgv2_auth")
    if auth_id:
        auth_store.delete_auth_token(auth_id)
        auth_tokens.pop(auth_id, None)
    log_event("auth.logout", had_cookie=bool(auth_id))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("sgv2_auth")
    return response


@app.post("/api/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest, upn: str = Depends(require_upn)) -> dict[str, Any]:
    # Non-streaming: collect every agent event into a list and return JSON.
    # The frontend replays the batch through the same onEvent dispatcher it
    # would otherwise use for SSE.
    started = now_ms()
    session = get_session_for_user(session_id, upn)
    log_event(
        "chat.start",
        session_id=session.id,
        stage=session.current_stage,
        message_chars=len(req.message or ""),
        material_count=len(req.materials),
    )
    if req.materials:
        session.materials.extend(req.materials)
    if req.message:
        msg_meta: dict[str, Any] = {}
        if req.auto_continue:
            msg_meta["auto_continue"] = True
            msg_meta["auto_depth"] = req.auto_depth
        session.conversation.append(
            ChatMessage(role=MessageRole.USER, content=req.message, metadata=msg_meta)
        )
    session.touch()
    persist_session(session)

    # Refresh the full SKILL.md of the selected neighbor skills so the agent can
    # edit them with a patch this turn without asking the user for the text.
    _load_selected_neighbor_full_md(session)

    events: list[dict[str, Any]] = []
    assistant_chunks: list[str] = []
    event_counts: dict[str, int] = {}
    try:
        for event in agent.stream(session, req.message):
            if not isinstance(event, dict):
                log_event("chat.event.non_dict", level="warning", session_id=session.id, event_type=type(event).__name__)
                assistant_chunks.append(str(event))
                continue
            event_name = str(event.get("event") or "text_delta")
            event_counts[event_name] = event_counts.get(event_name, 0) + 1
            data = event.get("data", {})
            if not isinstance(data, dict):
                log_event("chat.event.data_non_dict", level="warning", session_id=session.id, event=event_name, data_type=type(data).__name__)
                data = {"delta": str(data)} if event_name == "text_delta" else {"value": data}
            if event_name == "text_delta":
                assistant_chunks.append(str(data.get("delta") or ""))
            if event_name == "tool_call":
                log_event(
                    "chat.tool_call",
                    session_id=session.id,
                    tool=data.get("tool"),
                    call_id=data.get("call_id"),
                    stage=session.current_stage,
                )
                try:
                    apply_tool_effect(session, data["tool"], data.get("args", {}))
                except QualityGateError as qge:
                    log_event(
                        "chat.quality_gate_failed",
                        level="warning",
                        session_id=session.id,
                        tool=data.get("tool"),
                        missing=list(qge.missing),
                    )
                    events.append({"event": "quality_gate_failed", "data": {
                        "tool": data.get("tool"),
                        "missing": list(qge.missing),
                        "message": str(qge),
                    }})
                    continue
                except ValueError as rejected:
                    # Recoverable tool effect (e.g. invalid stage transition).
                    # Do NOT crash the turn: record guidance so the agent can
                    # self-correct next turn, and surface a non-fatal event so
                    # the user can be asked how to proceed.
                    guidance = _tool_effect_recovery_guidance(session, data.get("tool"), data.get("args", {}), rejected)
                    session.conversation.append(
                        ChatMessage(
                            role=MessageRole.SYSTEM,
                            content=guidance,
                            metadata={"tool": data.get("tool"), "recoverable_error": str(rejected)},
                        )
                    )
                    session.touch()
                    call_id = data.get("call_id")
                    session.pending_tool_calls = [
                        call for call in session.pending_tool_calls if call.call_id != call_id
                    ]
                    log_event(
                        "chat.tool_effect_rejected",
                        level="warning",
                        session_id=session.id,
                        tool=data.get("tool"),
                        stage=session.current_stage,
                        error=str(rejected),
                    )
                    events.append({"event": "tool_effect_rejected", "data": {
                        "tool": data.get("tool"),
                        "message": str(rejected),
                        "guidance": guidance,
                    }})
                    persist_session(session)
                    continue
                if data["tool"] in PASSIVE_TOOL_CALLS:
                    call_id = data.get("call_id")
                    session.pending_tool_calls = [
                        call for call in session.pending_tool_calls if call.call_id != call_id
                    ]
                persist_session(session)
            events.append({"event": event_name, "data": data})
        assistant_text = "".join(assistant_chunks).strip()
        if assistant_text:
            session.conversation.append(ChatMessage(role=MessageRole.ASSISTANT, content=assistant_text))
            session.touch()
        persist_session(session)
        log_event(
            "chat.done",
            session_id=session.id,
            stage=session.current_stage,
            assistant_chars=len(assistant_text),
            pending_tool_count=len(session.pending_tool_calls),
            event_counts=event_counts,
            duration_ms=elapsed_ms(started),
        )
        events.append({"event": "state_update", "data": session.model_dump(mode="json")})
        events.append({"event": "done", "data": {}})
    except Exception as exc:
        persist_session(session)
        log_exception("chat.failed", exc, session_id=session.id, stage=session.current_stage, duration_ms=elapsed_ms(started))
        events.append({"event": "error", "data": {"message": str(exc)}})
    return {"events": events}


@app.post("/api/sessions/{session_id}/tool-result")
def tool_result(session_id: str, req: ToolResultRequest, request: Request, upn: str = Depends(require_upn)) -> Session:
    started = now_ms()
    session = get_session_for_user(session_id, upn)
    call = next((c for c in session.pending_tool_calls if c.call_id == req.tool_call_id), None)
    if not call:
        log_event("tool_result.missing_call", level="warning", session_id=session.id, call_id=req.tool_call_id)
        raise HTTPException(status_code=404, detail="Tool call not found")
    result = req.result if isinstance(req.result, dict) else {"value": req.result}
    delegated_token = get_delegated_token(request)
    log_event(
        "tool_result.start",
        session_id=session.id,
        stage=session.current_stage,
        tool=call.tool,
        call_id=call.call_id,
        action=result.get("action") if isinstance(result, dict) else None,
    )

    try:
        if call.tool == "propose_skill_draft" and result.get("action") == "accept":
            _apply_draft_name_override(session, str(result.get("name") or "").strip())
            _assert_topology_preserved(session, session.current_skill.skill_md, tool="propose_skill_draft")
            _note_material_fidelity(session, session.current_skill.skill_md, tool="propose_skill_draft")
            _note_skill_lint(session, session.current_skill.skill_md, tool="propose_skill_draft")
            save_skill_dual_write(
                session,
                upn,
                result.get("name"),
                orphan_action=result.get("orphan_action"),
            )
            if session.current_stage == Stage.DRAFT.value:
                transition(session, Stage.REFINE, "Draft accepted and saved.")
        elif call.tool == "propose_patch" and result.get("action") == "accept":
            args = call.args
            target = args["target_file"]
            content = current_content(session, target)
            updated, new_hash = apply_v4a_to_content(content, args["patch"])
            # Guardrail: a patch is applied to a skill that already declares its
            # layer, so anything that drops that declaration is refused BEFORE
            # it reaches the session or Blob.
            _assert_topology_preserved(session, updated, tool="propose_patch")
            set_current_content(session, target, updated, new_hash)
            # REFINE is exactly where material-aligned code gets rewritten back
            # into invented code, so the check runs here too.
            _note_material_fidelity(session, updated, tool="propose_patch")
            _note_skill_lint(session, updated, tool="propose_patch")
            session.patch_history.append(
                PatchRecord(
                    target_file=target,
                    v4a_patch=args["patch"],
                    version_hash_before=version_hash(content),
                    version_hash_after=new_hash,
                    content_before=content,
                    content_after=updated,
                    reason=args.get("reason", ""),
                )
            )
            # Keep Blob + SQL consistent on every change: once a skill has
            # been persisted, push the patched content (and any metadata
            # change such as description) straight to Blob + SQL.
            if (session.remote_skill_id or "").strip():
                try:
                    save_skill_dual_write(session, upn)
                except HTTPException as sync_exc:
                    session.conversation.append(
                        ChatMessage(
                            role=MessageRole.SYSTEM,
                            content=(
                                "Patch applied locally, but syncing to Blob/SQL failed: "
                                f"{sync_exc.detail}. Use Save to retry the sync."
                            ),
                        )
                    )
                    session.touch()
                    log_event(
                        "tool_result.patch_autosync_failed",
                        level="warning",
                        session_id=session.id,
                        skill_name=session.remote_skill_id,
                        status_code=sync_exc.status_code,
                    )
        elif call.tool == "rename_skill" and result.get("action") == "accept":
            new_name = safe_skill_name(str(call.args.get("new_name", "")).strip())
            before = current_content(session, "SKILL.md")
            before_hash = version_hash(before)
            updated = replace_frontmatter_name(before, new_name)
            new_hash = version_hash(updated)
            _assert_topology_preserved(session, updated, tool="rename_skill")
            set_current_content(session, "SKILL.md", updated, new_hash)
            record = PatchRecord(
                target_file="SKILL.md",
                v4a_patch="",
                version_hash_before=before_hash,
                version_hash_after=new_hash,
                content_before=before,
                content_after=updated,
                reason=call.args.get("reason", "") or f"Rename skill to {new_name}",
            )
            session.patch_history.append(record)
            if (session.remote_skill_id or "").strip():
                try:
                    save_skill_dual_write(session, upn)
                except HTTPException:
                    # Unlike a patch, a rename that cannot be persisted must not
                    # stay in the session: the draft would then carry a name that
                    # exists nowhere and every later save would be refused too.
                    set_current_content(session, "SKILL.md", before, before_hash)
                    session.patch_history.remove(record)
                    raise
        elif call.tool in {"propose_neighbor_edit", "propose_child_edit"} and result.get("action") == "accept":
            if call.tool == "propose_child_edit":
                child = safe_skill_name(str(call.args.get("skill_name", "")).strip())
                if child not in {safe_skill_name(c) for c in (session.children or [])}:
                    raise HTTPException(
                        status_code=400,
                        detail=f"`{child}` is not one of this skill's declared children.",
                    )
            try:
                applied = _apply_neighbor_edit_proposal(session, call.args)
                label = "Child edit" if call.tool == "propose_child_edit" else "Neighbor edit"
                session.conversation.append(
                    ChatMessage(
                        role=MessageRole.SYSTEM,
                        content=(
                            f"{label} accepted for `{applied['skill_name']}`. A new version was added to "
                            "its edit card -- review versions and press Save to Blob to sync, or revert to the original."
                        ),
                        metadata={"neighbor_edit_accepted": applied["skill_name"]},
                    )
                )
                if call.tool == "propose_child_edit":
                    _load_child_full_md(session)
            except PatchError as exc:
                # Surface a recoverable error so the agent can regenerate a wider patch.
                raise HTTPException(status_code=409, detail={
                    "error": str(exc),
                    "kind": "neighbor_patch_failed",
                    "recoverable": True,
                    "guidance": "The edit did not apply: the patch anchor/context was not found in the current SKILL.md. Re-copy a few unchanged CONTEXT lines VERBATIM from the current SKILL.md shown between the <<<BEGIN/END>>> markers in your prompt into a small V4A hunk around your `-`/`+` edits (indentation is matched leniently). Do NOT send a full rewrite.",
                }) from exc
        elif call.tool in {"propose_neighbor_edit", "propose_child_edit"} and result.get("action") == "reject":
            log_event("tool_result.neighbor_edit.rejected", session_id=session.id, call_id=call.call_id)
        elif call.tool == "request_test_run" and result.get("action") in {"run", "accept", None}:
            args = call.args
            use_remote = result.get("source") == "remote"
            remote_name = ensure_session_testable(session, allow_unsaved=use_remote)
            skill_md = session.current_skill.skill_md
            version = session.current_skill.version_hash
            if use_remote:
                files = store.load_skill(remote_name)
                skill_md = files.skill_md
                version = files.version_hash
                session.remote_skill_id = files.name
                session.remote_version_hash = files.version_hash
            log_event(
                "tool_result.request_test_run.accepted",
                session_id=session.id,
                stage=session.current_stage,
                remote_skill=remote_name,
                source=result.get("source") or "current",
                positive_samples=len(args.get("positive_samples", [])),
                negative_samples=len(args.get("negative_samples", [])),
                has_delegated_token=bool(delegated_token),
            )
            run = execute_selection_tests(
                skill_md,
                args.get("positive_samples", []),
                args.get("negative_samples", []),
                version_hash=version,
                delegated_token=delegated_token,
                session=session,
            )
            session.test_runs.append(run)
            move_to_test_after_run(session, "Agent-requested test run completed. Analyze latest test_runs before proposing changes.")
        elif call.tool == "request_positive_samples":
            # The user filled the in-chat positive-samples card. Persist the
            # positives into the Prepare Brief (keeping any existing negatives) so
            # the right-hand Routing samples panel and the test runner see them.
            raw = result.get("positive") if isinstance(result, dict) else None
            positives = [str(q).strip() for q in raw if str(q).strip()] if isinstance(raw, list) else []
            existing_neg = [n.model_dump() for n in session.prepare_brief.negative_samples]
            apply_tool_effect(session, "update_test_samples", {"positive": positives, "negative": existing_neg})
            log_event("tool_result.request_positive_samples.saved", session_id=session.id, positive=len(positives))
    except PatchError as exc:
        detail = patch_error_detail(call, exc)
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=f"Patch apply failed: {detail['kind']}. {detail['guidance']}",
                metadata={"tool": call.tool, "call_id": call.call_id, "patch_error": detail},
            )
        )
        session.touch()
        persist_session(session)
        log_event(
            "tool_result.patch_failed",
            level="warning",
            session_id=session.id,
            tool=call.tool,
            call_id=call.call_id,
            kind=detail["kind"],
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    except ValueError as exc:
        # Topology guardrail (T4/T10). Recoverable: the agent can re-send the
        # same edit with the layer declaration intact.
        log_event(
            "tool_result.topology_rejected",
            level="warning",
            session_id=session.id,
            tool=call.tool,
            call_id=call.call_id,
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail={
            "error": str(exc),
            "kind": "topology_violation",
            "recoverable": True,
            "guidance": str(exc),
        }) from exc
    except HTTPException as exc:
        log_event(
            "tool_result.failed",
            level="error",
            session_id=session.id,
            tool=call.tool,
            call_id=call.call_id,
            status_code=exc.status_code,
            detail=exc.detail,
            duration_ms=elapsed_ms(started),
        )
        raise
    except Exception as exc:
        log_exception("tool_result.failed", exc, session_id=session.id, tool=call.tool, call_id=call.call_id, duration_ms=elapsed_ms(started))
        raise HTTPException(status_code=500, detail=f"{call.tool} failed: {exc}") from exc

    # Post-processing tail: persistence/serialization issues here must also
    # return a JSON error detail rather than escaping as an opaque 500.
    try:
        session.pending_tool_calls = [c for c in session.pending_tool_calls if c.call_id != req.tool_call_id]
        session.conversation.append(
            ChatMessage(role=MessageRole.TOOL, content=json.dumps(req.result, ensure_ascii=False), metadata={"tool": call.tool})
        )
        session.touch()
        persist_session(session)
    except Exception as exc:  # noqa: BLE001 -- the tool effect already applied; report cleanly.
        log_exception("tool_result.persist_failed", exc, session_id=session.id, tool=call.tool, call_id=call.call_id)
        raise HTTPException(status_code=500, detail=f"{call.tool} applied but session persistence failed: {exc}") from exc
    log_event(
        "tool_result.done",
        session_id=session.id,
        stage=session.current_stage,
        tool=call.tool,
        call_id=call.call_id,
        pending_tool_count=len(session.pending_tool_calls),
        duration_ms=elapsed_ms(started),
    )
    return session


@app.post("/api/sessions/{session_id}/patches/{patch_id}/undo")
def undo_session_patch(session_id: str, patch_id: str, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    if not session.patch_history:
        raise HTTPException(status_code=404, detail="No patch history is available")
    latest = session.patch_history[-1]
    if latest.id != patch_id:
        raise HTTPException(status_code=409, detail="Only the latest applied patch can be undone")
    if not latest.content_before:
        raise HTTPException(status_code=409, detail="This patch cannot be undone because previous content was not recorded")
    set_current_content(session, latest.target_file, latest.content_before, latest.version_hash_before or version_hash(latest.content_before))
    session.patch_history.pop()
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=f"Latest patch undone: {latest.target_file}.",
            metadata={"patch_id": patch_id, "target_file": latest.target_file},
        )
    )
    persist_session(session)
    log_event("patch.undo.done", session_id=session.id, patch_id=patch_id, target_file=latest.target_file)
    return session


@app.post("/api/sessions/{session_id}/save")
def save_session_skill(session_id: str, req: SaveSessionSkillRequest, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    if not session.current_skill.skill_md.strip():
        log_event("skill.save.rejected", level="warning", session_id=session.id, reason="empty_skill_md")
        raise HTTPException(status_code=400, detail="No SKILL.md content to save")
    save_skill_dual_write(session, upn, req.name, orphan_action=req.orphan_action)
    persist_session(session)
    return session


@app.put("/api/sessions/{session_id}/draft")
def update_session_draft(session_id: str, draft: SkillDraft, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    session.current_skill = draft
    session.current_skill.version_hash = compute_hash(session.current_skill.skill_md)
    session.touch()
    persist_session(session)
    log_event(
        "session.draft.updated",
        session_id=session.id,
        version_hash=session.current_skill.version_hash,
        skill_md_bytes=len(session.current_skill.skill_md.encode("utf-8")),
    )
    return session


# PREPARE checkpoints form a linear dependency chain. Revising an upstream
# checkpoint means the downstream ones must be re-confirmed (their assumptions
# may have changed). This map drives that cascade.
# A session only ever holds one of `variables_ok` / `delegation_ok` (capability
# vs scenario); the cascade skips whichever key is absent from the checklist.
PREPARE_CHECKPOINT_DEPENDENTS: dict[str, tuple[str, ...]] = {
    "definition_clear": ("routing_uniqueness_confirmed", "variables_ok", "delegation_ok"),
    "routing_uniqueness_confirmed": ("variables_ok", "delegation_ok"),
    "variables_ok": (),
    "delegation_ok": (),
}


def _cascade_prepare_pending(session: Session, changed_key: str) -> list[str]:
    """Set any confirmed downstream checkpoints back to pending. Returns the
    list of checkpoints that were actually flipped (only previously-confirmed
    ones), so callers can tell the user what now needs re-confirmation."""
    brief = session.prepare_brief
    affected: list[str] = []
    for dep in PREPARE_CHECKPOINT_DEPENDENTS.get(changed_key, ()):
        if brief.verify_checklist.get(dep):
            brief.verify_checklist[dep] = False
            brief.verify_updated_at.pop(dep, None)
            affected.append(dep)
    return affected


@app.post("/api/sessions/{session_id}/checklist")
def update_session_checklist(session_id: str, req: ChecklistUpdateRequest, upn: str = Depends(require_upn)) -> Session:
    session = get_session_for_user(session_id, upn)
    # PREPARE-stage checkpoints live in prepare_brief.verify_checklist, not the
    # legacy VerifyChecklist. Handle a user-driven revise/confirm here, with the
    # downstream dependency cascade.
    if req.item in session.prepare_brief.verify_checklist:
        confirmed = req.status == "confirmed"
        brief = session.prepare_brief
        try:
            _assert_children_before_routing(session, req.item, confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        brief.verify_checklist[req.item] = confirmed
        brief.verify_updated_at[req.item] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        affected = _cascade_prepare_pending(session, req.item) if not confirmed else []
        brief.last_updated = now_ms()
        current = Stage(session.current_stage)
        returned_to_prepare = False
        if req.return_to_prepare and current != Stage.PREPARE and Stage.PREPARE in TRANSITION_TABLE.get(current, set()):
            try:
                transition(session, Stage.PREPARE, req.reason or f"Revising checkpoint: {req.item}.")
                returned_to_prepare = True
            except QualityGateError:
                pass
        note = f"Checkpoint {req.item} set to {'confirmed' if confirmed else 'pending'}."
        if affected:
            note += " Downstream re-confirmation needed: " + ", ".join(affected) + "."
        if req.reason:
            note += f" {req.reason}"
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=note.strip(),
                metadata={
                    "checkpoint": req.item,
                    "confirmed": confirmed,
                    "cascaded": affected,
                    "returned_to_prepare": returned_to_prepare,
                },
            )
        )
        session.touch()
        persist_session(session)
        log_event("session.checkpoint.updated", session_id=session.id, item=req.item, confirmed=confirmed, cascaded=affected)
        return session
    if req.item == "test_samples":
        # The Tests-tab "Save samples" must update the AUTHORITATIVE sample source
        # -- the Prepare Brief -- because that is what test runs read
        # (session_test_samples prefers brief.positive_samples/negative_samples).
        # Writing only the legacy VerifyChecklist.test_samples.content silently
        # left run tests using the brief's OLD samples. apply_tool_effect also
        # keeps the legacy content in sync and accepts plain-string negatives.
        content = req.content if isinstance(req.content, dict) else {}
        apply_tool_effect(
            session,
            "update_test_samples",
            {"positive": content.get("positive") or [], "negative": content.get("negative") or []},
        )
        current = Stage(session.current_stage)
        returned_to_prepare = False
        if req.return_to_prepare and current != Stage.PREPARE and Stage.PREPARE in TRANSITION_TABLE.get(current, set()):
            try:
                transition(session, Stage.PREPARE, req.reason or "Revising routing test samples.")
                returned_to_prepare = True
            except QualityGateError:
                pass
        note = "Routing test samples updated."
        if returned_to_prepare:
            note += " Returned to PREPARE to rethink requirements."
        elif current != Stage.PREPARE:
            note += " Kept current stage and SKILL.md."
        if req.reason:
            note += f" {req.reason}"
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=note.strip(),
                metadata={"checklist_item": "test_samples", "status": req.status, "returned_to_prepare": returned_to_prepare},
            )
        )
        session.touch()
        persist_session(session)
        log_event("session.checklist.updated", session_id=session.id, item="test_samples", status=req.status, stage=session.current_stage)
        return session
    checklist_item = getattr(session.verify_checklist, req.item, None)
    if checklist_item is None:
        log_event("session.checklist.update.rejected", level="warning", session_id=session.id, item=req.item)
        raise HTTPException(status_code=400, detail=f"Unknown checklist item: {req.item}")
    checklist_item.status = req.status
    checklist_item.content = req.content
    # Editing a checklist item (e.g. revising routing test_samples) no longer
    # forces a return to PREPARE -- that would discard an already-generated
    # SKILL.md. Stay in the current stage and keep the draft. Only replan from
    # PREPARE when the caller explicitly asks for it.
    current = Stage(session.current_stage)
    returned_to_prepare = False
    if req.return_to_prepare and current != Stage.PREPARE and Stage.PREPARE in TRANSITION_TABLE.get(current, set()):
        try:
            transition(session, Stage.PREPARE, req.reason or f"Checklist item revised: {req.item}.")
            returned_to_prepare = True
        except QualityGateError:
            pass
    note = "Checklist revised: {item}.{reason}".format(
        item=req.item,
        reason=f" {req.reason}" if req.reason else "",
    )
    if not returned_to_prepare and current != Stage.PREPARE:
        note += " Kept current stage and SKILL.md."
    session.conversation.append(
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=note.strip(),
            metadata={"checklist_item": req.item, "status": req.status, "returned_to_prepare": returned_to_prepare},
        )
    )
    session.touch()
    persist_session(session)
    log_event("session.checklist.updated", session_id=session.id, item=req.item, status=req.status, stage=session.current_stage)
    return session


@app.post("/api/sessions/{session_id}/samples")
def update_session_samples(session_id: str, req: SamplesUpdateRequest, upn: str = Depends(require_upn)) -> Session:
    """User-driven edit of the PREPARE routing samples (positive + structured
    negative). Persists into the Prepare Brief; no message is sent to the agent.
    """
    session = get_session_for_user(session_id, upn)
    apply_tool_effect(
        session,
        "update_test_samples",
        {"positive": list(req.positive), "negative": [n.model_dump() for n in req.negative]},
    )
    # Only when the user signals this edit may change the requirements do we mark
    # routing_uniqueness_confirmed (and downstream) pending. A plain "just update
    # the samples" save keeps confirmed checkpoints intact.
    cascaded = _cascade_prepare_pending(session, "routing_uniqueness_confirmed") if req.revisit else []
    persist_session(session)
    log_event(
        "session.samples.updated",
        session_id=session.id,
        positive=len(req.positive),
        negative=len(req.negative),
        revisit=req.revisit,
        cascaded=cascaded,
        stage=session.current_stage,
    )
    return session


def _variable_content_signature(variables: list) -> list[tuple]:
    """Signature of the variables that actually affect SKILL.md, IGNORING the
    in_aca deployment status (reuse vs add produces identical skill text)."""
    sig = []
    for v in variables:
        d = v.model_dump() if hasattr(v, "model_dump") else dict(v)
        sig.append(
            (
                str(d.get("name", "")).strip(),
                str(d.get("kind", "runtime")),
                str(d.get("description", "")).strip(),
                str(d.get("example", "")).strip(),
                bool(d.get("required", True)),
            )
        )
    return sorted(sig)


@app.post("/api/sessions/{session_id}/neighbors")
def update_session_neighbors(session_id: str, req: NeighborsUpdateRequest, upn: str = Depends(require_upn)) -> Session:
    """User-driven edit of the neighbor_skills single source. Persists into the
    understanding brief; no message is sent to the agent."""
    session = get_session_for_user(session_id, upn)
    apply_tool_effect(
        session,
        "record_understanding",
        {"neighbor_skills": [n.model_dump() for n in req.neighbor_skills]},
    )
    # neighbor_skills belong to the routing block and feed everything downstream,
    # so an actual change re-opens routing AND every dependent block for
    # step-by-step re-confirmation.
    cascaded: list[str] = []
    if req.revisit:
        brief = session.prepare_brief
        if brief.verify_checklist.get("routing_uniqueness_confirmed"):
            brief.verify_checklist["routing_uniqueness_confirmed"] = False
            brief.verify_updated_at.pop("routing_uniqueness_confirmed", None)
            cascaded.append("routing_uniqueness_confirmed")
        cascaded += _cascade_prepare_pending(session, "routing_uniqueness_confirmed")
        brief.last_updated = now_ms()
    persist_session(session)
    log_event(
        "session.neighbors.updated",
        session_id=session.id,
        count=len(req.neighbor_skills),
        revisit=req.revisit,
        cascaded=cascaded,
        stage=session.current_stage,
    )
    return session


def _apply_neighbor_edit_proposal(session: Session, args: dict[str, Any]) -> dict[str, Any]:
    """Apply an accepted propose_neighbor_edit to the currently SELECTED version
    and APPEND it as a new, uniquely-versioned agent_proposed entry (never
    overwriting prior versions). Accepts a single V4A patch (indentation is
    matched leniently) -- full rewrites are not accepted. Raises PatchError if the
    patch does not apply (recoverable), or HTTPException(404) if the neighbor is
    unloadable."""
    skill_name = safe_skill_name(str(args.get("skill_name", "")).strip())
    patch = str(args.get("patch", "") or "")
    if not skill_name or not patch.strip():
        raise HTTPException(status_code=400, detail="propose_neighbor_edit needs skill_name and a V4A patch")
    ne = _get_or_init_neighbor_edit(session, skill_name)  # 404 if not loadable
    # Anchor base = the currently SELECTED (in-use) version, which is what the
    # agent was shown in its prompt. But the agent's `find`/anchor may have been
    # copied from a DIFFERENT version it saw earlier (e.g. the pristine Original
    # before any edit), so when it misses the selected version we fall back and
    # try every other version (newest first, Original last) and apply to whichever
    # the snippet actually matches. This makes multi-version neighbors patch
    # reliably instead of failing with an anchor-not-found error. The V4A patch
    # is applied with lenient indentation matching. PatchError on a total miss is
    # recoverable. We keep newest-first ordering so the most up-to-date matching
    # version wins, never silently dropping later edits when the selected matches.
    selected = next((v for v in ne.versions if v.version_id == ne.selected_version_id), None)
    candidates: list[NeighborVersion] = []
    if selected is not None:
        candidates.append(selected)
    for v in reversed(ne.versions):
        if v is not selected:
            candidates.append(v)
    if not candidates:
        raise PatchError("the snippet/anchor was not found in the current SKILL.md.")

    def _try_apply(src: str) -> str:
        applied, _ = apply_v4a_to_content(src, patch)
        return applied

    new_md: str | None = None
    matched: NeighborVersion = candidates[0]
    last_err: PatchError | None = None
    for cand in candidates:
        try:
            new_md = _try_apply(cand.skill_md)
            matched = cand
            break
        except PatchError as exc:
            last_err = exc
            new_md = None
    if new_md is None:
        raise last_err or PatchError("the snippet/anchor was not found in the current SKILL.md.")
    if not new_md.strip():
        raise HTTPException(status_code=400, detail="propose_neighbor_edit produced empty content")
    # APPEND a brand-new, uniquely-versioned entry (uuid version_id + a visibly
    # unique "#N" label) -- never overwrite a prior version even if labels repeat.
    seq = len(ne.versions)
    base_label = (str(args.get("label") or "").strip() or "Agent edit")
    version = NeighborVersion(
        label=f"{base_label} #{seq}",
        skill_md=new_md,
        version_hash=compute_hash(new_md),
        origin="agent_proposed",
    )
    ne.versions.append(version)
    ne.selected_version_id = version.version_id
    ne.status = "draft"
    session.touch()
    log_event(
        "tool.propose_neighbor_edit.applied",
        session_id=session.id,
        skill=skill_name,
        versions=len(ne.versions),
        version_id=version.version_id,
        matched_base=matched.label,
    )
    return {"skill_name": skill_name, "versions": len(ne.versions)}


def _get_or_init_neighbor_edit(session: Session, skill_name: str) -> NeighborEdit:
    """Return the NeighborEdit for ``skill_name``, creating it with an ``original``
    snapshot loaded from the store on first access. Raises 404 if the neighbor is
    not loadable, 403 if the user cannot modify it."""
    for ne in session.neighbor_edits:
        if ne.skill_name == skill_name:
            return ne
    name = safe_skill_name(skill_name)
    try:
        files = store.load_skill(name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"Neighbor skill not found: {skill_name}") from exc
    original = NeighborVersion(
        label="Original",
        skill_md=files.skill_md,
        version_hash=files.version_hash or compute_hash(files.skill_md),
        origin="original",
    )
    ne = NeighborEdit(
        skill_name=name,
        versions=[original],
        selected_version_id=original.version_id,
        saved_version_id=original.version_id,
        status="saved",
    )
    session.neighbor_edits.append(ne)
    return ne


def _neighbor_version(ne: NeighborEdit, version_id: str) -> NeighborVersion | None:
    return next((v for v in ne.versions if v.version_id == version_id), None)


@app.post("/api/sessions/{session_id}/neighbor-edits/{skill}/open")
def neighbor_edit_open(session_id: str, skill: str, upn: str = Depends(require_upn)) -> Session:
    """Open a neighbor skill for user editing: load its current Blob content as
    the immutable 'original' version so the edit card appears. No Blob write."""
    session = get_session_for_user(session_id, upn)
    _get_or_init_neighbor_edit(session, skill)  # raises 404 if not loadable
    persist_session(session)
    log_event("session.neighbor_edit.open", session_id=session.id, skill=safe_skill_name(skill))
    return session


@app.post("/api/sessions/{session_id}/neighbor-edits/{skill}/propose")
def neighbor_edit_propose(session_id: str, skill: str, req: NeighborProposeRequest, upn: str = Depends(require_upn)) -> Session:
    """Push a new version into a neighbor skill's session-scoped edit history.
    Nothing is written to Blob here -- the user must Save explicitly."""
    session = get_session_for_user(session_id, upn)
    ne = _get_or_init_neighbor_edit(session, skill)
    version = NeighborVersion(
        label=req.label or f"Edit {len(ne.versions)}",
        skill_md=req.skill_md,
        version_hash=compute_hash(req.skill_md),
        origin=req.origin,
    )
    ne.versions.append(version)
    ne.selected_version_id = version.version_id
    ne.status = "draft"
    session.touch()
    persist_session(session)
    log_event("session.neighbor_edit.propose", session_id=session.id, skill=ne.skill_name, versions=len(ne.versions))
    return session


@app.post("/api/sessions/{session_id}/neighbor-edits/{skill}/select")
def neighbor_edit_select(session_id: str, skill: str, req: NeighborSelectRequest, upn: str = Depends(require_upn)) -> Session:
    """Select which version is active (preview / to-be-saved). No Blob write."""
    session = get_session_for_user(session_id, upn)
    ne = _get_or_init_neighbor_edit(session, skill)
    if _neighbor_version(ne, req.version_id) is None:
        raise HTTPException(status_code=400, detail="Unknown neighbor version id")
    ne.selected_version_id = req.version_id
    ne.status = "draft" if req.version_id != ne.saved_version_id else "saved"
    session.touch()
    persist_session(session)
    log_event("session.neighbor_edit.select", session_id=session.id, skill=ne.skill_name, version_id=req.version_id)
    return session


@app.post("/api/sessions/{session_id}/neighbor-edits/{skill}/save")
def neighbor_edit_save(session_id: str, skill: str, upn: str = Depends(require_upn)) -> Session:
    """Write the selected version of a neighbor skill to Blob + SQL. Gated by the
    user's modify rights (readable == modifiable)."""
    session = get_session_for_user(session_id, upn)
    ne = _get_or_init_neighbor_edit(session, skill)
    version = _neighbor_version(ne, ne.selected_version_id)
    if version is None:
        raise HTTPException(status_code=400, detail="No selected version to save")
    if not _user_can_modify_skill(upn, ne.skill_name):
        raise HTTPException(status_code=403, detail=f"You cannot modify the skill: {ne.skill_name}")
    files = SkillFiles(name=ne.skill_name, skill_md=version.skill_md, version_hash=version.version_hash)
    saved = store.save_skill(files)
    try:
        existing = skills_repo.get_skill(saved.name)
        skills_repo.upsert_skill(saved.name, is_public=bool(existing and existing.is_public))
    except Exception as exc:  # noqa: BLE001
        log_exception("session.neighbor_edit.sql_failed", exc, session_id=session.id, skill=ne.skill_name)
        raise HTTPException(status_code=500, detail="Neighbor skill saved to Blob but SQL upsert failed") from exc
    ne.saved_version_id = version.version_id
    ne.status = "saved"
    session.touch()
    persist_session(session)
    log_event("session.neighbor_edit.save", session_id=session.id, skill=ne.skill_name, version_id=version.version_id)
    return session


@app.post("/api/sessions/{session_id}/variables")
def update_session_variables(session_id: str, req: VariablesUpdateRequest, upn: str = Depends(require_upn)) -> Session:
    """User-driven edit of the PREPARE skill variables (env reuse/add + runtime).

    Persists into the Prepare Brief. A pure in_aca status flip (reuse <-> add) is
    a deployment-only change and stays silent. A content change (name, kind,
    description, example, required, or add/remove) that affects SKILL.md triggers
    a system message asking the agent to sync the Prerequisites / Required Inputs
    sections -- but only when a draft already exists.
    """
    session = get_session_for_user(session_id, upn)
    before_sig = _variable_content_signature(session.prepare_brief.variables)
    apply_tool_effect(session, "record_variables", {"variables": [v.model_dump() for v in req.variables]})
    after_sig = _variable_content_signature(session.prepare_brief.variables)
    content_changed = before_sig != after_sig
    has_draft = bool(session.current_skill.skill_md.strip())
    if content_changed and has_draft:
        session.conversation.append(
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=(
                    "The user edited the PREPARE variables and a draft already exists. "
                    "Propose ONE narrow patch to sync the '## Prerequisites' (env variables) "
                    "and '## Required Inputs' (runtime variables) sections of SKILL.md with the "
                    "updated variables. A reuse/add (in_aca) status flip is NOT a content change "
                    "and needs no patch."
                ),
                metadata={"variables_content_changed": True},
            )
        )
    persist_session(session)
    log_event(
        "session.variables.updated",
        session_id=session.id,
        count=len(req.variables),
        stage=session.current_stage,
    )
    return session


@app.post("/api/sessions/{session_id}/test")
def test_session_skill(session_id: str, req: RunSessionTestRequest, request: Request, upn: str = Depends(require_upn)) -> Session:
    started = now_ms()
    session = get_session_for_user(session_id, upn)
    if not session.current_skill.skill_md.strip():
        log_event("session.test.rejected", level="warning", session_id=session.id, reason="empty_skill_md")
        raise HTTPException(status_code=400, detail="No skill files are available to test yet. Generate a draft first.")
    use_remote = req.source == "remote"
    remote_name = ensure_session_testable(session, allow_unsaved=use_remote)
    skill_md = session.current_skill.skill_md
    version = session.current_skill.version_hash
    if use_remote:
        files = store.load_skill(remote_name)
        skill_md = files.skill_md
        version = files.version_hash
        session.remote_skill_id = files.name
        session.remote_version_hash = files.version_hash
    positive_samples, negative_samples = session_test_samples(session)
    log_event(
        "session.test.start",
        session_id=session.id,
        stage=session.current_stage,
        remote_skill=remote_name,
        source=req.source,
        positive_samples=len(positive_samples),
        negative_samples=len(negative_samples),
        runner="APIM /run",
        has_delegated_token=bool(get_delegated_token(request)),
    )
    run = execute_selection_tests(
        skill_md,
        positive_samples,
        negative_samples,
        version_hash=version,
        delegated_token=get_delegated_token(request),
        session=session,
    )
    session.test_runs.append(run)
    summary = (
        "Manual test run completed against saved Blob version. Analyze latest test_runs before proposing changes."
        if use_remote
        else "Manual test run completed. Analyze latest test_runs before proposing changes."
    )
    move_to_test_after_run(session, summary)
    session.touch()
    persist_session(session)
    log_event(
        "session.test.done",
        session_id=session.id,
        stage=session.current_stage,
        positive_hit_rate=run.positive_hit_rate,
        negative_correct_reject_rate=run.negative_correct_reject_rate,
        duration_ms=elapsed_ms(started),
    )
    return session


@app.get("/api/skills")
def list_skills(
    upn: str = Depends(require_upn),
    store_filter: str | None = Query(default=None, alias="store", description="Optional blob_store_id filter."),
):
    started = now_ms()
    store_id = _active_store_id()
    log_event("skills.list.start", user_upn=upn, store_filter=store_filter or "", active_store_id=store_id)
    # 1) SQL: get skills the user has grants on (already enabled-only).
    try:
        sql_rows = skills_repo.list_skills_for_user(upn)
    except Exception as exc:  # noqa: BLE001
        log_exception("skills.list.sql_failed", exc, user_upn=upn)
        raise HTTPException(
            status_code=503,
            detail=(
                "Skill metadata (Azure SQL) is unreachable. "
                "Check (a) AZURE_SQL_SERVER / AZURE_SQL_DATABASE in .env, "
                "(b) `az login` is fresh, (c) SQL firewall allows this IP. "
                f"Underlying error: {type(exc).__name__}: {str(exc)[:300]}"
            ),
        ) from exc
    sql_by_name = {row.skill_name: row for row in sql_rows}
    # 2) Blob: get the live list to confirm content exists.
    try:
        blob_skills = store.list_skills()
    except Exception as exc:  # noqa: BLE001
        log_exception("skills.list.blob_failed", exc, user_upn=upn)
        msg = str(exc)
        hint = _blob_access_hint(msg)
        diag = f" {hint}" if hint else ""
        raise HTTPException(
            status_code=503,
            detail=(
                "Skill content (Blob) is unreachable. "
                "Check AZURE_STORAGE_ACCOUNT_URL in .env and storage firewall."
                + diag
                + f" Underlying error: {type(exc).__name__}: {msg[:300]}"
            ),
        ) from exc
    blob_names = {s.name for s in blob_skills}
    # 3) Intersection of (SQL grants) AND (Blob exists). Skills present in only
    #    one side are intentionally hidden from modify mode.
    visible_names = set(sql_by_name) & blob_names
    sql_only = sorted(set(sql_by_name) - blob_names)
    blob_only = sorted(blob_names - set(sql_by_name))
    out = []
    skipped_store = 0
    for entry in blob_skills:
        if entry.name not in visible_names:
            continue
        # 4) Optional blob-store filter: only surface skills from the requested
        #    store (e.g. the store the active session is pinned to).
        entry_store = getattr(entry, "blob_store_id", "") or store_id
        if store_filter and entry_store != store_filter:
            skipped_store += 1
            continue
        payload = entry.model_dump() if hasattr(entry, 'model_dump') else dict(entry.__dict__)
        sql_row = sql_by_name[entry.name]
        # description stays whatever the blob frontmatter says -- dbo.skills has
        # no description column since schema v2.
        payload["enabled"] = bool(sql_row.enabled)
        payload["is_public"] = bool(sql_row.is_public)
        payload["is_internal"] = bool(sql_row.is_internal)
        payload["skill_scope"] = sql_row.skill_scope
        payload["sql_created_at"] = sql_row.created_at.isoformat() if sql_row.created_at else ""
        payload["sql_updated_at"] = sql_row.updated_at.isoformat() if sql_row.updated_at else ""
        out.append(payload)
    log_event(
        "skills.list.done",
        user_upn=upn,
        store_filter=store_filter or "",
        active_store_id=store_id,
        sql_count=len(sql_by_name),
        blob_count=len(blob_names),
        visible_count=len(out),
        visible_names=[entry["name"] for entry in out],
        sql_only_hidden=sql_only,
        blob_only_hidden=blob_only,
        store_filtered_out=skipped_store,
        duration_ms=elapsed_ms(started),
    )
    return out


@app.get("/api/skills/{name}")
def load_skill(name: str, upn: str = Depends(require_upn)) -> SkillFiles:
    acl_mod.assert_can_access(upn, name)
    try:
        started = now_ms()
        files = store.load_skill(name)
        log_event("skills.load.done", skill_name=name, user_upn=upn, version_hash=files.version_hash, duration_ms=elapsed_ms(started))
        return files
    except FileNotFoundError as exc:
        log_exception("skills.load.not_found", exc, skill_name=name)
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/skills/{name}/test")
def test_skill(name: str, req: SkillTestRequest, request: Request, upn: str = Depends(require_upn)):
    acl_mod.assert_can_access(upn, name)
    started = now_ms()
    skill_content = req.skill_content
    version = ""
    if not skill_content:
        files = store.load_skill(name)
        skill_content = files.skill_md
        version = files.version_hash
    log_event(
        "skills.test.start",
        skill_name=name,
        user_upn=upn,
        positive_samples=len(req.positive_samples),
        negative_samples=len(req.negative_samples),
        has_inline_content=bool(req.skill_content),
        has_delegated_token=bool(get_delegated_token(request)),
    )
    run = execute_selection_tests(
        skill_content,
        req.positive_samples,
        req.negative_samples,
        version_hash=version,
        delegated_token=get_delegated_token(request),
    )
    log_event(
        "skills.test.done",
        skill_name=name,
        positive_hit_rate=run.positive_hit_rate,
        negative_correct_reject_rate=run.negative_correct_reject_rate,
        duration_ms=elapsed_ms(started),
    )
    return run


@app.post("/api/skills/refresh")
def refresh_skills_cache(upn: str = Depends(require_upn)):
    acl_mod.get_cache().invalidate(upn)
    log_event("skills.cache.refresh", user_upn=upn)
    return {"refreshed": True, "user_upn": upn}


@app.get("/api/skills/{name}/grants")
def list_skill_grants(name: str, upn: str = Depends(require_upn)):
    acl_mod.assert_can_access(upn, name)
    row = skills_repo.get_skill(name)
    grants = skills_repo.list_grants(name)
    return {
        "skill_name": name,
        "is_public": bool(row.is_public) if row else False,
        "grants": [g.to_payload() for g in grants],
    }


class VisibilityRequest(BaseModel):
    is_public: bool


def _propagate_visibility_to_children(
    parent: str, is_public: bool, upn: str
) -> tuple[list[str], list[str]]:
    """Carry a parent's visibility onto the children it hides from the catalog.

    Only internal children follow: a declared child that is still in the host
    catalog belongs to everyone, and re-scoping it is not this parent's call.
    """
    changed: list[str] = []
    failed: list[str] = []
    for child in _children_of_saved_skill(parent):
        if not _is_internal_skill(child):
            continue
        try:
            skills_repo.set_skill_visibility(child, is_public)
            if not is_public:
                # A private child with no grant is reachable by nobody at all.
                skills_repo.add_grant(child, upn, granted_by=upn)
            changed.append(child)
        except Exception as exc:  # noqa: BLE001 - one child must not abort the rest.
            failed.append(child)
            log_exception(
                "skills.visibility.child_failed",
                exc,
                skill_name=parent,
                child_skill=child,
                user_upn=upn,
                is_public=is_public,
            )
    return changed, failed


@app.patch("/api/skills/{name}/visibility")
def set_skill_visibility(name: str, req: VisibilityRequest, upn: str = Depends(require_upn)):
    acl_mod.assert_can_access(upn, name)
    row = skills_repo.get_skill(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
    if req.is_public and row.owner_upn:
        raise HTTPException(
            status_code=409,
            detail="Only global skills can be made public (CK_skills_is_public_scope).",
        )
    skills_repo.set_skill_visibility(name, req.is_public)
    if not req.is_public:
        # Demoting to private would otherwise leave the caller with no way back in.
        skills_repo.add_grant(name, upn, granted_by=upn)
    children, failed_children = _propagate_visibility_to_children(name, req.is_public, upn)
    acl_mod.get_cache().invalidate(None)
    log_event(
        "skills.visibility.set",
        skill_name=name,
        user_upn=upn,
        is_public=req.is_public,
        children=children,
        failed_children=failed_children,
    )
    return {
        "skill_name": name,
        "is_public": req.is_public,
        "children": children,
        "failed_children": failed_children,
    }


class GrantRequest(BaseModel):
    user_upn: str
    expires_at: str | None = None


@app.post("/api/skills/{name}/grants")
def add_skill_grant(name: str, req: GrantRequest, upn: str = Depends(require_upn)):
    acl_mod.assert_can_access(upn, name)
    target = (req.user_upn or "").strip().lower()
    if not target:
        raise HTTPException(status_code=400, detail="user_upn required")
    expires_dt = None
    if req.expires_at:
        from datetime import datetime as _dt
        try:
            expires_dt = _dt.fromisoformat(req.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid expires_at: {exc}") from exc
    was_insert = skills_repo.add_grant(name, target, granted_by=upn, expires_at=expires_dt)
    acl_mod.get_cache().invalidate(target)
    return {"skill_name": name, "user_upn": target, "inserted": was_insert}


@app.delete("/api/skills/{name}/grants/{user_upn}")
def remove_skill_grant(name: str, user_upn: str, upn: str = Depends(require_upn)):
    acl_mod.assert_can_access(upn, name)
    target = (user_upn or "").strip().lower()
    if not target:
        raise HTTPException(status_code=400, detail="user_upn required")
    if target == upn:
        # Refuse to remove the last grant (caller would orphan themselves).
        row = skills_repo.get_skill(name)
        remaining = [g for g in skills_repo.list_grants(name) if g.user_upn != target]
        if not remaining and not (row and row.is_public):
            raise HTTPException(
                status_code=409,
                detail="Cannot remove the last grant; another user must be granted first.",
            )
    n = skills_repo.remove_grant(name, target)
    acl_mod.get_cache().invalidate(target)
    return {"skill_name": name, "user_upn": target, "removed": n}


@app.get("/api/diagnostics")
def diagnostics(upn: str = Depends(require_upn)):
    """Live health probe for Azure SQL, Azure Blob skill store, ACL, and Foundry config."""
    results: dict[str, Any] = {}

    try:
        db.ping()
        rows = db.fetchone("SELECT COUNT(*) AS n FROM dbo.skills")
        count = int(rows["n"]) if rows else 0
        results["azure_sql"] = {
            "ok": True,
            "detail": f"connected; dbo.skills has {count} rows",
            "hint": "",
        }
    except Exception as exc:  # noqa: BLE001
        results["azure_sql"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "hint": "Check AZURE_SQL_SERVER/DATABASE and the app's AAD credential (Service Principal / Managed Identity / az login).",
        }

    try:
        items = store.list_skills()
        results["azure_blob"] = {
            "ok": True,
            "detail": f"blob store reachable; {len(items)} skill object(s)",
            "hint": "",
        }
    except Exception as exc:  # noqa: BLE001
        results["azure_blob"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "hint": "Check AZURE_STORAGE_ACCOUNT_URL/CONNECTION_STRING, AZURE_BLOB_CONTAINER, and the app's blob credential.",
        }

    try:
        visible = sorted(acl_mod.visible_skill_names(upn))
        results["acl"] = {
            "ok": True,
            "detail": f"upn={upn} sees {len(visible)} skill(s) via grants",
            "hint": "",
        }
    except Exception as exc:  # noqa: BLE001
        results["acl"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "hint": "",
        }

    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    agent_name = os.getenv("FOUNDRY_AGENT_NAME", "").strip() or "skill-generator-agent"
    agent_version = os.getenv("FOUNDRY_AGENT_VERSION", "").strip() or "2"
    if endpoint:
        results["foundry_config"] = {
            "ok": True,
            "detail": f"endpoint set, agent={agent_name}:{agent_version}",
            "hint": "First chat turn validates the AAD credential (run `az login`).",
        }
    else:
        results["foundry_config"] = {
            "ok": False,
            "detail": "FOUNDRY_PROJECT_ENDPOINT missing",
            "hint": "Set FOUNDRY_PROJECT_ENDPOINT in .env to enable the LLM agent.",
        }

    overall_ok = all(r["ok"] for r in results.values())
    return {"ok": overall_ok, "user_upn": upn, "checks": results}


PROMPT_INDEX: list[dict[str, str]] = [
    {"filename": "00_global_system.md", "title": "Global System", "scope": "Loaded every turn (with best practices and format spec embedded)"},
    {"filename": "01_prepare.md", "title": "Prepare", "scope": "Active during Prepare"},
    {"filename": "01_prepare_scenario_addendum.md", "title": "Prepare (scenario addendum)", "scope": "Appended during Prepare for scenario skills"},
    {"filename": "02_draft.md", "title": "Draft", "scope": "Active during Draft (capability skills)"},
    {"filename": "02_draft_scenario.md", "title": "Draft (scenario)", "scope": "Replaces Draft for scenario skills"},
    {"filename": "03_refine.md", "title": "Refine", "scope": "Active during Refine"},
    {"filename": "04_test.md", "title": "Test", "scope": "Active during Test"},
    {"filename": "04_test_scenario_addendum.md", "title": "Test (scenario addendum)", "scope": "Appended during Test for scenario skills"},
    {"filename": "05_done.md", "title": "Done", "scope": "Active during Done"},
    {"filename": "09_best_practices.md", "title": "Writing Best Practices", "scope": "Embedded inside Global System"},
    {"filename": "10_format_spec.md", "title": "Skill Format Spec", "scope": "Embedded inside Global System (capability skills)"},
    {"filename": "10_format_spec_scenario.md", "title": "Scenario Skill Format Spec", "scope": "Replaces the format spec for scenario skills"},
    {"filename": "11_output_rules.md", "title": "Output Rules", "scope": "Parsed into the per-turn user prompt (output shape + rules)"},
]

STAGE_GROUPS_DESCRIPTION: list[dict[str, Any]] = [
    {"key": "PREPARE", "title": "Prepare", "purpose": "Collect materials and align scope.", "internal_stages": ["PREPARE"]},
    {"key": "DRAFT", "title": "Draft", "purpose": "Generate the first reviewable files.", "internal_stages": ["DRAFT"]},
    {"key": "REFINE", "title": "Refine", "purpose": "Apply focused patches.", "internal_stages": ["REFINE"]},
    {"key": "TEST", "title": "Test", "purpose": "Validate skill selection.", "internal_stages": ["TEST"]},
    {"key": "DONE", "title": "Done", "purpose": "Finalize or reopen the skill.", "internal_stages": ["DONE"]},
]

TOPOLOGY_RULES: list[dict[str, str]] = [
    {"rule": "T1", "description": "Frontmatter fences must start at the first line."},
    {"rule": "T2", "description": "The frontmatter block must not contain an extra fence."},
    {"rule": "T3", "description": "Frontmatter must be a valid YAML mapping."},
    {"rule": "T4", "description": "Scenario children must be a non-empty YAML list under metadata."},
    {"rule": "T5", "description": "Capability skills must not declare children."},
    {"rule": "T6", "description": "Stored and frontmatter names must match."},
    {"rule": "T7", "description": "Children must exist and cannot declare their own children."},
    {"rule": "T8", "description": "Large metadata mappings produce a warning."},
    {"rule": "T9", "description": "Scenario skills must not contain capability-only sections."},
    {"rule": "T10", "description": "Scenario skill_type and children must appear together."},
    {"rule": "P1", "description": "Every fetch_skill target must be declared in metadata.children."},
    {"rule": "P2", "description": "Every requested section name must resolve to a child's ## heading."},
    {"rule": "P3", "description": "Each skill is fetched once, naming all its sections at that call."},
    {"rule": "P4", "description": "A body with pointers must authorize fetching skills absent from list_skills."},
    {"rule": "P5", "description": "Every real skill the body names must be declared in metadata.children."},
    {"rule": "P6", "description": "The scenario must not restate a child's Required Inputs fields."},
    {"rule": "C1", "description": "No ## section name may be a substring of another after normalization."},
    {"rule": "C2", "description": "The needs-info contract must be its own fetchable ## section."},
    {"rule": "C3", "description": "Content above the first ## heading cannot be requested by name."},
]


@app.get("/api/inspect")
def inspect_agent() -> dict[str, Any]:
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    prompts: list[dict[str, str]] = []
    for entry in PROMPT_INDEX:
        path = prompts_dir / entry["filename"]
        content = path.read_text(encoding="utf-8") if path.exists() else f"<!-- Missing: {entry['filename']} -->"
        prompts.append({**entry, "content": content})
    transitions = [
        {
            "from": src.value,
            "edges": [{"to": tgt.value, "reason": reason} for tgt, reason in targets.items()],
        }
        for src, targets in TRANSITION_TABLE.items()
    ]
    common_prompts = [
        "00_global_system.md",
        "01_prepare.md",
        "03_refine.md",
        "04_test.md",
        "05_done.md",
        "09_best_practices.md",
        "11_output_rules.md",
    ]
    skill_kinds = [
        {
            "kind": SkillKind.CAPABILITY.value,
            "label": "Capability",
            "description": "A directly selectable skill that performs one bounded capability.",
            "prompts": common_prompts + ["02_draft.md", "10_format_spec.md"],
        },
        {
            "kind": SkillKind.SCENARIO.value,
            "label": "Scenario",
            "description": "A host-selected orchestration skill that delegates work to child capabilities.",
            "prompts": common_prompts + [
                "01_prepare_scenario_addendum.md",
                "02_draft_scenario.md",
                "04_test_scenario_addendum.md",
                "10_format_spec_scenario.md",
            ],
        },
    ]
    return {
        "tools": TOOL_SCHEMAS,
        "output_shape": FOUNDRY_OUTPUT_SHAPE,
        "output_rules": FOUNDRY_OUTPUT_RULES,
        "prompts": prompts,
        "stage_groups": STAGE_GROUPS_DESCRIPTION,
        "transitions": transitions,
        "skill_kinds": skill_kinds,
        "topology_rules": TOPOLOGY_RULES,
    }


@app.post("/api/patch/apply")
def apply_patch(req: ApplyPatchRequest, upn: str = Depends(require_upn)) -> ApplyPatchResponse:
    try:
        expected = req.expected_version_hash
        session = None
        if req.session_id:
            session = get_session_for_user(req.session_id, upn)
            expected = expected or version_hash(current_content(session, req.target_file))
        updated, new_hash = apply_v4a_to_content(req.current_content, req.patch, expected_version_hash=expected)
        if session is not None:
            set_current_content(session, req.target_file, updated, new_hash)
            persist_session(session)
        return ApplyPatchResponse(updated_content=updated, version_hash=new_hash)
    except PatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


if FRONTEND_DIR.exists():
    app.mount("/assets", NoCacheStaticFiles(directory=FRONTEND_DIR), name="assets")
if NODE_MODULES_DIR.exists():
    app.mount("/vendor", NoCacheStaticFiles(directory=NODE_MODULES_DIR), name="vendor")


@app.get("/")
def index():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def main() -> None:
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=6274, reload=True)


if __name__ == "__main__":
    main()
