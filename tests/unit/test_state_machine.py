from __future__ import annotations

import pytest

from backend.models import (
    ChatMessage,
    Delegation,
    IterationReflection,
    Material,
    MaterialKind,
    Mode,
    MessageRole,
    PatchRecord,
    Session,
    SkillKind,
    Stage,
    TestResult as ModelTestResult,
    TestRun as ModelTestRun,
)
from backend.state_machine import (
    TRANSITION_TABLE,
    QualityGateError,
    _format_latest_test_run,
    build_system_prompt,
    check_quality_gates,
    open_fix_items,
    transition,
)


def _fully_prepared_session() -> Session:
    """Helper: build a session that passes all PREPARE -> DRAFT quality gates."""
    s = Session(current_stage=Stage.PREPARE)
    s.prepare_brief.understanding.skill_goal = "Help users summarize their meeting notes."
    s.prepare_brief.understanding.input_sources = ["meeting transcript text"]
    s.prepare_brief.understanding.key_capabilities = ["summarize", "extract action items"]
    s.prepare_brief.understanding.differentiation = "Focuses on meeting transcripts, not general docs."
    s.prepare_brief.research.web_status = "ok"
    s.prepare_brief.research.summary = "No directly competing skill exists."
    s.prepare_brief.research.adjacent_skills = [{"name": "doc-summarizer", "similarity": 0.3}]
    s.prepare_brief.research.aca_status = "ok"
    for key in s.prepare_brief.verify_checklist:
        s.prepare_brief.verify_checklist[key] = True
    return s


def _fully_prepared_scenario() -> Session:
    session = _fully_prepared_session()
    session = Session.model_validate({
        **session.model_dump(mode="json"),
        "skill_kind": SkillKind.SCENARIO,
        "children": ["child-skill"],
        "prepare_brief": {
            **session.prepare_brief.model_dump(mode="json"),
            "delegation": [Delegation(
                child_skill="child-skill",
                credentials_key="payload_json",
                host_capabilities=["Collect the user's request"],
            ).model_dump(mode="json")],
        },
    })
    session.child_full_md = {
        "child-skill": "---\nname: child-skill\ndescription: Child\n---\n\n# Child\n",
    }
    for key in session.prepare_brief.verify_checklist:
        session.prepare_brief.verify_checklist[key] = True
    return session


def test_all_declared_stage_transitions_are_valid() -> None:
    for source, targets in TRANSITION_TABLE.items():
        for target in targets:
            if source is Stage.PREPARE and target is Stage.DRAFT:
                session = _fully_prepared_session()
            else:
                session = Session(current_stage=source)
            transition(session, target, "covered by transition table")
            assert session.current_stage == target.value


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (Stage.PREPARE, Stage.REFINE),
        (Stage.PREPARE, Stage.DONE),
        (Stage.DRAFT, Stage.TEST),
        (Stage.DRAFT, Stage.DONE),
    ],
)
def test_invalid_stage_transitions_are_rejected(source: Stage, target: Stage) -> None:
    session = Session(current_stage=source)
    with pytest.raises(ValueError, match="Invalid stage transition"):
        transition(session, target, "not allowed")


def test_prepare_to_draft_blocked_by_quality_gate() -> None:
    session = Session(current_stage=Stage.PREPARE)
    with pytest.raises(QualityGateError) as exc:
        transition(session, Stage.DRAFT, "premature")
    assert exc.value.missing  # at least one missing item
    # Session must NOT have advanced.
    assert session.current_stage == Stage.PREPARE.value


def test_prepare_to_draft_allowed_when_all_gates_pass() -> None:
    session = _fully_prepared_session()
    assert check_quality_gates(session) == []
    transition(session, Stage.DRAFT, "ready")
    assert session.current_stage == Stage.DRAFT.value


def test_any_to_prepare_sets_revisit_and_clears_draft() -> None:
    session = _fully_prepared_session()
    transition(session, Stage.DRAFT, "ready")
    session.current_skill.skill_md = "---\nname: demo\n---\nbody"
    session.current_skill.version_hash = "abc"
    transition(session, Stage.PREPARE, "scope change")
    assert session.current_stage == Stage.PREPARE.value
    assert session.prepare_brief.revisit is True
    assert session.current_skill.skill_md == ""


