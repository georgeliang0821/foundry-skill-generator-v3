from __future__ import annotations

from datetime import datetime, timezone

from backend.models import SkillFiles, SkillKind
from backend.skills_index import SkillsIndex
from backend.skills_repo import SkillRow


class FakeBlobStore:
    def __init__(self, files: list[SkillFiles]) -> None:
        self.files = {item.name: item for item in files}

    def list_skills(self):
        from backend.models import SkillIndexEntry

        return [
            SkillIndexEntry(name=item.name, description="calendar workflow", version_hash="v1")
            for item in self.files.values()
        ]

    def load_skill(self, name: str) -> SkillFiles:
        return self.files[name]


def _row(name: str, *, internal: bool = False) -> SkillRow:
    now = datetime.now(timezone.utc)
    return SkillRow(name, name, None, False, internal, True, now, now)


def test_keyword_topn_filters_peers_by_skill_kind(monkeypatch) -> None:
    parent = SkillFiles(
        name="calendar-parent",
        skill_md=(
            "---\nname: calendar-parent\ndescription: Calendar workflow\nmetadata:\n"
            "  skill_type: scenario-orchestration\n  children: [calendar-child]\n---\n"
        ),
    )
    child = SkillFiles(
        name="calendar-child",
        skill_md="---\nname: calendar-child\ndescription: Calendar workflow\n---\n",
    )
    capability = SkillFiles(
        name="calendar-capability",
        skill_md="---\nname: calendar-capability\ndescription: Calendar workflow\n---\n",
    )
    rows = [_row("calendar-parent"), _row("calendar-child", internal=True), _row("calendar-capability")]
    monkeypatch.setattr("backend.skills_repo.list_skills_for_user", lambda upn: rows)
    index = SkillsIndex(FakeBlobStore([parent, child, capability]), ttl_seconds=0)

    scenario_names = {
        card.name for card, _ in index.keyword_topn(
            "calendar workflow", [], upn="user@x", kind=SkillKind.SCENARIO
        )
    }
    capability_names = {
        card.name for card, _ in index.keyword_topn(
            "calendar workflow", [], upn="user@x", kind=SkillKind.CAPABILITY
        )
    }

    assert scenario_names == {"calendar-parent", "calendar-capability"}
    assert capability_names == {"calendar-child", "calendar-capability"}


def test_keyword_topn_excludes_declared_children(monkeypatch) -> None:
    """A freshly authored child is still is_internal=0, so the kind filter alone
    cannot keep it out of a scenario's routing peers."""
    child = SkillFiles(
        name="calendar-child",
        skill_md="---\nname: calendar-child\ndescription: Calendar workflow\n---\n",
    )
    capability = SkillFiles(
        name="calendar-capability",
        skill_md="---\nname: calendar-capability\ndescription: Calendar workflow\n---\n",
    )
    rows = [_row("calendar-child"), _row("calendar-capability")]
    monkeypatch.setattr("backend.skills_repo.list_skills_for_user", lambda upn: rows)
    index = SkillsIndex(FakeBlobStore([child, capability]), ttl_seconds=0)

    names = {
        card.name for card, _ in index.keyword_topn(
            "calendar workflow",
            [],
            upn="user@x",
            kind=SkillKind.SCENARIO,
            exclude={"calendar-child"},
        )
    }

    assert names == {"calendar-capability"}


def test_keyword_topn_drops_a_child_no_parent_has_claimed_yet(monkeypatch) -> None:
    """is_internal is only stamped when the parent is saved, so the parent's own
    children list has to carry the exclusion until then."""
    parent = SkillFiles(
        name="calendar-parent",
        skill_md=(
            "---\nname: calendar-parent\ndescription: Calendar workflow\nmetadata:\n"
            "  skill_type: scenario-orchestration\n  children: [calendar-child]\n---\n"
        ),
    )
    child = SkillFiles(
        name="calendar-child",
        skill_md="---\nname: calendar-child\ndescription: Calendar workflow\n---\n",
    )
    capability = SkillFiles(
        name="calendar-capability",
        skill_md="---\nname: calendar-capability\ndescription: Calendar workflow\n---\n",
    )
    rows = [_row("calendar-parent"), _row("calendar-child"), _row("calendar-capability")]
    monkeypatch.setattr("backend.skills_repo.list_skills_for_user", lambda upn: rows)
    index = SkillsIndex(FakeBlobStore([parent, child, capability]), ttl_seconds=0)

    names = {
        card.name for card, _ in index.keyword_topn(
            "calendar workflow", [], upn="user@x", kind=SkillKind.SCENARIO
        )
    }

    assert names == {"calendar-parent", "calendar-capability"}