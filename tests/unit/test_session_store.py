from __future__ import annotations

from pathlib import Path

from backend.models import Material, Session
from backend.session_store import LocalSessionStore


def test_session_store_save_load_and_list(tmp_path: Path) -> None:
    store = LocalSessionStore(tmp_path)
    session = Session(target_skill_id="demo-skill", materials=[Material(content="reference")])

    store.save(session)
    loaded = store.load_all()
    summaries = store.list(loaded)

    assert loaded[session.id].target_skill_id == "demo-skill"
    assert summaries[0].id == session.id
    # NEW sessions title from real content (here the material), not the
    # pinned target id which can go stale; target id is only the last resort.
    assert summaries[0].title == "reference"
    assert summaries[0].material_count == 1


def test_session_title_modify_mode_uses_target_id(tmp_path: Path) -> None:
    from backend.models import Mode

    store = LocalSessionStore(tmp_path)
    session = Session(mode=Mode.MODIFY, target_skill_id="demo-skill", materials=[Material(content="reference")])
    store.save(session)
    summaries = store.list(store.load_all())
    assert summaries[0].title == "demo-skill"


def test_session_title_falls_back_to_target_id(tmp_path: Path) -> None:
    store = LocalSessionStore(tmp_path)
    session = Session(target_skill_id="demo-skill")
    store.save(session)
    summaries = store.list(store.load_all())
    assert summaries[0].title == "demo-skill"


def test_session_store_ignores_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    store = LocalSessionStore(tmp_path)

    assert store.load_all() == {}


def test_session_store_sanitizes_session_id_for_path(tmp_path: Path) -> None:
    store = LocalSessionStore(tmp_path)

    path = store._path("../bad id")

    assert path == tmp_path / "badid.json"