def test_prompt_assembly_includes_stage_and_runtime_state() -> None:
    session = Session(current_stage=Stage.PREPARE)
    prompt = build_system_prompt(session)
    assert "Skill Generator v2" in prompt
    assert "stage: prepare" in prompt.lower()
    assert "## Blob Skill Binding" in prompt




def test_system_prompt_uses_documented_assembly_order() -> None:
    session = Session(current_stage=Stage.PREPARE)

    prompt = build_system_prompt(session)

    sections = [
        "# Skill Generator v2 - Global System",
        "# Stage: PREPARE",
        "# Writing Best Practices",
        "# Skill Format Spec",
        "## Runtime State",
        "## Blob Skill Binding",
        "## Allowed Stage Transitions",
    ]
    positions = [prompt.index(section) for section in sections]
    assert positions == sorted(positions)
    assert prompt.rstrip().endswith(
        "Pick the transition whose reason matches the actual situation; do not invent new edges."
    )


def test_session_model_coerces_string_research_items() -> None:
    raw = Session().model_dump(mode="json")
    raw["prepare_brief"]["research"]["adjacent_skills"] = ["Azure Search helper"]
    raw["prepare_brief"]["research"]["recommended_apis"] = ["Responses API"]

    session = Session.model_validate(raw)

    assert session.prepare_brief.research.adjacent_skills == [{"name": "Azure Search helper"}]
    assert session.prepare_brief.research.recommended_apis == [{"name": "Responses API"}]

def test_prompt_tolerates_string_research_items() -> None:
    session = Session(current_stage=Stage.PREPARE)
    session.prepare_brief.understanding.skill_goal = "Build rich RAG guidance."
    session.prepare_brief.understanding.key_capabilities = ["answer", "implementation guidance"]
    session.prepare_brief.understanding.differentiation = "Focuses on Azure Search and Responses API integration."
    session.prepare_brief.research.web_status = "ok"
    session.prepare_brief.research.summary = "Research captured."
    session.prepare_brief.research.adjacent_skills = ["azure-search-helper"]
    session.prepare_brief.research.recommended_apis = ["Azure AI Search", {"name": "Responses API", "why": "answer synthesis"}]

    prompt = build_system_prompt(session)

    assert "azure-search-helper" in prompt
    assert "Azure AI Search" in prompt
    assert "Responses API: answer synthesis" in prompt

def test_prompt_includes_latest_test_run_for_test_stage() -> None:
    session = Session(current_stage=Stage.TEST)
    session.test_runs.append(
        ModelTestRun(
            skill_version_hash="abc123",
            positive_results=[
                ModelTestResult(query="Help me", expected_skill="demo", actual_skill="demo", passed=True)
            ],
            negative_results=[
                ModelTestResult(query="Joke", expected_skill=None, actual_skill=None, passed=True)
            ],
            positive_hit_rate=1.0,
            negative_correct_reject_rate=1.0,
        )
    )
    prompt = build_system_prompt(session)
    assert "## Latest Test Run" in prompt
    assert "positive_hit_rate: 100%" in prompt


def test_prompt_carries_prepared_code_whole() -> None:
    # Regression: the response used to be sliced at 1200 chars, which hid every
    # os.environ read (they sit near the end) from the declared-vs-read check.
    script = 'import os\n\n' + ('# pad\n' * 400) + 'X = os.environ.get("hr_leave_json")\n'
    assert len(script) > 1200
    session = Session(current_stage=Stage.TEST)
    session.test_runs.append(
        ModelTestRun(
            skill_version_hash="abc123",
            positive_results=[
                ModelTestResult(
                    query="How many days left",
                    expected_skill="demo",
                    actual_skill="demo",
                    passed=True,
                    apim_response=script,
                )
            ],
            positive_hit_rate=1.0,
        )
    )
    prompt = build_system_prompt(session)
    assert "### Prepared code" in prompt
    assert 'os.environ.get("hr_leave_json")' in prompt


def test_prompt_omits_code_from_samples_routed_elsewhere() -> None:
    session = Session(current_stage=Stage.TEST)
    session.test_runs.append(
        ModelTestRun(
            skill_version_hash="abc123",
            negative_results=[
                ModelTestResult(
                    query="Book a room",
                    expected_skill=None,
                    actual_skill="other-skill",
                    passed=True,
                    apim_response="SOMEONE_ELSES_CODE = 1",
                )
            ],
            negative_correct_reject_rate=1.0,
        )
    )
    prompt = build_system_prompt(session)
    assert "SOMEONE_ELSES_CODE" not in prompt
    # Assert on the section itself: the TEST stage prompt names "### Prepared
    # code" in its instructions, so a substring check on the whole prompt hits.
    assert "### Prepared code" not in (_format_latest_test_run(session) or "")


