"""API tests for v7 RLS: grants endpoints + skill list filtering + session ownership."""
from __future__ import annotations

from tests.conftest import TEST_UPN
import pytest


def test_manage_delete_blocks_referenced_skill(client, backend_main, fake_sql):
    for name, children in (("child", "[]"), ("parent", "[child]")):
        backend_main.store.save_skill(backend_main.SkillFiles(
            name=name, skill_md=f"---\nname: {name}\nmetadata:\n  children: {children}\n---\n",
        ))
        fake_sql.upsert_skill(name, created_by=TEST_UPN)
    dependencies = client.get("/api/skills/child/dependencies")
    assert dependencies.status_code == 200
    assert dependencies.json()["dependents"][0]["name"] == "parent"
    response = client.request("DELETE", "/api/skills/child", json={"confirm_name": "child"})
    assert response.status_code == 409
    assert backend_main.skills_repo.get_skill("child") is not None
    assert backend_main.store.load_skill("child")


def test_manage_delete_cascades_grants(client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="remove-me", skill_md="---\nname: remove-me\n---\n"))
    fake_sql.upsert_skill("remove-me", created_by=TEST_UPN)
    fake_sql.add_grant("remove-me", TEST_UPN)
    response = client.request("DELETE", "/api/skills/remove-me", json={"confirm_name": "remove-me"})
    assert response.status_code == 200
    assert backend_main.skills_repo.get_skill("remove-me") is None
    assert not backend_main.skills_repo.list_grants("remove-me")
    with pytest.raises(FileNotFoundError):
        backend_main.store.load_skill("remove-me")
    backup = next(iter(backend_main.store.store.history.values()))
    assert backup["manifest"]["operation"] == "delete"
    assert backup["manifest"]["captured_by"] == TEST_UPN
    assert "SKILL.md" in backup["files"]


@pytest.mark.parametrize("action", [None, "keep_internal", "make_capability"])
def test_manage_delete_reconciles_only_unreferenced_internal_children(
    client, backend_main, fake_sql, action
):
    for name, children, creator in (
        ("scenario-a", "[shared-child, orphan-child, visible-child]", TEST_UPN),
        ("scenario-b", "[shared-child]", "other@example.com"),
        ("shared-child", "[]", TEST_UPN),
        ("orphan-child", "[]", "other@example.com"),
        ("visible-child", "[]", TEST_UPN),
    ):
        backend_main.store.save_skill(backend_main.SkillFiles(
            name=name, skill_md=f"---\nname: {name}\nmetadata:\n  children: {children}\n---\n",
        ))
        fake_sql.upsert_skill(
            name, created_by=creator, is_internal=name in {"shared-child", "orphan-child"},
            is_public=name == "orphan-child", enabled=name != "orphan-child",
        )
        fake_sql.add_grant(name, TEST_UPN)
    child_before = dict(fake_sql.skills["orphan-child"])
    grants_before = dict(fake_sql.grants)
    dependencies = client.get("/api/skills/scenario-a/dependencies").json()
    assert dependencies["orphaned_children"] == ["orphan-child"]
    assert "scenario-b" not in str(dependencies)

    response = client.request("DELETE", "/api/skills/scenario-a", json={
        "confirm_name": "scenario-a", "orphan_action": action,
    })

    if action is None:
        assert response.status_code == 409
        assert response.json()["detail"]["kind"] == "orphaned_children"
        assert response.json()["detail"]["children"] == ["orphan-child"]
        assert backend_main.store.load_skill("scenario-a")
        assert not backend_main.store.store.history
        assert fake_sql.skills["orphan-child"] == child_before
        assert fake_sql.grants == grants_before
    else:
        assert response.status_code == 200
        assert "scenario-a" not in fake_sql.skills
        assert fake_sql.skills["orphan-child"]["is_internal"] is (action == "keep_internal")
        for field in ("is_public", "enabled", "created_by"):
            assert fake_sql.skills["orphan-child"][field] == child_before[field]
        assert fake_sql.grants == {
            key: value for key, value in grants_before.items() if key[1] != "scenario-a"
        }
    assert fake_sql.skills["shared-child"]["is_internal"] is True
    assert fake_sql.skills["visible-child"]["is_internal"] is False
    for child in ("shared-child", "orphan-child", "visible-child"):
        assert backend_main.store.load_skill(child)


