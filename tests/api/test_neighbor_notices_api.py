import pytest

from backend import neighbor_notice_repo as repo
from backend.models import CreateSessionRequest, NeighborSkill, Session
from backend.state_machine import build_system_prompt


OWNER = "owner@example.com"
AUTHOR = "test@example.com"


def seed(main, sql, name, creator, description="Current description"):
    sql.upsert_skill(name, created_by=creator, is_public=True)
    main.store.save_skill(main.SkillFiles(name=name, skill_md=f"---\nname: {name}\ndescription: {description}\n---\nPrivate body for {name}"))


def source_session(main):
    session = Session(owner_upn=AUTHOR)
    session.current_skill.skill_md = "---\nname: source\ndescription: Source purpose\n---\nBody"
    session.prepare_brief.understanding.neighbor_skills = [NeighborSkill(skill="target")]
    main.sessions[session.id] = session
    return session


def test_queue_waits_for_save_and_never_edits_target(client, backend_main, fake_sql):
    seed(backend_main, fake_sql, "target", OWNER)
    session = source_session(backend_main)
    original = backend_main.store.load_skill("target")
    backend_main.apply_tool_effect(session, "update_prepare_checklist", {"item": "routing_uniqueness_confirmed", "confirmed": True})
    assert repo.list_open_notices(OWNER) == []
    assert session.neighbor_notice_outcomes[0].status == "pending"
    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 200
    assert session.neighbor_notice_outcomes[0].status == "sent"
    assert repo.list_open_notices(OWNER)[0].source_skill_keys == ["source"]
    assert backend_main.store.load_skill("target") == original
    assert session.neighbor_edits == []


def test_missing_creator_skips_and_noncreator_patch_is_rejected(backend_main, fake_sql):
    seed(backend_main, fake_sql, "target", None)
    session = source_session(backend_main)
    backend_main._queue_confirmed_neighbor_notices(session)
    assert session.neighbor_notice_outcomes[0].status == "skipped"
    with pytest.raises(ValueError, match="not yours"):
        backend_main.apply_tool_effect(session, "propose_neighbor_edit", {"skill_name": "target", "patch": "stale suggestion"})
    assert repo.list_open_notices(OWNER) == []


def test_missing_confirmed_neighbor_skips_without_blocking_routing(client, backend_main):
    session = source_session(backend_main)
    response = client.post(f"/api/sessions/{session.id}/checklist", json={"item": "routing_uniqueness_confirmed", "status": "confirmed"})
    assert response.status_code == 200
    assert response.json()["prepare_brief"]["verify_checklist"]["routing_uniqueness_confirmed"] is True
    assert response.json()["neighbor_notice_outcomes"][0]["status"] == "skipped"
    assert repo.list_open_notices(OWNER) == []


def test_failed_source_save_sends_nothing(client, backend_main, fake_sql, monkeypatch):
    seed(backend_main, fake_sql, "target", OWNER)
    session = source_session(backend_main)
    backend_main._queue_confirmed_neighbor_notices(session)
    monkeypatch.setattr(backend_main, "lint_skill_package", lambda *args, **kwargs: (_ for _ in ()).throw(backend_main.EaaLintUnavailable("Offline")))
    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 503
    assert repo.list_open_notices(OWNER) == []
    assert session.neighbor_notice_outcomes[0].status == "pending"


def test_notice_failure_does_not_undo_save_and_retry_succeeds(client, backend_main, fake_sql, monkeypatch):
    seed(backend_main, fake_sql, "target", OWNER)
    session = source_session(backend_main)
    backend_main._queue_confirmed_neighbor_notices(session)
    original = repo.upsert_open_notice
    monkeypatch.setattr(repo, "upsert_open_notice", lambda *args: (_ for _ in ()).throw(RuntimeError("Offline")))
    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 200
    assert session.neighbor_notice_outcomes[0].status == "failed"
    assert "source" in fake_sql.skills
    monkeypatch.setattr(repo, "upsert_open_notice", original)
    assert client.post(f"/api/sessions/{session.id}/save", json={}).status_code == 200
    assert session.neighbor_notice_outcomes[0].status == "sent"
    assert len(repo.list_open_notices(OWNER)) == 1