def test_prompt_dedupes_identical_prepared_code() -> None:
    script = "import os\nX = os.environ.get('a')\n"
    session = Session(current_stage=Stage.TEST)
    session.test_runs.append(
        ModelTestRun(
            skill_version_hash="abc123",
            positive_results=[
                ModelTestResult(query="q1", expected_skill="demo", actual_skill="demo", passed=True, apim_response=script),
                ModelTestResult(query="q2", expected_skill="demo", actual_skill="demo", passed=True, apim_response=script),
            ],
            positive_hit_rate=1.0,
        )
    )
    prompt = build_system_prompt(session)
    assert prompt.count("X = os.environ.get('a')") == 1
    assert "From: q1; q2" in prompt


def test_prompt_includes_done_reentry_guidance() -> None:
    session = Session(current_stage=Stage.DONE)
    prompt = build_system_prompt(session)
    assert "Reopen" in prompt or "DONE" in prompt or "re-entry" in prompt.lower()


def test_prompt_includes_mode_addenda() -> None:
    import_session = Session(mode=Mode.IMPORT)
    modify_session = Session(mode=Mode.MODIFY)
    assert "Import" in build_system_prompt(import_session)
    assert "Modify" in build_system_prompt(modify_session)


def test_session_store_title_uses_first_user_message(backend_main) -> None:
    session = Session()
    session.conversation.append(ChatMessage(role=MessageRole.USER, content="  Build a calendar sync skill  "))
    summary = backend_main.session_store.summary(session)
    assert summary.title == "Build a calendar sync skill"

def test_verify_checklist_keys_match_default_order() -> None:
    from backend.models import _DEFAULT_PREPARE_CHECKLIST, Session
    from backend.state_machine import _checklist_keys

    assert _checklist_keys(Session()) == tuple(_DEFAULT_PREPARE_CHECKLIST)


def test_routing_uniqueness_replaces_uniqueness_and_samples() -> None:
    from backend.models import _DEFAULT_PREPARE_CHECKLIST

    assert list(_DEFAULT_PREPARE_CHECKLIST) == [
        "definition_clear",
        "routing_uniqueness_confirmed",
        "variables_ok",
    ]


def test_peer_skills_drop_declared_children() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.children = ["hr-leave-requests"]
    session.prepare_brief.research.peer_skills = [
        {"name": "hr-leave-requests", "description": "Files leave requests"},
        {"name": "ms-graph-calendar", "description": "Reads the calendar"},
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert "### ms-graph-calendar" in rendered
    assert "### hr-leave-requests" not in rendered
    assert "Excluded as delegated children" in rendered


def test_peer_skills_report_when_every_peer_is_a_declared_child() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.children = ["hr-leave-requests"]
    session.prepare_brief.research.peer_skills = [
        {"name": "hr-leave-requests", "description": "Files leave requests"},
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert "hr-leave-requests" in rendered
    assert "Excluded as delegated children" in rendered


def test_peer_skills_are_untouched_without_children() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE)
    session.prepare_brief.research.peer_skills = [
        {"name": "ms-graph-calendar", "description": "Reads the calendar"},
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert "### ms-graph-calendar" in rendered
    assert "Excluded as delegated children" not in rendered


def _peer(name: str, **extra) -> dict:
    return {"name": name, "description": f"{name} does things", **extra}


def test_capability_peers_split_parents_into_the_cross_layer_section() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.CAPABILITY)
    session.prepare_brief.research.peer_skills = [
        _peer("leave-workflow", children=["hr-leave-system"]),
        _peer("ms-graph-calendar"),
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)
    rivals, cross = rendered.split("## Cross-layer skills (NOT routing rivals)")

    assert "### ms-graph-calendar" in rivals
    assert "### leave-workflow" not in rivals
    assert "### leave-workflow" in cross
    assert "backend candidate set" in cross


def test_scenario_peers_split_internal_children_into_the_cross_layer_section() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.prepare_brief.research.peer_skills = [
        _peer("hr-leave-system", is_internal=True),
        _peer("ms-graph-calendar"),
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)
    rivals, cross = rendered.split("## Cross-layer skills (NOT routing rivals)")

    assert "### ms-graph-calendar" in rivals
    assert "### hr-leave-system" not in rivals
    assert "### hr-leave-system" in cross
    assert "host directory" in cross


def test_scenario_peers_treat_an_unclaimed_child_as_cross_layer() -> None:
    """is_internal is only stamped once a parent is saved, so the peer's own
    children list is the earlier signal."""
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.prepare_brief.research.peer_skills = [
        _peer("leave-workflow", children=["hr-leave-system"]),
        _peer("hr-leave-system", is_internal=False),
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)
    rivals, cross = rendered.split("## Cross-layer skills (NOT routing rivals)")

    assert "### leave-workflow" in rivals
    assert "### hr-leave-system" in cross


def test_scenario_peers_keep_standalone_capabilities_as_rivals() -> None:
    """Both layers share the host directory, so a scenario really does compete
    with a standalone capability skill."""
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.prepare_brief.research.peer_skills = [_peer("ms-graph-calendar")]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert "### ms-graph-calendar" in rendered
    assert "## Cross-layer skills" not in rendered


def test_peers_loaded_before_the_layer_split_stay_rivals() -> None:
    from backend.state_machine import _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.CAPABILITY)
    session.prepare_brief.research.peer_skills = [
        {"name": "leave-workflow", "description": "Orchestrates leave"},
    ]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert "### leave-workflow" in rendered
    assert "## Cross-layer skills" not in rendered