@pytest.mark.parametrize("change", ["add_parent", "remove_parent", "incomplete_scan"])
def test_manage_delete_rechecks_child_references_after_backup(
    client, backend_main, fake_sql, monkeypatch, change
):
    for name, children in (
        ("scenario-a", "[child]"), ("child", "[]"),
        ("scenario-b", "[child]" if change == "remove_parent" else "[]"),
    ):
        backend_main.store.save_skill(backend_main.SkillFiles(
            name=name, skill_md=f"---\nname: {name}\nmetadata:\n  children: {children}\n---\n",
        ))
        fake_sql.upsert_skill(name, created_by=TEST_UPN, is_internal=name == "child")
    backup = backend_main.store.backup_for_delete

    def changed(name, upn):
        version = backup(name, upn)
        if change == "incomplete_scan":
            fake_sql.upsert_skill("missing-parent")
        else:
            children = "[child]" if change == "add_parent" else "[]"
            backend_main.store.save_skill(backend_main.SkillFiles(
                name="scenario-b",
                skill_md=f"---\nname: scenario-b\nmetadata:\n  children: {children}\n---\n",
            ))
        return version

    monkeypatch.setattr(backend_main.store, "backup_for_delete", changed)
    response = client.request("DELETE", "/api/skills/scenario-a", json={
        "confirm_name": "scenario-a",
        "orphan_action": None if change == "remove_parent" else "make_capability",
    })

    assert fake_sql.skills["child"]["is_internal"] is True
    if change == "add_parent":
        assert response.status_code == 200
        assert "scenario-a" not in fake_sql.skills
    else:
        assert response.status_code == (409 if change == "remove_parent" else 503)
        if change == "remove_parent":
            assert response.json()["detail"]["children"] == ["child"]
        assert backend_main.store.load_skill("scenario-a")
        assert "scenario-a" in fake_sql.skills


@pytest.mark.parametrize("failure", ["backup", "blob", "sql"])
def test_manage_delete_failure_preserves_registration(client, backend_main, fake_sql, monkeypatch, failure):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    if failure == "backup":
        monkeypatch.setattr(backend_main.store, "backup_for_delete", fail)
    elif failure == "blob":
        monkeypatch.setattr(backend_main.store, "delete_skill", fail)
    else:
        monkeypatch.setattr(backend_main.skills_repo, "delete_skill", fail)
    response = client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"})
    assert response.status_code == 503
    assert backend_main.skills_repo.get_skill("target") is not None
    if failure != "sql":
        assert backend_main.store.load_skill("target")
    else:
        assert "SQL cleanup failed" in response.json()["detail"]


def test_manage_delete_hides_inaccessible_parent(client, backend_main, fake_sql):
    for name, creator, children in (("child", TEST_UPN, "[]"), ("secret-parent", "other@x", "[child]")):
        backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md=f"---\nname: {name}\nmetadata:\n  children: {children}\n---\n"))
        fake_sql.upsert_skill(name, created_by=creator)
    response = client.get("/api/skills/child/dependencies")
    assert response.status_code == 200
    assert response.json()["hidden_dependents"] == 1
    assert response.json()["can_delete"] is False
    assert "secret-parent" not in response.text


def test_manage_delete_keeps_session_and_rejects_old_save(client, backend_main, grant_skill):
    backend_main.store.save_skill(backend_main.SkillFiles(name="bound", skill_md="---\nname: bound\ndescription: d\n---\n"))
    grant_skill("bound")
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "bound"}).json()["id"]
    before = client.get(f"/api/sessions/{sid}").json()
    assert client.request("DELETE", "/api/skills/bound", json={"confirm_name": "bound"}).status_code == 200
    assert client.get(f"/api/sessions/{sid}").json() == before
    assert client.post(f"/api/sessions/{sid}/save", json={"name": "bound"}).status_code == 403


