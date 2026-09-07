"""API tests for v7 RLS: grants endpoints + skill list filtering + session ownership."""
from __future__ import annotations

from tests.conftest import TEST_UPN



# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


def test_skills_list_requires_auth(anon_client):
    r = anon_client.get("/api/skills")
    assert r.status_code == 401


def test_sessions_list_requires_auth(anon_client):
    r = anon_client.get("/api/sessions")
    assert r.status_code == 401


def test_session_create_requires_auth(anon_client):
    r = anon_client.post("/api/sessions", json={"mode": "new", "materials": []})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Skill list filtering: SQL grant AND blob exist
# ---------------------------------------------------------------------------


def test_skills_list_filters_by_grant(client, backend_main, grant_skill, fake_sql):
    # Put two skills in blob, only grant one to the test user.
    for name in ("alpha-skill", "beta-skill"):
        backend_main.store.save_skill(
            backend_main.SkillFiles(
                name=name,
                skill_md=f"---\nname: {name}\ndescription: d\n---\n",
            )
        )
    grant_skill("alpha-skill")
    # beta-skill is in blob and in fake_sql (no grant) -- should NOT appear.
    fake_sql.upsert_skill("beta-skill")

    r = client.get("/api/skills")
    assert r.status_code == 200
    names = [item["name"] for item in r.json()]
    assert "alpha-skill" in names
    assert "beta-skill" not in names


def test_skills_list_excludes_sql_only_skill(client, backend_main, grant_skill, fake_sql):
    # SQL has the skill + grant, but Blob does not -> intersection drops it.
    grant_skill("ghost-skill")
    r = client.get("/api/skills")
    names = [item["name"] for item in r.json()]
    assert "ghost-skill" not in names


def test_skills_list_uses_frontmatter_description(client, backend_main, grant_skill):
    # Schema v2 dropped dbo.skills.description; frontmatter is the only source.
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="desc-skill",
            skill_md="---\nname: desc-skill\ndescription: From frontmatter\n---\n",
        )
    )
    grant_skill("desc-skill")
    r = client.get("/api/skills")
    desc_item = next(item for item in r.json() if item["name"] == "desc-skill")
    assert desc_item["description"] == "From frontmatter"


def test_public_skill_visible_without_grant(client, backend_main, fake_sql):
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="public-skill",
            skill_md="---\nname: public-skill\ndescription: d\n---\n",
        )
    )
    fake_sql.upsert_skill("public-skill", is_public=True)
    r = client.get("/api/skills")
    item = next(i for i in r.json() if i["name"] == "public-skill")
    assert item["is_public"] is True
    assert not any(key[1] == "public-skill" for key in fake_sql.grants)


# ---------------------------------------------------------------------------
# Per-skill read/modify ACL
# ---------------------------------------------------------------------------


def test_load_skill_denied_without_grant(client, backend_main):
    # Skill exists in blob but no SQL row + no grant.
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="locked",
            skill_md="---\nname: locked\n---\n",
        )
    )
    r = client.get("/api/skills/locked")
    assert r.status_code == 403


def test_load_skill_allowed_with_grant(client, backend_main, grant_skill):
    backend_main.store.save_skill(
        backend_main.SkillFiles(
            name="ok-skill",
            skill_md="---\nname: ok-skill\n---\n",
        )
    )
    grant_skill("ok-skill")
    r = client.get("/api/skills/ok-skill")
    assert r.status_code == 200


def test_modify_session_denied_without_grant(client, backend_main):
    backend_main.store.save_skill(
        backend_main.SkillFiles(name="locked-mod", skill_md="---\nname: locked-mod\n---\n")
    )
    r = client.post(
        "/api/sessions",
        json={"mode": "modify", "target_skill_id": "locked-mod", "materials": []},
    )
    # Should be 404 (not leak existence).
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Save flow: blob + SQL + self-grant
# ---------------------------------------------------------------------------


def test_save_creates_sql_row_and_self_grant(client, backend_main, fake_sql):
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{sid}/draft",
        json={"skill_md": "---\nname: new-saver\ndescription: Hello\n---\n"},
    )
    r = client.post(f"/api/sessions/{sid}/save", json={"name": "new-saver"})
    assert r.status_code == 200
    # SQL row created.
    assert "new-saver" in fake_sql.skills
    assert fake_sql.skills["new-saver"]["is_public"] is False
    # Self-grant created.
    assert (TEST_UPN, "new-saver") in fake_sql.grants
    # User can now list it.
    listed = client.get("/api/skills").json()
    assert any(item["name"] == "new-saver" for item in listed)