def test_recipient_loads_latest_content_projection_and_closes_immediately(client, backend_main, fake_sql):
    seed(backend_main, fake_sql, "target", OWNER, "Old target")
    seed(backend_main, fake_sql, "source", AUTHOR, "Old source")
    notice = repo.upsert_open_notice("target", OWNER, AUTHOR, ["source"])
    seed(backend_main, fake_sql, "target", OWNER, "Latest target")
    seed(backend_main, fake_sql, "source", AUTHOR, "Latest source")
    assert client.get("/api/neighbor-notices").json() == []
    client.headers["X-Test-UPN"] = OWNER
    listing = client.get("/api/neighbor-notices").json()
    assert listing[0]["sources"][0]["description"] == "Latest source"
    assert "Private body" not in str(listing)
    response = client.post(f"/api/neighbor-notices/{notice.id}/start-modify")
    assert response.status_code == 200
    opened = response.json()
    assert opened["current_stage"] == "prepare"
    assert "Latest target" in opened["current_skill"]["skill_md"]
    assert opened["neighbor_notice_review"]["sources"][0]["description"] == "Latest source"
    assert client.get("/api/neighbor-notices").json() == []
    assert client.post(f"/api/neighbor-notices/{notice.id}/start-modify").json()["id"] == opened["id"]
    assert "Latest target" in backend_main.store.load_skill("target").skill_md
    assert "Creator's Neighbor Relationship Review" in build_system_prompt(backend_main.sessions[opened["id"]])


def test_persistence_failure_keeps_open_and_retry_recovers(client, backend_main, fake_sql, monkeypatch):
    seed(backend_main, fake_sql, "target", AUTHOR)
    notice = repo.upsert_open_notice("target", AUTHOR, OWNER, ["missing-source"])
    original = backend_main.persist_session
    monkeypatch.setattr(backend_main, "persist_session", lambda session: (_ for _ in ()).throw(RuntimeError("Disk unavailable")))
    with pytest.raises(RuntimeError, match="Disk unavailable"):
        client.post(f"/api/neighbor-notices/{notice.id}/start-modify")
    assert len(repo.list_open_notices(AUTHOR)) == 1
    monkeypatch.setattr(backend_main, "persist_session", original)
    result = client.post(f"/api/neighbor-notices/{notice.id}/start-modify")
    assert result.status_code == 200
    assert result.json()["neighbor_notice_review"]["sources"][0]["available"] is False


@pytest.mark.parametrize("invalid", ["deleted", "creator", "scoped"])
def test_start_requires_exact_current_owned_target(client, backend_main, fake_sql, invalid):
    seed(backend_main, fake_sql, "target", AUTHOR)
    key = "target"
    if invalid == "scoped":
        fake_sql.upsert_skill("target", owner_upn=AUTHOR, created_by=AUTHOR)
        key = f"target_{AUTHOR}"
    notice = repo.upsert_open_notice(key, AUTHOR, OWNER, ["source"])
    if invalid == "deleted":
        fake_sql.skills.pop(key)
    elif invalid == "creator":
        fake_sql.skills[key]["created_by"] = OWNER
    assert client.post(f"/api/neighbor-notices/{notice.id}/start-modify").status_code == 409
    assert len(repo.list_open_notices(AUTHOR)) == 1
    assert not backend_main.sessions


def test_dismiss_requires_recipient_and_closes_without_session(client, backend_main, fake_sql):
    notice = repo.upsert_open_notice("target", OWNER, AUTHOR, ["source"])
    assert client.post(f"/api/neighbor-notices/{notice.id}/dismiss").status_code == 404
    client.headers["X-Test-UPN"] = OWNER
    assert client.post(f"/api/neighbor-notices/{notice.id}/dismiss").status_code == 200
    assert client.post(f"/api/neighbor-notices/{notice.id}/dismiss").status_code == 200
    assert repo.list_open_notices(OWNER) == []
    assert not backend_main.sessions