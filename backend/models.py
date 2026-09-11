"""Pydantic models for Skill Generator v2."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now_iso() -> str:
    """Return a stable UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


class Stage(str, Enum):
    PREPARE = "prepare"
    DRAFT = "draft"
    REFINE = "refine"
    TEST = "test"
    DONE = "done"


# Mapping for migrating persisted legacy sessions (8-stage uppercase) to new 5-stage.
LEGACY_STAGE_MAP: dict[str, str] = {
    "INTAKE": "prepare",
    "RESEARCH": "prepare",
    "VERIFY": "prepare",
    "DRAFT": "draft",
    "REFINE": "refine",
    "TEST": "test",
    "ITERATE": "refine",
    "DONE": "done",
}


class Mode(str, Enum):
    NEW = "new"
    IMPORT = "import"
    MODIFY = "modify"


class SkillKind(str, Enum):
    """Which layer of the two-tier skill topology a session is authoring.

    Orthogonal to Mode: any Mode can target either kind.
    """

    CAPABILITY = "capability"
    SCENARIO = "scenario"


SCENARIO_SKILL_TYPE = "scenario-orchestration"


class MaterialKind(str, Enum):
    """Only CODE, API_SPEC and TEXT are offered in the UI -- one per fidelity tier.

    FILE, EXISTING_SKILL and URL are retired aliases retained so sessions persisted
    before the picker was reduced still deserialize.
    """

    CODE = "code"
    API_SPEC = "api_spec"
    URL = "url"
    FILE = "file"
    EXISTING_SKILL = "existing_skill"
    TEXT = "text"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class Material(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: MaterialKind = MaterialKind.TEXT
    content: str
    created_at: str = Field(default_factory=utc_now_iso)


class MaterialUpsertRequest(BaseModel):
    kind: MaterialKind = MaterialKind.TEXT
    content: str


class ChatMessage(BaseModel):
    role: MessageRole
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class SkillDraft(BaseModel):
    skill_md: str = ""
    version_hash: str = ""


class ChecklistItem(BaseModel):
    status: Literal["pending", "confirmed", "revised"] = "pending"
    content: Any = None


class VerifyChecklist(BaseModel):
    """Legacy checklist kept for backward compatibility with persisted sessions."""

    understanding: ChecklistItem = Field(default_factory=ChecklistItem)
    differentiation: ChecklistItem = Field(default_factory=ChecklistItem)
    identity: ChecklistItem = Field(default_factory=ChecklistItem)
    metadata: ChecklistItem = Field(default_factory=ChecklistItem)
    io_spec: ChecklistItem = Field(default_factory=ChecklistItem)
    env_vars: ChecklistItem = Field(default_factory=ChecklistItem)
    test_samples: ChecklistItem = Field(default_factory=ChecklistItem)

    def all_confirmed(self) -> bool:
        return all(
            item.status == "confirmed"
            for item in (
                self.understanding,
                self.differentiation,
                self.identity,
                self.metadata,
                self.io_spec,
                self.env_vars,
                self.test_samples,
            )
        )


# ---------------------------------------------------------------------------
# v6 PREPARE-stage briefs (new in 5-stage refactor)
# ---------------------------------------------------------------------------


class NeighborSkill(BaseModel):
    """A similar existing skill and how the new skill is distinguished from it.

    Single source for the routing_uniqueness flow: ``axis`` feeds the description
    contrast, ``scenario`` feeds both the When NOT to Use routing line and the
    derived negative sample, and ``skill`` is the peer to route to instead.
    """

    skill: str = ""        # neighbor skill name (the peer to route to)
    axis: str = ""         # the distinguishing axis (区分轴) -> description contrast
    scenario: str = ""     # the neighbor's scenario (鄰居场景) -> When NOT to Use + negative sample


class UnderstandingBrief(BaseModel):
    # The goal of the SKILL being built (read by a coding agent that selects one
    # skill per request). Renamed from the legacy ``user_goal``; the validator
    # below migrates persisted sessions and accepts the old key.
    skill_goal: str = ""
    input_sources: list[str] = Field(default_factory=list)  # data/inputs the skill consumes
    key_capabilities: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    differentiation: str = ""  # vs existing skills; required for quality gate
    neighbor_skills: list[NeighborSkill] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_user_goal(cls, data: Any) -> Any:
        """Accept the legacy ``user_goal`` key and map it to ``skill_goal``."""
        if isinstance(data, dict) and "skill_goal" not in data and "user_goal" in data:
            data = dict(data)
            data["skill_goal"] = data.pop("user_goal")
        return data


class ExistingSkillOverlap(BaseModel):
    name: str
    path: str = ""
    similarity: float = 0.0  # 0..1
    overlap_areas: list[str] = Field(default_factory=list)
    differentiation_required: bool = False


class ResearchBrief(BaseModel):
    web_status: Literal["ok", "failed", "skipped"] = "skipped"
    error: str | None = None
    summary: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    adjacent_skills: list[dict[str, Any]] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    recommended_apis: list[dict[str, Any]] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    existing_skills_overlap: list[ExistingSkillOverlap] = Field(default_factory=list)
    aca_status: Literal["ok", "failed", "skipped"] = "skipped"
    aca_findings: dict[str, Any] = Field(default_factory=dict)
    # Boundary digests of the skills the current user can access (granted in SQL
    # AND present in Blob). Loaded in parallel on PREPARE entry. Each item:
    # {name, description, when_to_use, when_not_to_use, granted}. Powers
    # differentiation, the new skill's When NOT to Use router, and old-skill
    # modification suggestions.
    peer_skills: list[dict[str, Any]] = Field(default_factory=list)
    peer_skills_status: Literal["ok", "partial", "failed", "skipped"] = "skipped"
    # Why the list is empty/incomplete. Empty when nothing went wrong -- an empty
    # peer_skills with an empty error genuinely means "no granted peers".
    peer_skills_error: str = ""

    @field_validator("sources", "adjacent_skills", "recommended_apis", "peer_skills", mode="before")
    @classmethod
    def _coerce_named_dict_items(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({"name": str(item)})
        return normalized


_DEFAULT_PREPARE_CHECKLIST: dict[str, bool] = {
    # 3-block PREPARE flow. Order matters: each builds on the prior.
    # routing_uniqueness merges the old uniqueness + samples checkpoints because
    # negative samples are derived from the same neighbor/differentiation finding.
    "definition_clear": False,             # goal + input sources + capabilities + neighbor_skills
    "routing_uniqueness_confirmed": False, # neighbor compare + description contrast + mutual-exclusion + When NOT to Use + samples
    "variables_ok": False,                 # aca_env + obo_token + runtime variables confirmed
}

# A scenario skill declares no variables of its own -- the third block confirms
# what is delegated to the child capability skills instead.
_SCENARIO_PREPARE_CHECKLIST: dict[str, bool] = {
    "definition_clear": False,
    "routing_uniqueness_confirmed": False,
    "delegation_ok": False,                # children + credentials key + host capabilities + handshakes
}

_CURRENT_CHECKLIST_KEYS: frozenset[str] = frozenset(_DEFAULT_PREPARE_CHECKLIST) | frozenset(
    _SCENARIO_PREPARE_CHECKLIST
)


def prepare_checklist_for(kind: "SkillKind | str") -> dict[str, bool]:
    """Return a fresh PREPARE checklist for the given skill kind."""
    if SkillKind(kind) is SkillKind.SCENARIO:
        return dict(_SCENARIO_PREPARE_CHECKLIST)
    return dict(_DEFAULT_PREPARE_CHECKLIST)

# Legacy checklist keys -> current 3-key checklist. A new key is confirmed only
# when every legacy source key that is present was itself confirmed.
_LEGACY_CHECKLIST_MERGE: dict[str, tuple[str, ...]] = {
    # Same-named keys come first so an already-current checklist is preserved.
    "definition_clear": ("definition_clear", "scope_clear", "users_clear", "capabilities_listed"),
    "routing_uniqueness_confirmed": (
        "routing_uniqueness_confirmed",
        "uniqueness_confirmed",
        "samples_confirmed",
        "adjacent_skills_checked",
        "differentiation_written",
        "no_duplicate_skill",
    ),
    "variables_ok": ("variables_ok", "env_vars_ok"),
}


def _migrate_prepare_checklist(brief: dict[str, Any]) -> None:
    """In-place migrate a persisted prepare_brief dict from the legacy 8-key
    checklist to the merged 4-key form. Idempotent."""
    if not isinstance(brief, dict):
        return
    checklist = brief.get("verify_checklist")
    if not isinstance(checklist, dict):
        return
    if set(checklist).issubset(_CURRENT_CHECKLIST_KEYS):
        base = (
            _SCENARIO_PREPARE_CHECKLIST
            if "delegation_ok" in checklist
            else _DEFAULT_PREPARE_CHECKLIST
        )
        for key in base:
            checklist.setdefault(key, False)
        return
    evidence = brief.get("verify_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    updated = brief.get("verify_updated_at")
    updated = updated if isinstance(updated, dict) else {}
    new_checklist: dict[str, bool] = {}
    new_evidence: dict[str, str] = {}
    new_updated: dict[str, str] = {}
    for new_key, sources in _LEGACY_CHECKLIST_MERGE.items():
        present = [s for s in sources if s in checklist]
        new_checklist[new_key] = bool(present) and all(checklist.get(s) for s in present)
        parts = [f"{s}: {evidence[s]}" for s in sources if evidence.get(s)]
        if parts:
            new_evidence[new_key] = "\n".join(parts)
        stamps = [updated[s] for s in sources if updated.get(s)]
        if stamps:
            new_updated[new_key] = max(stamps)
    brief["verify_checklist"] = new_checklist
    brief["verify_evidence"] = new_evidence
    brief["verify_updated_at"] = new_updated


class NegativeSample(BaseModel):
    """A structured negative routing sample.

    A query the SAME kind of user might say that is SIMILAR to this skill but
    should route to a different (peer) skill. Generated by the agent from the
    differentiation result; the user can edit it. Doubles as concrete evidence
    of differentiation.
    """

    query: str = ""
    route_to_peer: str = ""  # the peer skill this query should route to instead
    why_not_this: str = ""  # why it must NOT select the current skill


class SkillVariable(BaseModel):
    """A variable the skill depends on, confirmed in the variables_ok checkpoint.

    kind:
      - ``aca_env``: an ACA environment variable read at deploy time. ``in_aca``
        is the deployment status (True = already exists, False = must be added);
        it does NOT change the SKILL.md text. Missing at runtime is a deployment
        error -> non-zero exit.
      - ``obo_token``: an OBO token variable injected by the OBO exchange.
        ``in_aca`` True = already registered in OBO_SCOPE_REGISTRY. Missing means
        the OBO chain is broken -> non-zero exit.
      - ``runtime``: a value that differs on every user query (resource id, file
        name, target URL, ...) -- inferred from the query/positive samples or
        asked from the user. Documented as a Required Input; when missing the
        script prints ``[NEEDS_INFO] missing=...`` and exits 0.
      - ``platform_identity``: the platform-injected verified actor string. The
        only legal name is ``EAA_VERIFIED_USER_UPN``. It is neither an ACA
        variable the deployment sets nor a caller input: the platform injects it
        after a successful OBO exchange and strips any caller-supplied value, so
        absence means the identity was never verified -> non-zero exit. Declare
        it ONLY when a downstream interface demands the actor's UPN/email/alias
        as a STRING; when the downstream accepts an OBO resource token instead,
        that token is the identity and this variable has no place in the skill.
    """

    name: str = ""
    kind: Literal["aca_env", "obo_token", "runtime", "platform_identity"] = "runtime"
    in_aca: bool = False  # aca_env/obo_token only: True = already exists
    description: str = ""  # what it is and, for runtime, how to obtain it
    example: str = ""  # example value (mainly runtime); never copied into SKILL.md
    required: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_variable(cls, data: Any) -> Any:
        """Migrate older variable shapes to the current kind/in_aca form.
        Idempotent. Handles (1) the legacy 3-value ``type`` (env_reuse/env_add/
        runtime) with source/reason, and (2) the phase-2 ``kind='env'`` form."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "type" in d:
            legacy = d.pop("type", None)
            if "kind" not in d:
                d["kind"] = "aca_env" if legacy in ("env_reuse", "env_add") else "runtime"
            if "in_aca" not in d:
                d["in_aca"] = legacy == "env_reuse"
            if not d.get("description"):
                parts = [str(d.pop("source", "")).strip(), str(d.pop("reason", "")).strip()]
                d["description"] = " -- ".join(p for p in parts if p)
            else:
                d.pop("source", None)
                d.pop("reason", None)
        # Phase-2 kind='env' -> aca_env.
        if d.get("kind") == "env":
            d["kind"] = "aca_env"
        return d


class Delegation(BaseModel):
    """What a scenario skill hands to ONE child capability skill.

    A scenario skill declares no variables of its own: everything it needs is
    either a host capability it performs itself, or a payload it delegates.
    """

    child_skill: str = ""
    # The single `credentials` key the host puts the serialized payload under.
    credentials_key: str = ""
    # The child's `##` section names this scenario points at with `fetch_skill`.
    # The field contract lives in those sections and is never copied here.
    sections: list[str] = Field(default_factory=list)
    # Things the HOST must do itself because the child cannot (e.g. read the
    # user's calendar). These replace `input_sources` for a scenario skill.
    host_capabilities: list[str] = Field(default_factory=list)
    # The child operations this scenario drives. One the child supports but this
    # list omits is a capability the host can never reach through this scenario.
    operations: list[str] = Field(default_factory=list)
    # Round trips the child can demand before it completes, e.g.
    # "needs_input missing=LEAVE_CONFLICT_ACK -> ask the user, resend same session_id".
    handshakes: list[str] = Field(default_factory=list)


class PrepareBrief(BaseModel):
    understanding: UnderstandingBrief = Field(default_factory=UnderstandingBrief)
    research: ResearchBrief = Field(default_factory=ResearchBrief)
    verify_checklist: dict[str, bool] = Field(
        default_factory=lambda: dict(_DEFAULT_PREPARE_CHECKLIST)
    )
    verify_evidence: dict[str, str] = Field(default_factory=dict)
    verify_updated_at: dict[str, str] = Field(default_factory=dict)
    # Routing test samples (same source as the checkpoint, not the legacy
    # VerifyChecklist). positive = user-authored queries that SHOULD route here.
    positive_samples: list[str] = Field(default_factory=list)
    negative_samples: list[NegativeSample] = Field(default_factory=list)
    # Three kinds of variables confirmed in variables_ok.
    variables: list[SkillVariable] = Field(default_factory=list)
    # Scenario skills only: confirmed in delegation_ok, one entry per child.
    delegation: list[Delegation] = Field(default_factory=list)
    revisit: bool = False
    last_updated: int | str | None = None

    @field_validator("verify_checklist", mode="before")
    @classmethod
    def _normalize_checklist(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if set(value).issubset(_CURRENT_CHECKLIST_KEYS):
            # delegation_ok only exists in the scenario checklist, so its presence
            # is enough to tell the two current shapes apart without the kind.
            base = (
                _SCENARIO_PREPARE_CHECKLIST
                if "delegation_ok" in value
                else _DEFAULT_PREPARE_CHECKLIST
            )
            merged = dict(base)
            merged.update({k: bool(v) for k, v in value.items()})
            return merged
        wrapper = {"verify_checklist": dict(value)}
        _migrate_prepare_checklist(wrapper)
        return wrapper["verify_checklist"]


class IterationReflection(BaseModel):
    reflection_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=utc_now_iso)
    test_run_id: str = ""
    what_went_wrong: list[str] = Field(default_factory=list)
    what_to_change: list[str] = Field(default_factory=list)
    confidence_delta: float = 0.0  # -1.0 .. +1.0
    raw: str = ""  # full text, never truncated in storage


class NeighborVersion(BaseModel):
    """One snapshot in a neighbor skill's edit history (session-scoped)."""

    version_id: str = Field(default_factory=lambda: uuid4().hex)
    label: str = ""
    skill_md: str = ""
    version_hash: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    origin: Literal["original", "agent_proposed", "user_edited"] = "agent_proposed"


class NeighborEdit(BaseModel):
    """Versioned edit history for ONE neighbor skill, kept on the session.

    versions[0] is always the ``original`` snapshot loaded before any edit, so
    the user can always restore it. Nothing is written to Blob until the user
    explicitly Saves the selected version.
    """

    skill_name: str
    versions: list[NeighborVersion] = Field(default_factory=list)
    selected_version_id: str = ""
    saved_version_id: str = ""  # last version synced to Blob (empty = never saved)
    status: Literal["draft", "saved"] = "draft"


class PatchRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    version_hash_before: str = ""
    version_hash_after: str = ""
    target_file: Literal["SKILL.md"]
    v4a_patch: str
    content_before: str = ""
    content_after: str = ""
    applied_at: str = Field(default_factory=utc_now_iso)
    applied_by: Literal["agent", "user"] = "user"
    reason: str = ""
    # Verbatim IterationReflection.what_to_change entries this patch closes.
    addresses: list[str] = Field(default_factory=list)


class TestResult(BaseModel):
    query: str
    expected_skill: str | None = None
    actual_skill: str | None = None
    # Every skill the runtime reported routing to (APIM top-level
    # ``skills_referenced``). The authoritative routing signal -- the response
    # prose "Skill used:" line can disagree with it.
    skills_referenced: list[str] = Field(default_factory=list)
    # UI only -- this never enters the agent prompt. Always [] for skills
    # authored here (single file, schema and sample code inline); non-empty only
    # for an imported skill that ships real resource files next to SKILL.md.
    loaded_resources: list[str] = Field(default_factory=list)
    passed: bool | None = None
    reasoning: str = ""
    apim_response: str = ""
    apim_status: str = ""
    apim_session_id: str = ""
    apim_uploads: list[str] = Field(default_factory=list)
    apim_raw_response: dict[str, Any] = Field(default_factory=dict)
    # Lint findings against ``apim_response`` -- the script the runtime WROTE
    # from the body prose, which is a different artifact from the sample code
    # the file ships. Computed once at run time: recomputing it later would
    # report the current SKILL.md against a script an older one produced.
    prepared_code_lint: list[dict[str, Any]] = Field(default_factory=list)
    request_sent: str = ""
    tokens: int | None = None
    duration_ms: int | None = None
    error: str | None = None


class ScenarioLayerResult(BaseModel):
    """One layer of the scenario test ladder, cheapest first.

    L1 is static and sends nothing; L2 and L3 each hit the runner. A layer only
    runs when the previous one passed, so ``passed=None`` means "not reached".
    """

    layer: Literal["L1", "L2", "L3"]
    title: str = ""
    passed: bool | None = None
    details: list[str] = Field(default_factory=list)
    # On failure this must name EVERY plausible cause, not the likeliest one.
    diagnosis: str = ""
    results: list[TestResult] = Field(default_factory=list)


class TestRun(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    skill_version_hash: str = ""
    positive_results: list[TestResult] = Field(default_factory=list)
    negative_results: list[TestResult] = Field(default_factory=list)
    positive_hit_rate: float = 0.0
    negative_correct_reject_rate: float = 0.0
    # Scenario skills only; empty for a capability run.
    scenario_layers: list[ScenarioLayerResult] = Field(default_factory=list)
    # Things the run could NOT establish. Shown verbatim next to the results so
    # a green run is never mistaken for a stronger claim than it is.
    notes: list[str] = Field(default_factory=list)
    ran_at: str = Field(default_factory=utc_now_iso)


class PendingToolCall(BaseModel):
    call_id: str = Field(default_factory=lambda: f"call_{uuid4().hex}")
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class Session(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=lambda: uuid4().hex)
    mode: Mode = Mode.NEW
    # Sessions persisted before the two-tier topology existed are capability
    # sessions, which is exactly what the default gives them.
    skill_kind: SkillKind = SkillKind.CAPABILITY
    target_skill_id: str | None = None
    remote_skill_id: str | None = None
    remote_version_hash: str = ""
    blob_store_id: str = ""
    current_stage: Stage = Stage.PREPARE
    owner_upn: str = ""
    materials: list[Material] = Field(default_factory=list)
    current_skill: SkillDraft = Field(default_factory=SkillDraft)
    verify_checklist: VerifyChecklist = Field(default_factory=VerifyChecklist)
    prepare_brief: PrepareBrief = Field(default_factory=PrepareBrief)
    iteration_reflections: list[IterationReflection] = Field(default_factory=list)
    patch_history: list[PatchRecord] = Field(default_factory=list)
    neighbor_edits: list[NeighborEdit] = Field(default_factory=list)
    # Transient: full current SKILL.md of the SELECTED neighbor skills, loaded
    # best-effort so the agent can edit them without asking the user for the
    # text. Excluded from persistence (re-derived from the store each turn).
    neighbor_full_md: dict[str, str] = Field(default_factory=dict, exclude=True)
    # Scenario skills only: every skill this scenario may use. The host treats
    # this as a whitelist, so a skill omitted here simply never materializes.
    # It is wider than the set of children actually delegated to.
    children: list[str] = Field(default_factory=list)
    # Transient full SKILL.md of each child, same contract as neighbor_full_md.
    child_full_md: dict[str, str] = Field(default_factory=dict, exclude=True)
    # Transient `##` heading index of every declared child, so the agent can
    # write section pointers instead of guessing names.
    child_sections: dict[str, list[str]] = Field(default_factory=dict, exclude=True)
    test_runs: list[TestRun] = Field(default_factory=list)
    conversation: list[ChatMessage] = Field(default_factory=list)
    pending_tool_calls: list[PendingToolCall] = Field(default_factory=list)
    research_summary: str = ""
    aca_env_result: dict[str, Any] | None = None
    aca_env_error: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def _align_checklist_to_kind(self) -> "Session":
        """Keep the PREPARE checklist keys in step with ``skill_kind``.

        The checklist validator cannot see the kind, so a session that switched
        kinds (or was persisted before scenarios existed) would otherwise carry
        keys its quality gate never reads.
        """
        expected = prepare_checklist_for(self.skill_kind)
        current = self.prepare_brief.verify_checklist
        if set(current) != set(expected):
            self.prepare_brief.verify_checklist = {
                key: bool(current.get(key, default)) for key, default in expected.items()
            }
        return self

    def touch(self) -> None:
        self.updated_at = utc_now_iso()


def migrate_legacy_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate a persisted legacy session dict to the v6 5-stage schema.

    Idempotent: returns ``raw`` unchanged if already in new format.
    """

    if not isinstance(raw, dict):
        return raw
    data = dict(raw)
    stage = data.get("current_stage")
    if isinstance(stage, str) and stage in LEGACY_STAGE_MAP:
        data["current_stage"] = LEGACY_STAGE_MAP[stage]
    elif isinstance(stage, str) and stage.upper() in LEGACY_STAGE_MAP and stage not in {s.value for s in Stage}:
        data["current_stage"] = LEGACY_STAGE_MAP[stage.upper()]
    pb = data.setdefault("prepare_brief", PrepareBrief().model_dump())
    if isinstance(pb, dict):
        _migrate_prepare_checklist(pb)
    data.setdefault("iteration_reflections", [])
    return data


class SessionSummary(BaseModel):
    id: str
    mode: Mode
    skill_kind: SkillKind = SkillKind.CAPABILITY
    target_skill_id: str | None = None
    current_stage: Stage
    owner_upn: str = ""
    title: str = ""
    created_at: str
    updated_at: str
    message_count: int = 0
    material_count: int = 0
    pending_tool_count: int = 0


def delegation_children(session: "Session") -> list[str]:
    """The children this scenario actually delegates work to.

    A narrower set than ``session.children``, which is the whitelist of every
    skill the scenario may use. Only these are capability skills owned by this
    parent; the rest are pre-existing skills the scenario merely consumes.
    """
    return list(
        dict.fromkeys(
            name
            for name in (
                (d.child_skill or "").strip()
                for d in session.prepare_brief.delegation or []
            )
            if name
        )
    )


class CreateSessionRequest(BaseModel):
    mode: Mode = Mode.NEW
    skill_kind: SkillKind | None = None
    target_skill_id: str | None = None
    materials: list[Material] = Field(default_factory=list)

class ChatRequest(BaseModel):
    message: str = ""
    materials: list[Material] = Field(default_factory=list)
    auto_continue: bool = False
    auto_depth: int = 0


class ToolResultRequest(BaseModel):
    tool_call_id: str
    result: dict[str, Any] = Field(default_factory=dict)


class SaveSessionSkillRequest(BaseModel):
    name: str | None = None
    orphan_action: Literal["keep_internal", "make_capability"] | None = None


class ChildrenUpdateRequest(BaseModel):
    children: list[str] = Field(default_factory=list)


class RunSessionTestRequest(BaseModel):
    source: Literal["current", "remote"] = "current"


class ChecklistUpdateRequest(BaseModel):
    item: str
    content: Any = None
    status: Literal["pending", "confirmed", "revised"] = "revised"
    reason: str = ""
    # When False (default) a checklist edit updates the item in place and keeps
    # the current stage + generated SKILL.md. Set True only when the user
    # explicitly wants to replan from PREPARE (which clears the draft).
    return_to_prepare: bool = False


class SamplesUpdateRequest(BaseModel):
    """User-driven edit of the PREPARE routing samples.

    revisit=False (default) means "just update the samples": persist them for
    later testing WITHOUT marking routing_uniqueness_confirmed (and downstream)
    pending and without any agent message. revisit=True means the edit may change
    the requirements, so the routing checkpoint cascade fires and the caller also
    sends the agent a re-review message.
    """

    positive: list[str] = Field(default_factory=list)
    negative: list[NegativeSample] = Field(default_factory=list)
    revisit: bool = False


class NeighborsUpdateRequest(BaseModel):
    """User-driven edit of the neighbor_skills single source.

    revisit=True means the list actually changed, so the routing block (which
    owns neighbor_skills) and every dependent downstream block are re-opened for
    step-by-step re-confirmation, and the caller also messages the agent.
    """

    neighbor_skills: list[NeighborSkill] = Field(default_factory=list)
    revisit: bool = False


class NeighborProposeRequest(BaseModel):
    """Push a new version into a neighbor skill's session edit history."""

    skill_md: str
    label: str = ""
    origin: Literal["agent_proposed", "user_edited"] = "user_edited"


class NeighborSelectRequest(BaseModel):
    """Select which version of a neighbor edit is active (preview / to-save)."""

    version_id: str


class VariablesUpdateRequest(BaseModel):
    """User-driven edit of the PREPARE skill variables (no agent message)."""

    variables: list[SkillVariable] = Field(default_factory=list)


class SkillIndexEntry(BaseModel):
    name: str
    description: str = ""
    version_hash: str = ""
    blob_store_id: str = ""


class SkillFiles(BaseModel):
    name: str
    skill_md: str = ""
    version_hash: str = ""
    blob_path: str = ""


class ApplyPatchRequest(BaseModel):
    session_id: str | None = None
    target_file: Literal["SKILL.md"]
    current_content: str
    patch: str
    expected_version_hash: str = ""


class ApplyPatchResponse(BaseModel):
    updated_content: str
    version_hash: str
    applied: bool = True


class SkillTestRequest(BaseModel):
    skill_content: str = ""
    positive_samples: list[str] = Field(default_factory=list)
    negative_samples: list[str] = Field(default_factory=list)


class StageTransition(BaseModel):
    target: Stage
    summary: str = ""


class SSEEvent(BaseModel):
    event: str
    data: dict[str, Any] = Field(default_factory=dict)