# ---------------------------------------------------------------------------
# Grants endpoints
# ---------------------------------------------------------------------------


def test_grants_list_returns_my_grant(client, backend_main, grant_skill):
    grant_skill("shared")
    r = client.get("/api/skills/shared/grants")
    assert r.status_code == 200
    upns = [g["user_upn"] for g in r.json()["grants"]]
    assert TEST_UPN in upns


def test_grants_list_denied_without_grant(client):
    r = client.get("/api/skills/private/grants")
    assert r.status_code == 403


def test_add_grant_succeeds(client, backend_main, grant_skill, fake_sql):
    grant_skill("teamskill")
    r = client.post(
        "/api/skills/teamskill/grants",
        json={"user_upn": "newuser@x.test"},
    )
    assert r.status_code == 200
    assert ("newuser@x.test", "teamskill") in fake_sql.grants


def test_add_grant_requires_user_upn(client, grant_skill):
    grant_skill("blank-target")
    r = client.post("/api/skills/blank-target/grants", json={"user_upn": ""})
    assert r.status_code == 400


def test_remove_grant_succeeds(client, backend_main, grant_skill, fake_sql):
    grant_skill("multi-share")
    # add another user first
    fake_sql.add_grant("multi-share", "second@x", granted_by="x")
    r = client.delete(f"/api/skills/multi-share/grants/second@x")
    assert r.status_code == 200
    assert ("second@x", "multi-share") not in fake_sql.grants


def test_cannot_remove_last_grant_for_self(client, grant_skill):
    grant_skill("only-mine")
    r = client.delete(f"/api/skills/only-mine/grants/{TEST_UPN}")
    assert r.status_code == 409


def test_refresh_skills_cache_returns_200(client, grant_skill):
    grant_skill("any")
    r = client.post("/api/skills/refresh")
    assert r.status_code == 200
    assert r.json()["refreshed"] is True


# ---------------------------------------------------------------------------
# Visibility (is_public)
# ---------------------------------------------------------------------------


def test_saving_a_skill_never_publishes_it(client, backend_main, fake_sql):
    """Saving is a content action; POST /save has no say over visibility.

    Publishing is a second, explicit PATCH. The self-grant the save created is
    left behind on purpose -- v_my_skills is `grant OR is_public`, so an inert
    grant on a public skill costs nothing and keeps the demote path working.
    """
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{sid}/draft",
        json={"skill_md": "---\nname: open-skill\ndescription: Hello\n---\n"},
    )
    r = client.post(f"/api/sessions/{sid}/save", json={"name": "open-skill", "is_public": True})
    assert r.status_code == 200
    assert fake_sql.skills["open-skill"]["is_public"] is False
    assert (TEST_UPN, "open-skill") in fake_sql.grants

    r = client.patch("/api/skills/open-skill/visibility", json={"is_public": True})
    assert r.status_code == 200
    assert fake_sql.skills["open-skill"]["is_public"] is True
    assert (TEST_UPN, "open-skill") in fake_sql.grants


def test_a_later_content_save_never_rewrites_visibility(client, backend_main, fake_sql):
    """Locks the invariant that makes the split safe.

    save_skill_dual_write is also called by the patch and rename auto-syncs,
    which carry no visibility intent. Give it any say over is_public and
    flipping a skill from the grants modal gets silently undone by the next
    edit. This asserts the observable end of that: content saves are inert.
    """
    sid = client.post("/api/sessions", json={"mode": "new", "materials": []}).json()["id"]
    client.put(
        f"/api/sessions/{sid}/draft",
        json={"skill_md": "---\nname: drifter\ndescription: Hello\n---\n"},
    )
    client.post(f"/api/sessions/{sid}/save", json={"name": "drifter"})
    client.patch("/api/skills/drifter/visibility", json={"is_public": True})

    session = backend_main.get_session_for_user(sid, TEST_UPN)
    backend_main.save_skill_dual_write(session, TEST_UPN)
    assert fake_sql.skills["drifter"]["is_public"] is True

    client.patch("/api/skills/drifter/visibility", json={"is_public": False})
    backend_main.save_skill_dual_write(session, TEST_UPN)
    assert fake_sql.skills["drifter"]["is_public"] is False


