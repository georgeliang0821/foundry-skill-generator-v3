from __future__ import annotations

from pathlib import Path

from backend.models import Material, Session
from backend.session_store import LocalSessionStore


def test_explicit_session_root_overrides_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SGV2_SESSION_DIR", str(tmp_path / "configured"))
    store = LocalSessionStore(tmp_path / "explicit")
    assert store.root == tmp_path / "explicit"
    assert LocalSessionStore().root == tmp_path / "configured"


def test_e2e_startup_preserves_explicit_session_directory(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    environment = dict(os.environ, SGV2_E2E_MODE="1", SGV2_E2E_FAKE_AGENT="1", SGV2_SKILL_STORE="local", SGV2_SESSION_DIR=str(tmp_path / "isolated"))
    result = subprocess.run(
        [sys.executable, "-c", "import os; import backend.main as app; assert str(app.session_store.root) == os.environ['SGV2_SESSION_DIR']; assert app.session_store.root.name == 'isolated'"],
        env=environment, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