def test_cross_layer_peers_share_the_render_cap() -> None:
    from backend.state_machine import _PEER_RENDER_CAP, _format_peer_skills

    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.CAPABILITY)
    session.prepare_brief.research.peer_skills = [
        _peer(f"rival-{i}") for i in range(_PEER_RENDER_CAP)
    ] + [_peer("leave-workflow", children=["hr-leave-system"])]
    session.prepare_brief.research.peer_skills_status = "ok"

    rendered = _format_peer_skills(session)

    assert rendered.count("### rival-") == _PEER_RENDER_CAP
    assert "### leave-workflow" not in rendered


def test_revising_definition_resets_the_scenario_delegation_checkpoint(backend_main) -> None:
    session = Session(current_stage=Stage.PREPARE, skill_kind=SkillKind.SCENARIO)
    session.prepare_brief.verify_checklist["delegation_ok"] = True

    affected = backend_main._cascade_prepare_pending(session, "definition_clear")

    assert affected == ["delegation_ok"]
    assert session.prepare_brief.verify_checklist["delegation_ok"] is False


def test_quality_gate_requires_variables_ok() -> None:
    session = _fully_prepared_session()
    assert check_quality_gates(session) == []

    session.prepare_brief.verify_checklist["variables_ok"] = False
    missing = check_quality_gates(session)
    assert "verify_checklist.variables_ok" in missing


def test_scenario_quality_gate_requires_host_capabilities() -> None:
    session = _fully_prepared_scenario()
    session.prepare_brief.delegation[0].host_capabilities = []

    assert "delegation.host_capabilities" in check_quality_gates(session)


@pytest.mark.parametrize(
    ("child_md", "missing_key"),
    [
        (None, "children.unresolved:child-skill"),
        ("---\nname: other-skill\n---\n", "children.name_mismatch:child-skill"),
        (
            "---\nname: child-skill\nmetadata:\n  children: [grandchild]\n---\n",
            "children.nested:child-skill",
        ),
    ],
)
def test_scenario_quality_gate_validates_child_topology(child_md: str | None, missing_key: str) -> None:
    session = _fully_prepared_scenario()
    session.child_full_md = {} if child_md is None else {"child-skill": child_md}

    assert missing_key in check_quality_gates(session)


def test_scenario_prompt_uses_scenario_draft_and_format_spec() -> None:
    scenario = Session(current_stage=Stage.DRAFT, skill_kind=SkillKind.SCENARIO)
    capability = Session(current_stage=Stage.DRAFT)

    scenario_prompt = build_system_prompt(scenario)
    capability_prompt = build_system_prompt(capability)

    assert "scenario-orchestration" in scenario_prompt
    assert "# Stage: DRAFT (scenario skill)" in scenario_prompt
    assert "# Stage: DRAFT (scenario skill)" not in capability_prompt
    assert "scenario-orchestration" not in capability_prompt