def test_visibility_toggle_public_then_back_restores_caller_grant(client, grant_skill, fake_sql):
    grant_skill("toggle-me")

    r = client.patch("/api/skills/toggle-me/visibility", json={"is_public": True})
    assert r.status_code == 200
    assert fake_sql.skills["toggle-me"]["is_public"] is True

    r = client.patch("/api/skills/toggle-me/visibility", json={"is_public": False})
    assert r.status_code == 200
    assert fake_sql.skills["toggle-me"]["is_public"] is False
    # Demoting must not lock the caller out of their own skill.
    assert (TEST_UPN, "toggle-me") in fake_sql.grants


def test_visibility_requires_access(client, fake_sql):
    fake_sql.upsert_skill("someone-elses")
    r = client.patch("/api/skills/someone-elses/visibility", json={"is_public": True})
    assert r.status_code == 403


def test_last_grant_can_be_removed_when_skill_is_public(client, grant_skill, fake_sql):
    grant_skill("public-last")
    fake_sql.set_skill_visibility("public-last", True)
    r = client.delete(f"/api/skills/public-last/grants/{TEST_UPN}")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Visibility: internal children follow their parent
# ---------------------------------------------------------------------------


_PARENT_WITH_CHILDREN_MD = (
    "---\n"
    "name: family\n"
    "description: Parent scenario\n"
    "metadata:\n"
    "  skill_type: scenario-orchestration\n"
    "  children: [hidden-child, catalog-dep]\n"
    "---\n\n# Parent\n"
)


def _seed_family(backend_main, grant_skill, fake_sql):
    """A parent with one internal child and one plain catalog dependency."""
    backend_main.store.save_skill(
        backend_main.SkillFiles(name="family", skill_md=_PARENT_WITH_CHILDREN_MD)
    )
    grant_skill("family")
    fake_sql.upsert_skill("hidden-child", is_internal=True)
    fake_sql.upsert_skill("catalog-dep", is_internal=False)


def test_publishing_a_parent_publishes_its_internal_children(
    client, backend_main, grant_skill, fake_sql
):
    _seed_family(backend_main, grant_skill, fake_sql)

    r = client.patch("/api/skills/family/visibility", json={"is_public": True})
    assert r.status_code == 200
    assert r.json()["children"] == ["hidden-child"]
    assert fake_sql.skills["hidden-child"]["is_public"] is True
    # A child still in the host catalog belongs to everyone, not to this parent.
    assert fake_sql.skills["catalog-dep"]["is_public"] is False


def test_demoting_a_parent_demotes_and_regrants_its_internal_children(
    client, backend_main, grant_skill, fake_sql
):
    _seed_family(backend_main, grant_skill, fake_sql)
    client.patch("/api/skills/family/visibility", json={"is_public": True})

    r = client.patch("/api/skills/family/visibility", json={"is_public": False})
    assert r.status_code == 200
    assert fake_sql.skills["hidden-child"]["is_public"] is False
    # A private child with no grant would be reachable by nobody at all.
    assert (TEST_UPN, "hidden-child") in fake_sql.grants


def test_visibility_of_a_childless_skill_reports_no_children(client, grant_skill, fake_sql):
    grant_skill("solo")
    r = client.patch("/api/skills/solo/visibility", json={"is_public": True})
    assert r.status_code == 200
    assert r.json()["children"] == []
    assert r.json()["failed_children"] == []



# ---------------------------------------------------------------------------
# Session ownership
# ---------------------------------------------------------------------------


def test_session_create_records_owner_upn(client):
    r = client.post("/api/sessions", json={"mode": "new", "materials": []})
    assert r.json()["owner_upn"] == TEST_UPN


def test_session_list_filters_by_owner(client, backend_main):
    # Create one session as TEST_UPN.
    r = client.post("/api/sessions", json={"mode": "new", "materials": []})
    sid_mine = r.json()["id"]
    # Inject a session owned by a different user directly.
    from backend.models import Session
    other = Session(owner_upn="someone-else@x")
    backend_main.sessions[other.id] = other
    listed = client.get("/api/sessions").json()
    ids = [s["id"] for s in listed]
    assert sid_mine in ids
    assert other.id not in ids


def test_session_read_other_owner_returns_404(client, backend_main):
    from backend.models import Session
    other = Session(owner_upn="someone-else@x")
    backend_main.sessions[other.id] = other
    r = client.get(f"/api/sessions/{other.id}")
    assert r.status_code == 404