def test_manage_delete_fails_closed_on_incomplete_scan(client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    fake_sql.upsert_skill("missing")
    assert client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"}).status_code == 503
    assert backend_main.store.load_skill("target")


@pytest.mark.parametrize("creator, owner", [(None, None), ("other@x", None), (TEST_UPN, TEST_UPN)])
def test_manage_delete_rejects_ineligible_scope(client, backend_main, fake_sql, creator, owner):
    fake_sql.upsert_skill("target", created_by=creator, owner_upn=owner, is_public=owner is None)
    assert client.get("/api/skills/target/dependencies").status_code == 403
    assert client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"}).status_code == 403


def test_manage_delete_requires_auth_and_exact_confirmation(client, anon_client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    assert anon_client.get("/api/skills/target/dependencies").status_code == 401
    assert anon_client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"}).status_code == 401
    for confirmation in ("TARGET", " target", "other"):
        assert client.request("DELETE", "/api/skills/target", json={"confirm_name": confirmation}).status_code == 400
    assert not backend_main.store.store.history


@pytest.mark.parametrize("change", ["dependency", "version"])
def test_manage_delete_rechecks_after_backup(client, backend_main, fake_sql, monkeypatch, change):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    backup = backend_main.store.backup_for_delete

    def changed(name, upn):
        version = backup(name, upn)
        if change == "dependency":
            backend_main.store.save_skill(backend_main.SkillFiles(name="parent", skill_md="---\nname: parent\nmetadata:\n  children: [target]\n---\n"))
            fake_sql.upsert_skill("parent", created_by=TEST_UPN)
        else:
            backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\nChanged"))
        return version

    monkeypatch.setattr(backend_main.store, "backup_for_delete", changed)
    assert client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"}).status_code == 409
    assert backend_main.store.load_skill("target")


@pytest.mark.parametrize("content", ["no frontmatter", "---\nname: broken\nmetadata: [bad]\n---\n", "---\nname: broken\nmetadata:\n  children: target\n---\n"])
def test_manage_delete_rejects_malformed_dependency_documents(client, backend_main, fake_sql, content):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    backend_main.store.save_skill(backend_main.SkillFiles(name="broken", skill_md=content))
    assert client.get("/api/skills/target/dependencies").status_code == 503


def test_manage_delete_does_not_confuse_private_and_global_names(client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    fake_sql.upsert_skill("target", owner_upn="other@x")
    assert client.get("/api/skills/target/dependencies").status_code == 503


def test_manage_delete_invalidates_caches(client, backend_main, fake_sql, monkeypatch):
    from backend.skills_index import get_skills_index

    backend_main.store.save_skill(backend_main.SkillFiles(name="target", skill_md="---\nname: target\n---\n"))
    fake_sql.upsert_skill("target", created_by=TEST_UPN)
    fake_sql.add_grant("target", TEST_UPN)
    assert backend_main.acl_mod.can_access(TEST_UPN, "target")
    assert backend_main.store.list_skills()
    invalidated = []
    monkeypatch.setattr(get_skills_index(), "invalidate", lambda: invalidated.append(True))
    assert client.request("DELETE", "/api/skills/target", json={"confirm_name": "target"}).status_code == 200
    assert not backend_main.store.list_skills()
    assert not backend_main.acl_mod.can_access(TEST_UPN, "target")
    assert invalidated



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
        json={"skill_md": "---\nname: new-saver\ndescription: Hello\n---\n", "created_by": "spoofed@x"},
    )
    r = client.post(f"/api/sessions/{sid}/save", json={"name": "new-saver", "created_by": "spoofed@x"})
    assert r.status_code == 200
    # SQL row created.
    assert "new-saver" in fake_sql.skills
    assert fake_sql.skills["new-saver"]["is_public"] is False
    assert fake_sql.skills["new-saver"]["created_by"] == TEST_UPN
    # Self-grant created.
    assert (TEST_UPN, "new-saver") in fake_sql.grants
    # User can now list it.
    listed = client.get("/api/skills").json()
    assert any(item["name"] == "new-saver" for item in listed)


def test_modify_list_only_includes_creator_not_readers(client, backend_main, fake_sql):
    for name, creator, public in (
        ("mine", TEST_UPN, False), ("shared", "other@x", False),
        ("public", "other@x", True), ("legacy", None, True),
        ("disabled", TEST_UPN, False),
    ):
        backend_main.store.save_skill(backend_main.SkillFiles(name=name, skill_md=f"---\nname: {name}\n---\n"))
        fake_sql.upsert_skill(name, created_by=creator, is_public=public, enabled=name != "disabled")
    fake_sql.add_grant("shared", TEST_UPN)
    fake_sql.upsert_skill("sql-only", created_by=TEST_UPN)
    assert {entry["name"] for entry in client.get("/api/skills?purpose=modify").json()} == {"mine"}
    assert {entry["name"] for entry in client.get("/api/skills").json()} == {"shared", "public", "legacy"}
    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "mine"})
    assert response.status_code == 200