def test_runtime_state_says_children_are_still_undeclared() -> None:
    prompt = build_system_prompt(Session(skill_kind=SkillKind.SCENARIO))

    assert "children: (none declared yet" in prompt


def test_runtime_state_lists_declared_children() -> None:
    session = Session(skill_kind=SkillKind.SCENARIO, children=["child-skill", "mailer"])

    prompt = build_system_prompt(session)

    assert "children: child-skill, mailer" in prompt


def test_runtime_state_stays_silent_about_children_for_a_capability() -> None:
    assert "children:" not in build_system_prompt(Session())


def _with_material(kind: MaterialKind, content: str) -> Session:
    session = Session(current_stage=Stage.DRAFT)
    session.materials.append(Material(id="m1", kind=kind, content=content))
    return session


def test_code_materials_are_delivered_in_full_under_the_verbatim_policy() -> None:
    session = _with_material(MaterialKind.CODE, "def submit_leave():\n    return 1\n")

    prompt = build_system_prompt(session)

    assert "## Materials" in prompt
    assert "<<<BEGIN MATERIAL m1 (kind=code)>>>" in prompt
    assert "def submit_leave():" in prompt
    assert "Tier 1 -- REPRODUCE, DO NOT PARAPHRASE" in prompt
    # The tiers the session has no material for must not be advertised.
    assert "Tier 3 -- CONTEXT ONLY" not in prompt


def test_prose_materials_only_carry_the_context_only_policy() -> None:
    session = _with_material(MaterialKind.TEXT, "Employees get 14 days of annual leave.")

    prompt = build_system_prompt(session)

    assert "Tier 3 -- CONTEXT ONLY" in prompt
    assert "Tier 1 -- REPRODUCE, DO NOT PARAPHRASE" not in prompt


def test_no_materials_means_no_materials_block() -> None:
    prompt = build_system_prompt(Session(current_stage=Stage.DRAFT))

    assert "<<<BEGIN MATERIAL" not in prompt
    assert "Tier 1 -- REPRODUCE, DO NOT PARAPHRASE" not in prompt


def _reflected_session(*items: str) -> Session:
    session = Session(current_stage=Stage.REFINE)
    session.iteration_reflections.append(
        IterationReflection(what_to_change=list(items), raw="reflection")
    )
    return session


def _record_patch(session: Session, *addresses: str, applied_at: str | None = None) -> None:
    record = PatchRecord(target_file="SKILL.md", v4a_patch="", addresses=list(addresses))
    if applied_at:
        record.applied_at = applied_at
    session.patch_history.append(record)


def test_open_fix_items_are_empty_without_a_reflection() -> None:
    assert open_fix_items(Session(current_stage=Stage.REFINE)) == ([], [])


def test_open_fix_items_close_only_what_a_patch_addresses() -> None:
    session = _reflected_session("fix one", "fix two", "fix three")
    _record_patch(session, "fix two")

    closed, still_open = open_fix_items(session)

    assert closed == ["fix two"]
    assert still_open == ["fix one", "fix three"]


def test_open_fix_items_match_on_normalized_whitespace_and_case() -> None:
    session = _reflected_session("Fix   one")
    _record_patch(session, "fix one")

    assert open_fix_items(session) == (["Fix   one"], [])


def test_open_fix_items_ignore_patches_applied_before_the_reflection() -> None:
    session = _reflected_session("fix one")
    _record_patch(session, "fix one", applied_at="2000-01-01T00:00:00+00:00")

    assert open_fix_items(session) == ([], ["fix one"])


def test_open_fix_items_only_track_the_latest_reflection() -> None:
    session = _reflected_session("old fix")
    session.iteration_reflections.append(
        IterationReflection(what_to_change=["new fix"], raw="second")
    )

    assert open_fix_items(session) == ([], ["new fix"])


def test_prompt_carries_the_open_fix_list_in_refine() -> None:
    session = _reflected_session("fix one", "fix two")
    _record_patch(session, "fix one")

    prompt = build_system_prompt(session)

    assert "## Open Fix List" in prompt
    assert "1 of 2 fix item(s)" in prompt
    assert "- [x] fix one" in prompt
    assert "- [ ] fix two" in prompt


def test_prompt_omits_the_open_fix_list_outside_refine_and_test() -> None:
    session = _reflected_session("fix one")
    session.current_stage = Stage.DONE

    assert "## Open Fix List" not in build_system_prompt(session)