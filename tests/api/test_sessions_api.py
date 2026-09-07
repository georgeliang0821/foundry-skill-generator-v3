from __future__ import annotations

import pytest


def test_create_list_and_read_session(client) -> None:
    created = client.post("/api/sessions", json={"mode": "new", "materials": []})

    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["current_stage"] == "prepare"

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == session_id

    read = client.get(f"/api/sessions/{session_id}")
    assert read.status_code == 200
    assert read.json()["id"] == session_id


def test_read_missing_session_returns_404(client) -> None:
    response = client.get("/api/sessions/missing")

    assert response.status_code == 404


def test_update_draft_recalculates_version_hash(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.put(
        f"/api/sessions/{session_id}/draft",
        json={
            "skill_md": "---\nname: demo-skill\n---\n",
            "version_hash": "client-supplied-stale-hash",
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["current_skill"]["skill_md"].startswith("---")
    assert body["current_skill"]["version_hash"]
    assert body["current_skill"]["version_hash"] != "client-supplied-stale-hash"


def test_update_checklist_item_keeps_session_in_prepare(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/checklist",
        json={
            "item": "identity",
            "status": "confirmed",
            "content": {"name": "demo-skill"},
            "reason": "identity confirmed",
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["current_stage"] == "prepare"
    assert body["verify_checklist"]["identity"]["status"] == "confirmed"


def test_update_unknown_checklist_item_returns_400(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/checklist",
        json={"item": "missing", "status": "confirmed"},
    )

    assert response.status_code == 400


def _seed_refine_session_with_draft(backend_main, owner="test@example.com"):
    from backend.models import Session
    session = Session(current_stage="refine", owner_upn=owner)
    session.current_skill.skill_md = "---\nname: demo-skill\ndescription: Demo\n---\nbody"
    session.current_skill.version_hash = "v-demo"
    backend_main.sessions[session.id] = session
    return session


def test_checklist_revise_keeps_stage_and_skill_md_by_default(client, backend_main) -> None:
    session = _seed_refine_session_with_draft(backend_main)

    response = client.post(
        f"/api/sessions/{session.id}/checklist",
        json={
            "item": "test_samples",
            "status": "revised",
            "content": {"positive": ["a"], "negative": ["b"]},
            "reason": "tweak samples",
        },
    )

    body = response.json()
    assert response.status_code == 200
    # Default: stay in REFINE and keep the generated SKILL.md.
    assert body["current_stage"] == "refine"
    assert body["current_skill"]["skill_md"].startswith("---\nname: demo-skill")
    assert body["verify_checklist"]["test_samples"]["content"] == {"positive": ["a"], "negative": ["b"]}


def test_checklist_revise_can_opt_into_prepare_replan(client, backend_main) -> None:
    session = _seed_refine_session_with_draft(backend_main)

    response = client.post(
        f"/api/sessions/{session.id}/checklist",
        json={
            "item": "test_samples",
            "status": "revised",
            "content": {"positive": ["a"], "negative": ["b"]},
            "reason": "replan",
            "return_to_prepare": True,
        },
    )

    body = response.json()
    assert response.status_code == 200
    # Explicit opt-in: replan from PREPARE clears the draft.
    assert body["current_stage"] == "prepare"
    assert body["current_skill"]["skill_md"] == ""

def test_update_samples_writes_brief_and_syncs_legacy(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/samples",
        json={
            "positive": ["how do I do X", ""],
            "negative": [
                {"query": "do Y", "route_to_peer": "peer-y", "why_not_this": "different goal"},
                {"query": ""},
            ],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["prepare_brief"]["positive_samples"] == ["how do I do X"]
    assert len(body["prepare_brief"]["negative_samples"]) == 1
    assert body["prepare_brief"]["negative_samples"][0]["route_to_peer"] == "peer-y"
    # Legacy mirror kept in sync for the Tests tab / older readers.
    assert body["verify_checklist"]["test_samples"]["content"]["positive"] == ["how do I do X"]


def test_update_variables_writes_brief_and_coerces(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/variables",
        json={
            "variables": [
                {"name": "API_KEY", "kind": "aca_env", "in_aca": True, "description": "auth"},
                {"name": "GRAPH", "kind": "obo_token", "in_aca": True, "description": "graph scope"},
                {"name": "RES_ID", "kind": "runtime", "description": "the resource id", "example": "res-123"},
                {
                    "name": "EAA_VERIFIED_USER_UPN",
                    "kind": "platform_identity",
                    "description": "the actor field the ticket API requires",
                },
            ]
        },
    )

    body = response.json()
    assert response.status_code == 200
    variables = body["prepare_brief"]["variables"]
    assert [v["kind"] for v in variables] == [
        "aca_env",
        "obo_token",
        "runtime",
        "platform_identity",
    ]
    assert variables[0]["name"] == "API_KEY"
    assert variables[0]["in_aca"] is True
    assert variables[2]["example"] == "res-123"
    assert variables[3]["in_aca"] is False


def test_update_variables_inaca_flip_is_silent_with_draft(client, backend_main) -> None:
    session = _seed_refine_session_with_draft(backend_main)
    client.post(
        f"/api/sessions/{session.id}/variables",
        json={"variables": [{"name": "API_KEY", "kind": "aca_env", "in_aca": False, "description": "auth"}]},
    )

    def _sys_count(s):
        return sum(1 for m in s.conversation if m.role == "system" and m.metadata.get("variables_content_changed"))

    before = _sys_count(backend_main.sessions[session.id])
    # Pure in_aca flip (add -> exists): no content change, no agent sync message.
    client.post(
        f"/api/sessions/{session.id}/variables",
        json={"variables": [{"name": "API_KEY", "kind": "aca_env", "in_aca": True, "description": "auth"}]},
    )
    assert _sys_count(backend_main.sessions[session.id]) == before
    # Description change with a draft present -> one sync message appended.
    client.post(
        f"/api/sessions/{session.id}/variables",
        json={"variables": [{"name": "API_KEY", "kind": "aca_env", "in_aca": True, "description": "auth token"}]},
    )
    assert _sys_count(backend_main.sessions[session.id]) == before + 1


def test_prepare_checkpoint_revise_cascades_downstream_pending(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    for key in ("definition_clear", "routing_uniqueness_confirmed", "variables_ok"):
        client.post(f"/api/sessions/{session_id}/checklist", json={"item": key, "status": "confirmed"})

    response = client.post(
        f"/api/sessions/{session_id}/checklist",
        json={"item": "routing_uniqueness_confirmed", "status": "revised"},
    )

    body = response.json()
    assert response.status_code == 200
    checks = body["prepare_brief"]["verify_checklist"]
    assert checks["definition_clear"] is True
    assert checks["routing_uniqueness_confirmed"] is False
    # Downstream of routing_uniqueness must revert to pending.
    assert checks["variables_ok"] is False


def test_update_samples_cascades_variables_pending(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    for key in ("routing_uniqueness_confirmed", "variables_ok"):
        client.post(f"/api/sessions/{session_id}/checklist", json={"item": key, "status": "confirmed"})

    response = client.post(
        f"/api/sessions/{session_id}/samples",
        json={"positive": ["q"], "negative": [], "revisit": True},
    )  # revisit=True: ask the agent to review -> cascades variables_ok -> pending

    body = response.json()
    assert response.status_code == 200
    # Editing samples WITH revisit invalidates the (downstream) variables checkpoint.
    assert body["prepare_brief"]["verify_checklist"]["variables_ok"] is False

def test_update_neighbors_writes_brief(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    response = client.post(
        f"/api/sessions/{session_id}/neighbors",
        json={
            "neighbor_skills": [
                {"skill": "ms-graph-todo", "axis": "personal to-do vs meeting tasks", "scenario": "manage a personal to-do list"},
                {"skill": "", "axis": "x", "scenario": "y"},
            ]
        },
    )

    body = response.json()
    assert response.status_code == 200
    neighbors = body["prepare_brief"]["understanding"]["neighbor_skills"]
    assert len(neighbors) == 1
    assert neighbors[0]["skill"] == "ms-graph-todo"
    assert neighbors[0]["scenario"] == "manage a personal to-do list"


def _seed_neighbor_skill(backend_main, grant_skill, name="ms-graph-todo"):
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name=name,
            skill_md=f"---\nname: {name}\ndescription: Neighbor\n---\n## When NOT to Use This Skill\n- nothing yet\n",
        )
    )
    grant_skill(name, description="Neighbor")


def test_neighbor_edit_propose_select_save_and_restore(client, backend_main, grant_skill) -> None:
    _seed_neighbor_skill(backend_main, grant_skill)
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    # Propose an edited version (adds a backlink line).
    edited = "---\nname: ms-graph-todo\ndescription: Neighbor\n---\n## When NOT to Use This Skill\n- Managing meeting follow-up tasks -> use `meeting-action-items`\n"
    body = client.post(
        f"/api/sessions/{session_id}/neighbor-edits/ms-graph-todo/propose",
        json={"skill_md": edited, "label": "backlink v1", "origin": "agent_proposed"},
    ).json()
    ne = next(n for n in body["neighbor_edits"] if n["skill_name"] == "ms-graph-todo")
    # versions[0] is the original snapshot, versions[1] is the proposed edit.
    assert ne["versions"][0]["origin"] == "original"
    assert len(ne["versions"]) == 2
    assert ne["status"] == "draft"
    original_vid = ne["versions"][0]["version_id"]
    edited_vid = ne["versions"][1]["version_id"]
    assert ne["selected_version_id"] == edited_vid

    # Save the selected (edited) version to Blob.
    client.post(f"/api/sessions/{session_id}/neighbor-edits/ms-graph-todo/save", json={})
    saved = client.get("/api/skills/ms-graph-todo").json()
    assert "meeting-action-items" in saved["skill_md"]

    # Restore: select the original version and save again.
    client.post(
        f"/api/sessions/{session_id}/neighbor-edits/ms-graph-todo/select",
        json={"version_id": original_vid},
    )
    client.post(f"/api/sessions/{session_id}/neighbor-edits/ms-graph-todo/save", json={})
    restored = client.get("/api/skills/ms-graph-todo").json()
    assert "meeting-action-items" not in restored["skill_md"]
    assert "nothing yet" in restored["skill_md"]


def test_neighbor_edit_unknown_skill_returns_404(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    response = client.post(
        f"/api/sessions/{session_id}/neighbor-edits/does-not-exist/propose",
        json={"skill_md": "x", "label": "v"},
    )
    assert response.status_code == 404



def test_propose_neighbor_edit_is_deferred_until_accept(client, backend_main, grant_skill) -> None:
    # Seed a neighbor skill the agent will propose an edit for.
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="peer-skill",
            skill_md="---\nname: peer-skill\ndescription: Peer\n---\n## When NOT to Use This Skill\n- nothing\n",
        )
    )
    grant_skill("peer-skill", description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    # apply_tool_effect is now a no-op for propose_neighbor_edit (interactive):
    # nothing happens until the user accepts the chat card.
    patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@ ## When NOT to Use This Skill\n"
        "- nothing\n"
        "+ Doing the new thing -> use `new-skill`\n"
        "*** End Patch"
    )
    backend_main.apply_tool_effect(
        session,
        "propose_neighbor_edit",
        {"skill_name": "peer-skill", "patch": patch, "label": "add backlink"},
    )
    assert not session.neighbor_edits  # deferred, not applied

    # Accepting applies it: original snapshot kept + a new agent_proposed version.
    backend_main._apply_neighbor_edit_proposal(
        session, {"skill_name": "peer-skill", "patch": patch, "label": "add backlink"}
    )
    ne = next(n for n in session.neighbor_edits if n.skill_name == "peer-skill")
    assert ne.versions[0].origin == "original"
    assert len(ne.versions) == 2
    assert ne.versions[1].origin == "agent_proposed"
    assert ne.selected_version_id == ne.versions[1].version_id
    assert ne.status == "draft"


def test_propose_neighbor_edit_accept_via_tool_result(client, backend_main, grant_skill) -> None:
    from backend.models import PendingToolCall

    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="peer-accept",
            skill_md="---\nname: peer-accept\ndescription: Peer\n---\n## When NOT to Use This Skill\n- nothing\n",
        )
    )
    grant_skill("peer-accept", description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@ ## When NOT to Use This Skill\n"
        "- nothing\n"
        "+ New thing -> use `new-skill`\n"
        "*** End Patch"
    )
    call = PendingToolCall(tool="propose_neighbor_edit", args={"skill_name": "peer-accept", "patch": patch})
    session.pending_tool_calls.append(call)

    resp = client.post(
        f"/api/sessions/{session_id}/tool-result",
        json={"tool_call_id": call.call_id, "result": {"action": "accept"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    ne = next(n for n in body["neighbor_edits"] if n["skill_name"] == "peer-accept")
    assert len(ne["versions"]) == 2
    assert "new-skill" in ne["versions"][1]["skill_md"]
    # The pending call is consumed.
    assert all(c["call_id"] != call.call_id for c in body["pending_tool_calls"])


def test_propose_neighbor_edit_helper_unloadable_raises_404(client, backend_main) -> None:
    from fastapi import HTTPException

    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    minimal_patch = "*** Begin Patch\n*** Update File: SKILL.md\n@@ anchor\n- a\n+ b\n*** End Patch"
    with pytest.raises(HTTPException) as exc:
        backend_main._apply_neighbor_edit_proposal(session, {"skill_name": "does-not-exist", "patch": minimal_patch})
    assert exc.value.status_code == 404



def _seed_neighbor_for_patch(backend_main, grant_skill, name="peer-patch"):
    md = "---\nname: " + name + "\ndescription: Peer\n---\n## When NOT to Use This Skill\n- nothing yet\n"
    backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md=md))
    grant_skill(name, description="Peer")
    return md


def test_propose_neighbor_edit_applies_v4a_patch(client, backend_main, grant_skill) -> None:
    _seed_neighbor_for_patch(backend_main, grant_skill, "peer-patch")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@ ## When NOT to Use This Skill\n"
        "- nothing yet\n"
        "+ Doing the new thing -> use `new-skill`\n"
        "*** End Patch"
    )
    backend_main._apply_neighbor_edit_proposal(
        session,
        {"skill_name": "peer-patch", "patch": patch, "label": "add backlink"},
    )

    ne = next(n for n in session.neighbor_edits if n.skill_name == "peer-patch")
    assert ne.versions[0].origin == "original"
    assert len(ne.versions) == 2
    # The patch was applied to the original content.
    assert "new-skill" in ne.versions[1].skill_md
    assert "nothing yet" not in ne.versions[1].skill_md
    assert ne.status == "draft"


def test_propose_neighbor_edit_patch_miss_raises_no_fallback(client, backend_main, grant_skill) -> None:
    _seed_neighbor_for_patch(backend_main, grant_skill, "peer-miss")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    bad_patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@ ## No Such Heading\n"
        "- not present\n"
        "+ whatever\n"
        "*** End Patch"
    )
    # Full rewrites were removed: a patch whose anchor does not match ALWAYS
    # raises PatchError (recoverable -> the agent regenerates a wider patch).
    # The original snapshot is created on first access but NO proposed version is
    # added, and a stray skill_md is ignored (no silent full rewrite).
    from backend.patch import PatchError

    with pytest.raises(PatchError):
        backend_main._apply_neighbor_edit_proposal(
            session, {"skill_name": "peer-miss", "patch": bad_patch}
        )
    with pytest.raises(PatchError):
        backend_main._apply_neighbor_edit_proposal(
            session,
            {"skill_name": "peer-miss", "patch": bad_patch, "skill_md": "ignored full rewrite"},
        )
    ne = next(n for n in session.neighbor_edits if n.skill_name == "peer-miss")
    assert len(ne.versions) == 1
    assert ne.versions[0].origin == "original"


def test_load_selected_neighbor_full_md(client, backend_main, grant_skill) -> None:
    from backend.models import NeighborSkill

    _seed_neighbor_for_patch(backend_main, grant_skill, "peer-full")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    session.prepare_brief.understanding.neighbor_skills = [
        NeighborSkill(skill="peer-full", axis="a", scenario="s"),
        NeighborSkill(skill="does-not-exist", axis="a", scenario="s"),
    ]

    backend_main._load_selected_neighbor_full_md(session)

    # The loadable neighbor's full SKILL.md is present; the missing one is skipped.
    assert "peer-full" in session.neighbor_full_md
    assert "When NOT to Use" in session.neighbor_full_md["peer-full"]
    assert "does-not-exist" not in session.neighbor_full_md



def test_neighbor_edit_open_loads_original_for_user_editing(client, backend_main, grant_skill) -> None:
    md = "---\nname: peer-open\ndescription: Peer\n---\n## When NOT to Use This Skill\n- nothing\n"
    backend_main.store.save_skill(backend_main.SkillFiles(name="peer-open", skill_md=md))
    grant_skill("peer-open", description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]

    body = client.post(f"/api/sessions/{session_id}/neighbor-edits/peer-open/open", json={}).json()

    ne = next(n for n in body["neighbor_edits"] if n["skill_name"] == "peer-open")
    # The original is loaded as the only version; user can now edit + propose.
    assert len(ne["versions"]) == 1
    assert ne["versions"][0]["origin"] == "original"
    assert "nothing" in ne["versions"][0]["skill_md"]
    assert ne["status"] == "saved"


def test_neighbor_edit_open_unknown_skill_returns_404(client) -> None:
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    response = client.post(f"/api/sessions/{session_id}/neighbor-edits/nope/open", json={})
    assert response.status_code == 404

def test_tests_tab_save_updates_authoritative_brief_samples(client) -> None:
    """The Tests-tab "Save samples" (POST /checklist item=test_samples) must update
    the Prepare Brief samples that test runs actually read -- not only the legacy
    VerifyChecklist content -- otherwise a saved edit is silently ignored."""
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    # Seed the brief with samples, as the PREPARE flow would.
    client.post(
        f"/api/sessions/{sid}/samples",
        json={
            "positive": ["old positive"],
            "negative": [{"query": "old negative", "route_to_peer": "peer-x", "why_not_this": "because"}],
        },
    )
    # The user edits samples in the Tests tab (negatives arrive as plain strings).
    r = client.post(
        f"/api/sessions/{sid}/checklist",
        json={
            "item": "test_samples",
            "status": "revised",
            "content": {"positive": ["new positive"], "negative": ["new negative 1", "new negative 2"]},
            "return_to_prepare": False,
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Authoritative source (read by test runs) is updated.
    assert body["prepare_brief"]["positive_samples"] == ["new positive"]
    assert [n["query"] for n in body["prepare_brief"]["negative_samples"]] == ["new negative 1", "new negative 2"]
    # Legacy content stays in sync.
    assert body["verify_checklist"]["test_samples"]["content"]["negative"] == ["new negative 1", "new negative 2"]

def test_samples_revisit_flag_controls_cascade(client) -> None:
    """A plain "just update samples" save (revisit=False) keeps an already-confirmed
    routing checkpoint; revisit=True cascades it back to pending."""
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    # Confirm routing + the downstream variables checkpoint first.
    for key in ("routing_uniqueness_confirmed", "variables_ok"):
        client.post(f"/api/sessions/{sid}/checklist", json={"item": key, "status": "confirmed"})
    # Just update samples -> downstream variables_ok stays confirmed.
    r1 = client.post(
        f"/api/sessions/{sid}/samples",
        json={"positive": ["q1"], "negative": [{"query": "n1"}], "revisit": False},
    )
    assert r1.status_code == 200
    assert r1.json()["prepare_brief"]["verify_checklist"]["variables_ok"] is True
    # Ask the agent to review -> downstream variables_ok cascades back to pending.
    r2 = client.post(
        f"/api/sessions/{sid}/samples",
        json={"positive": ["q1", "q2"], "negative": [{"query": "n1"}], "revisit": True},
    )
    assert r2.status_code == 200
    assert r2.json()["prepare_brief"]["verify_checklist"]["variables_ok"] is False

def test_request_positive_samples_writes_brief(client, backend_main) -> None:
    """Submitting the in-chat positive-samples card persists the positives into the
    Prepare Brief (keeping existing negatives) so the right panel + runner see them."""
    from backend.models import PendingToolCall

    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    # Seed an existing negative so we can prove it is preserved.
    from backend.models import NegativeSample
    session.prepare_brief.negative_samples = [NegativeSample(query="keep me", route_to_peer="peer-x", why_not_this="reason")]

    call = PendingToolCall(tool="request_positive_samples", args={})
    session.pending_tool_calls.append(call)

    resp = client.post(
        f"/api/sessions/{session_id}/tool-result",
        json={"tool_call_id": call.call_id, "result": {"positive": ["q one", "q two", "  "]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prepare_brief"]["positive_samples"] == ["q one", "q two"]
    # Existing negatives are preserved.
    assert [n["query"] for n in body["prepare_brief"]["negative_samples"]] == ["keep me"]
    assert all(c["call_id"] != call.call_id for c in body["pending_tool_calls"])

def test_neighbors_revisit_reopens_routing_and_downstream(client) -> None:
    """Changing the neighbor skills (revisit=True) re-opens the routing block it
    belongs to AND every dependent downstream block for step-by-step re-review."""
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    for key in ("definition_clear", "routing_uniqueness_confirmed", "variables_ok"):
        client.post(f"/api/sessions/{sid}/checklist", json={"item": key, "status": "confirmed"})
    # revisit=False -> no checkpoint change.
    r0 = client.post(
        f"/api/sessions/{sid}/neighbors",
        json={"neighbor_skills": [{"skill": "peer-a", "axis": "x", "scenario": "y"}], "revisit": False},
    )
    vc0 = r0.json()["prepare_brief"]["verify_checklist"]
    assert vc0["routing_uniqueness_confirmed"] is True and vc0["variables_ok"] is True
    # revisit=True -> routing + downstream variables re-open; definition stays.
    r1 = client.post(
        f"/api/sessions/{sid}/neighbors",
        json={"neighbor_skills": [{"skill": "peer-b", "axis": "x", "scenario": "y"}], "revisit": True},
    )
    vc1 = r1.json()["prepare_brief"]["verify_checklist"]
    assert vc1["definition_clear"] is True
    assert vc1["routing_uniqueness_confirmed"] is False
    assert vc1["variables_ok"] is False

def test_neighbor_prompt_injection_prefers_selected_version(client, backend_main, grant_skill) -> None:
    """The agent prompt must show the edit card's SELECTED (latest) version, not
    the Blob original, so its V4A patch lands on the same text the backend applies
    it to (otherwise the anchor never matches once a neighbor was edited)."""
    from backend.models import NeighborSkill, NeighborEdit, NeighborVersion

    backend_main.store.save_skill(backend_main.SkillFiles(name="peer-x", skill_md="ORIGINAL blob content\n"))
    grant_skill("peer-x", description="Peer")
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[sid]
    session.prepare_brief.understanding.neighbor_skills = [NeighborSkill(skill="peer-x", axis="a", scenario="s")]

    v0 = NeighborVersion(label="Original", skill_md="ORIGINAL blob content\n", origin="original")
    v1 = NeighborVersion(label="Edit 1", skill_md="EDITED latest content\n", origin="user_edited")
    session.neighbor_edits = [NeighborEdit(
        skill_name="peer-x", versions=[v0, v1], selected_version_id=v1.version_id, saved_version_id=v0.version_id,
    )]

    backend_main._load_selected_neighbor_full_md(session)
    assert session.neighbor_full_md["peer-x"] == "EDITED latest content\n"


def test_neighbor_prompt_injection_falls_back_to_blob_when_no_edit(client, backend_main, grant_skill) -> None:
    from backend.models import NeighborSkill

    backend_main.store.save_skill(backend_main.SkillFiles(name="peer-y", skill_md="BLOB original y\n"))
    grant_skill("peer-y", description="Peer")
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[sid]
    session.prepare_brief.understanding.neighbor_skills = [NeighborSkill(skill="peer-y", axis="a", scenario="s")]
    session.neighbor_edits = []

    backend_main._load_selected_neighbor_full_md(session)
    assert session.neighbor_full_md["peer-y"] == "BLOB original y\n"

def test_propose_neighbor_edit_patch_is_indentation_tolerant(client, backend_main, grant_skill) -> None:
    """A V4A patch applies even when the agent does not reproduce the file's exact
    YAML/Markdown indentation in its context / `-` lines."""
    name = "peer-fr"
    md = (
        "---\nname: peer-fr\ndescription: >\n"
        "  Search and find content across Microsoft 365.\n"
        "---\n## When NOT to Use This Skill\n- nothing yet\n"
    )
    backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md=md))
    grant_skill(name, description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    # The `-` line is NOT indented, but the file line is indented by 2 spaces --
    # lenient matching must still apply the edit.
    patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@\n"
        "-Search and find content across Microsoft 365.\n"
        "+  Search and locate content across Microsoft 365 by keyword.\n"
        "*** End Patch"
    )
    backend_main._apply_neighbor_edit_proposal(
        session, {"skill_name": name, "patch": patch, "label": "tighten description"}
    )
    ne = next(n for n in session.neighbor_edits if n.skill_name == name)
    assert len(ne.versions) == 2
    assert "locate content" in ne.versions[1].skill_md
    assert "find content across Microsoft 365." not in ne.versions[1].skill_md


def test_propose_neighbor_edit_patch_not_found_raises(client, backend_main, grant_skill) -> None:
    from backend.patch import PatchError

    name = "peer-fr2"
    backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md="---\nname: peer-fr2\n---\n- a\n"))
    grant_skill(name, description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]
    patch = (
        "*** Begin Patch\n"
        "*** Update File: SKILL.md\n"
        "@@\n"
        "-does not exist anywhere\n"
        "+x\n"
        "*** End Patch"
    )
    with pytest.raises(PatchError):
        backend_main._apply_neighbor_edit_proposal(session, {"skill_name": name, "patch": patch})
    ne = next(n for n in session.neighbor_edits if n.skill_name == name)
    assert len(ne.versions) == 1


def test_propose_neighbor_edit_falls_back_across_versions(client, backend_main, grant_skill) -> None:
    """When the agent's patch context was copied from an EARLIER version (e.g. the
    pristine Original) and no longer matches the currently selected/edited version,
    the apply falls back across versions and still succeeds instead of raising an
    anchor-not-found error."""
    name = "peer-multi"
    md = "---\nname: peer-multi\n---\nline a\nhello world\nline b\n"
    backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md=md))
    grant_skill(name, description="Peer")
    session_id = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    session = backend_main.sessions[session_id]

    def patch_for(repl):
        return (
            "*** Begin Patch\n"
            "*** Update File: SKILL.md\n"
            "@@\n"
            "-hello world\n"
            f"+{repl}\n"
            "*** End Patch"
        )

    # Edit #1: rewrite the "hello world" line -> selected version no longer has it.
    backend_main._apply_neighbor_edit_proposal(session, {"skill_name": name, "patch": patch_for("HELLO")})
    ne = next(n for n in session.neighbor_edits if n.skill_name == name)
    assert len(ne.versions) == 2
    assert "hello world" not in ne.versions[1].skill_md

    # Edit #2: agent's patch still references the ORIGINAL text. It misses the
    # selected version but matches the Original -> fallback applies it.
    backend_main._apply_neighbor_edit_proposal(session, {"skill_name": name, "patch": patch_for("WORLD2")})
    assert len(ne.versions) == 3
    assert "WORLD2" in ne.versions[2].skill_md