@pytest.mark.parametrize("creator", [None, "other@x"])
@pytest.mark.parametrize("public", [False, True])
def test_modify_denies_noncreator_even_with_access(client, backend_main, fake_sql, creator, public):
    backend_main.store.save_skill(backend_main.SkillFiles(name="shared", skill_md="---\nname: shared\n---\n"))
    fake_sql.upsert_skill("shared", created_by=creator, is_public=public)
    fake_sql.add_grant("shared", TEST_UPN)
    assert client.get("/api/skills/shared").status_code == 200
    response = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "shared"})
    assert response.status_code == 404


@pytest.mark.parametrize("creator", [None, "other@x"])
def test_new_same_name_cannot_overwrite_noncreator(client, backend_main, fake_sql, creator):
    original = backend_main.store.save_skill(backend_main.SkillFiles(name="shared", skill_md="---\nname: shared\n---\nOriginal"))
    fake_sql.upsert_skill("shared", created_by=creator, is_public=True)
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": "---\nname: shared\n---\nChanged"})
    response = client.post(f"/api/sessions/{sid}/save", json={"name": "shared"})
    assert response.status_code == 403
    assert backend_main.store.load_skill("shared").skill_md == original.skill_md
    assert fake_sql.skills["shared"]["created_by"] == creator


def test_old_modify_session_rechecks_creator_before_write(client, backend_main, fake_sql):
    original = backend_main.store.save_skill(backend_main.SkillFiles(name="mine", skill_md="---\nname: mine\n---\nOriginal"))
    fake_sql.upsert_skill("mine", created_by=TEST_UPN)
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "mine"}).json()["id"]
    fake_sql.skills["mine"]["created_by"] = "other@x"
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": "---\nname: mine\n---\nChanged"})
    response = client.post(f"/api/sessions/{sid}/save", json={"name": "mine"})
    assert response.status_code == 403
    assert backend_main.store.load_skill("mine").skill_md == original.skill_md


def test_creator_can_save_without_usage_grant(client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="mine", skill_md="---\nname: mine\n---\nOriginal"))
    fake_sql.upsert_skill("mine", created_by=TEST_UPN)
    sid = client.post("/api/sessions", json={"mode": "modify", "target_skill_id": "mine"}).json()["id"]
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": "---\nname: mine\n---\nChanged"})
    assert client.post(f"/api/sessions/{sid}/save", json={"name": "mine"}).status_code == 200
    assert "Changed" in backend_main.store.load_skill("mine").skill_md
    assert fake_sql.skills["mine"]["created_by"] == TEST_UPN


@pytest.mark.parametrize("creator", [None, "other@x"])
def test_neighbor_save_denies_noncreator_without_writing(client, backend_main, fake_sql, monkeypatch, creator):
    original = backend_main.store.save_skill(backend_main.SkillFiles(name="shared", skill_md="---\nname: shared\n---\nOriginal"))
    fake_sql.upsert_skill("shared", created_by=creator, is_public=True)
    fake_sql.add_grant("shared", TEST_UPN)
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    client.post(f"/api/sessions/{sid}/neighbors", json={"neighbor_skills": [{"skill": "shared"}]})
    response = client.post(f"/api/sessions/{sid}/neighbor-edits/shared/propose", json={"skill_md": "Changed"})
    assert response.status_code == 200
    assert response.json()["neighbor_edits"] == []

    def no_write(*args, **kwargs):
        pytest.fail("An unauthorized modification attempted a write")

    monkeypatch.setattr(backend_main.store, "save_skill", no_write)
    monkeypatch.setattr(backend_main.skills_repo, "upsert_skill", no_write)
    assert client.post(f"/api/sessions/{sid}/neighbor-edits/shared/save").status_code == 403
    assert backend_main.store.load_skill("shared").skill_md == original.skill_md


def test_competing_create_cannot_overwrite_winner_or_claim_creator(client, backend_main, fake_sql, monkeypatch):
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": "---\nname: racing\n---\nLosing draft"})
    save = backend_main.store.save_skill

    def race(files, expected_version_hash="", *, create_only=False, captured_by="", change_summary=""):
        save(backend_main.SkillFiles(name="racing", skill_md="Winning draft"), create_only=True)
        fake_sql.upsert_skill("racing", created_by="winner@x")
        return save(files, expected_version_hash, create_only=create_only, captured_by=captured_by, change_summary=change_summary)

    monkeypatch.setattr(backend_main.store, "save_skill", race)
    assert client.post(f"/api/sessions/{sid}/save", json={"name": "racing"}).status_code == 409
    assert backend_main.store.load_skill("racing").skill_md == "Winning draft"
    assert fake_sql.skills["racing"]["created_by"] == "winner@x"
    assert (TEST_UPN, "racing") not in fake_sql.grants


def test_orphan_blob_cannot_be_claimed_by_new_save(client, backend_main, fake_sql):
    backend_main.store.save_skill(backend_main.SkillFiles(name="orphan", skill_md="Original"))
    sid = client.post("/api/sessions", json={"mode": "new"}).json()["id"]
    client.put(f"/api/sessions/{sid}/draft", json={"skill_md": "---\nname: orphan\n---\nChanged"})
    assert client.post(f"/api/sessions/{sid}/save", json={"name": "orphan"}).status_code == 409
    assert backend_main.store.load_skill("orphan").skill_md == "Original"
    assert "orphan" not in fake_sql.skills


# ---------------------------------------------------------------------------
# Grants endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_public", [False, True])
def test_permission_management_requires_creator(client, fake_sql, is_public):
    fake_sql.upsert_skill("other-owned", created_by="other@example.com", is_public=is_public)
    fake_sql.add_grant("other-owned", TEST_UPN)
    fake_sql.add_grant("other-owned", "recipient@example.com")
    assert client.get("/api/skills/other-owned/grants").status_code == 403
    assert client.post("/api/skills/other-owned/grants", json={"user_upn": "new@example.com"}).status_code == 403
    assert client.delete("/api/skills/other-owned/grants/recipient@example.com").status_code == 403
    assert client.patch("/api/skills/other-owned/visibility", json={"is_public": not is_public}).status_code == 403
    assert ("recipient@example.com", "other-owned") in fake_sql.grants
    assert fake_sql.skills["other-owned"]["is_public"] is is_public


def test_creator_can_manage_without_use_grant(client, fake_sql):
    fake_sql.upsert_skill("owned-no-grant", created_by=TEST_UPN)
    assert client.get("/api/skills/owned-no-grant/grants").status_code == 200
    assert client.post("/api/skills/owned-no-grant/grants", json={"user_upn": "new@example.com"}).status_code == 200


def test_permission_selector_only_lists_own_global_skills(client, fake_sql):
    fake_sql.upsert_skill("owned", created_by=TEST_UPN)
    fake_sql.upsert_skill("disabled", created_by=TEST_UPN, enabled=False)
    fake_sql.upsert_skill("private", created_by=TEST_UPN, owner_upn=TEST_UPN)
    fake_sql.upsert_skill("public-other", created_by="other@example.com", is_public=True)
    fake_sql.upsert_skill("shared-other", created_by="other@example.com")
    fake_sql.add_grant("shared-other", TEST_UPN)
    fake_sql.upsert_skill("unknown-creator")
    assert [row["skill_name"] for row in client.get("/api/permissions/skills").json()["skills"]] == ["disabled", "owned"]


def test_private_scope_rejected_by_permission_guard(client, fake_sql, monkeypatch):
    from backend import skills_repo
    fake_sql.upsert_skill("private", created_by=TEST_UPN, owner_upn=TEST_UPN)
    row = skills_repo.get_skill("private", TEST_UPN)
    monkeypatch.setattr(skills_repo, "get_skill", lambda name: row)
    assert client.get("/api/skills/private/grants").status_code == 403
    assert client.patch("/api/skills/private/visibility", json={"is_public": True}).status_code == 403


def test_sql_principal_search_and_group_grant(client, grant_skill, fake_sql, monkeypatch):
    from backend import skills_repo
    grant_skill("owned")
    monkeypatch.setattr(skills_repo, "list_database_principals", lambda query, kind: [{"name": "EAA", "type": "X"}] if query == "EA" and kind == "X" else [])
    monkeypatch.setattr(skills_repo, "get_database_principal", lambda name: {"name": "EAA", "type": "X"} if name == "EAA" else None)
    assert client.get("/api/permissions/principals?q=EA&principal_type=X").json()["principals"] == [{"name": "EAA", "type": "X"}]
    response = client.post("/api/skills/owned/grants", json={"user_upn": "EAA", "principal_type": "X"})
    assert response.status_code == 200
    assert response.json()["user_upn"] == "EAA"
    assert ("EAA", "owned") in fake_sql.grants
    assert client.delete("/api/skills/owned/grants/EAA").status_code == 200
    assert ("EAA", "owned") not in fake_sql.grants
    assert client.post("/api/skills/owned/grants", json={"user_upn": "missing", "principal_type": "X"}).status_code == 400
    assert client.post("/api/skills/owned/grants", json={"user_upn": "EAA", "principal_type": "E"}).status_code == 400
    assert client.get("/api/permissions/principals?principal_type=R").status_code == 400


def test_permission_endpoints_require_auth(anon_client):
    assert anon_client.get("/api/permissions/skills").status_code == 401
    assert anon_client.get("/api/permissions/principals").status_code == 401


def test_permission_principal_errors_fail_closed(client, grant_skill, monkeypatch):
    from backend import skills_repo
    assert client.get("/api/permissions/skills").json() == {"skills": []}
    assert client.get("/api/permissions/principals").status_code == 403
    grant_skill("owned")

    def unavailable(*args):
        raise RuntimeError("Database metadata unavailable")

    monkeypatch.setattr(skills_repo, "list_database_principals", unavailable)
    monkeypatch.setattr(skills_repo, "get_database_principal", unavailable)
    assert client.get("/api/permissions/principals").status_code == 503
    assert client.post("/api/skills/owned/grants", json={"user_upn": "EAA", "principal_type": "X"}).status_code == 503


def test_selected_sql_upn_is_normalized(client, grant_skill, fake_sql, monkeypatch):
    from backend import skills_repo
    grant_skill("owned")
    monkeypatch.setattr(skills_repo, "get_database_principal", lambda name: {"name": "Alice@Contoso.com", "type": "E"})
    assert client.post("/api/skills/owned/grants", json={"user_upn": "Alice@Contoso.com", "principal_type": "E"}).status_code == 200
    assert ("alice@contoso.com", "owned") in fake_sql.grants


@pytest.mark.parametrize("group_name", ["Group" * 33, "\u7fa4\u7d44"])
def test_group_grant_rejects_unsupported_names(client, grant_skill, monkeypatch, group_name):
    from backend import skills_repo
    grant_skill("owned")
    monkeypatch.setattr(skills_repo, "get_database_principal", lambda name: {"name": group_name, "type": "X"})
    assert client.post("/api/skills/owned/grants", json={"user_upn": group_name, "principal_type": "X"}).status_code == 400


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
    fake_sql.upsert_skill("hidden-child", is_internal=True, created_by=TEST_UPN)
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


def test_parent_visibility_never_changes_another_creators_child(client, backend_main, grant_skill, fake_sql):
    _seed_family(backend_main, grant_skill, fake_sql)
    fake_sql.skills["hidden-child"]["created_by"] = "other@example.com"
    response = client.patch("/api/skills/family/visibility", json={"is_public": True})
    assert response.status_code == 200
    assert response.json()["failed_children"] == ["hidden-child"]
    assert fake_sql.skills["hidden-child"]["is_public"] is False


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