from __future__ import annotations

import re

import pytest

from backend.blob_store import (
    MAX_SKILL_NAME_LEN,
    AzureBlobSkillStore,
    LocalSkillStore,
    VersionConflict,
    blob_path_of,
    blob_prefix_of,
    infer_skill_kind,
    parse_frontmatter,
    replace_frontmatter_name,
    safe_skill_name,
    script_blob_path_of,
    script_relpath_of,
)
from backend.models import SkillFiles, SkillKind


def test_parse_frontmatter() -> None:
    name, description = parse_frontmatter("---\nname: demo\ndescription: Test skill\n---\nbody")

    assert name == "demo"
    assert description == "Test skill"


def test_parse_frontmatter_recovers_description_with_colon() -> None:
    # An unquoted description containing ': ' is invalid YAML; we must still
    # recover the field via the regex fallback (regression: the SQL column
    # used to end up holding the literal 'name: <skill>' line instead).
    name, description = parse_frontmatter(
        "---\nname: demo\ndescription: do X: then Y\n---\nbody"
    )
    assert name == "demo"
    assert description == "do X: then Y"


def test_parse_frontmatter_without_delimiters_returns_empty() -> None:
    assert parse_frontmatter("no frontmatter here") == ("", "")


@pytest.mark.parametrize(
    "skill_md",
    [
        "---\nname: demo\nmetadata:\n  children:\n    - hr-leave-system\n---\nbody",
        # skill_type alone is not enough, but together they still agree.
        "---\nname: demo\nmetadata:\n  skill_type: scenario-orchestration\n  children:\n    - hr-leave-system\n---\nbody",
    ],
)
def test_infer_skill_kind_reads_scenario_from_children(skill_md: str) -> None:
    assert infer_skill_kind(skill_md) is SkillKind.SCENARIO


@pytest.mark.parametrize(
    "skill_md",
    [
        "---\nname: demo\ndescription: Demo\n---\nbody",
        "---\nname: demo\nmetadata:\n  children: []\n---\nbody",
        "---\nname: demo\nmetadata:\n  children: hr-leave-system\n---\nbody",
        "---\nname: demo\nmetadata:\n  children:\n    - '   '\n---\nbody",
        # Mirrors create_session, which infers from children alone and leaves the
        # skill_type mismatch to its own 400.
        "---\nname: demo\nmetadata:\n  skill_type: scenario-orchestration\n---\nbody",
        "no frontmatter here",
        "---\nname: demo\n  bad: [indent\n---\nbody",
        "",
    ],
)
def test_infer_skill_kind_falls_back_to_capability(skill_md: str) -> None:
    assert infer_skill_kind(skill_md) is SkillKind.CAPABILITY


def test_replace_frontmatter_name_only_touches_the_top_level_key() -> None:
    md = (
        "---\n"
        "name: old-skill\n"
        "description: >\n"
        "  Uses old-skill for X: and Y.\n"
        "metadata:\n"
        "  name: not-the-skill-name\n"
        "  children:\n"
        "    - hr-leave-system\n"
        "---\n"
        "\n"
        '## Body\n\nfetch_skill(skill_name: old-skill)\nname: old-skill in prose\n'
    )

    updated = replace_frontmatter_name(md, "new-skill")

    assert parse_frontmatter(updated)[0] == "new-skill"
    # The nested metadata key, the folded description and the body are untouched.
    assert updated == md.replace("name: old-skill\n", "name: new-skill\n", 1)
    assert "  name: not-the-skill-name" in updated
    assert "fetch_skill(skill_name: old-skill)" in updated


def test_replace_frontmatter_name_preserves_crlf() -> None:
    md = "---\r\nname: old\r\ndescription: d\r\n---\r\n\r\nbody\r\n"

    updated = replace_frontmatter_name(md, "new")

    assert updated == "---\r\nname: new\r\ndescription: d\r\n---\r\n\r\nbody\r\n"


@pytest.mark.parametrize(
    "md",
    ["no frontmatter here", "---\ndescription: d\n---\nbody"],
)
def test_replace_frontmatter_name_rejects_unrenameable_documents(md: str) -> None:
    with pytest.raises(ValueError):
        replace_frontmatter_name(md, "new")


def test_safe_skill_name() -> None:
    assert safe_skill_name("../My Skill!") == "my-skill"
    assert safe_skill_name("...") == "skill"


@pytest.mark.parametrize(
    "raw",
    ["../My Skill!", "...", "-lead-and-trail-", "dots.and_underscores", "A" * 200, "  "],
)
def test_safe_skill_name_always_satisfies_the_db_check_constraint(raw: str) -> None:
    # dbo.skills.CK_skill_name_format: [a-z0-9-] only, no leading/trailing
    # hyphen, LEN <= 64. Anything else is rejected at INSERT time.
    name = safe_skill_name(raw)
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name)
    assert len(name) <= MAX_SKILL_NAME_LEN


def test_blob_paths_mirror_the_computed_columns() -> None:
    assert blob_prefix_of("alpha") == "skills/alpha/"
    assert blob_path_of("alpha") == "skills/alpha/SKILL.md"
    assert blob_prefix_of("alpha", "a@x") == "skills/_private/a@x/alpha/"
    assert blob_path_of("alpha", "a@x") == "skills/_private/a@x/alpha/SKILL.md"


def test_azure_store_rejects_a_prefix_that_drifts_from_the_computed_column(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://example.blob.core.windows.net")
    monkeypatch.setenv("AZURE_BLOB_CONTAINER", "skills-container/not-skills")
    with pytest.raises(ValueError, match="AZURE_BLOB_CONTAINER"):
        AzureBlobSkillStore()


def test_local_store_save_load_and_list() -> None:
    store = LocalSkillStore()

    saved = store.save_skill(
        SkillFiles(
            name="Demo Skill",
            skill_md="---\nname: demo-skill\ndescription: Demo\n---\n",
        )
    )

    assert saved.version_hash
    assert store.load_skill("demo-skill").name == "demo-skill"
    assert store.list_skills()[0].description == "Demo"


def test_local_store_rejects_stale_expected_version() -> None:
    store = LocalSkillStore()
    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\n"))

    with pytest.raises(VersionConflict, match="Version hash mismatch"):
        store.save_skill(SkillFiles(name="demo", skill_md="changed"), expected_version_hash="stale")


def test_script_path_is_named_after_the_skill() -> None:
    assert script_relpath_of("Room Finder") == "scripts/room-finder.py"
    assert script_blob_path_of("room-finder") == "skills/room-finder/scripts/room-finder.py"


def test_local_store_keeps_the_script_when_a_save_omits_it() -> None:
    store = LocalSkillStore()
    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\n", script="print(1)\n"))

    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\nEdited.\n"))

    assert store.load_skill("demo").script == "print(1)\n"
    assert store.has_script("demo")
    store.delete_skill("demo")
    assert not store.has_script("demo")


def test_local_store_list_marks_script_skills() -> None:
    store = LocalSkillStore()
    store.save_skill(SkillFiles(name="with-script", skill_md="---\nname: with-script\n---\n", script="print(1)\n"))
    store.save_skill(SkillFiles(name="inline", skill_md="---\nname: inline\n---\n"))

    assert {entry.name: entry.has_script for entry in store.list_skills()} == {"inline": False, "with-script": True}


def test_azure_store_list_marks_script_skills_from_the_listing() -> None:
    contents = {
        "skills/with-script/SKILL.md": "---\nname: with-script\n---\n",
        "skills/with-script/scripts/with-script.py": "print(1)\n",
        "skills/inline/SKILL.md": "---\nname: inline\n---\n",
        # A script under another skill's name does not count.
        "skills/stray/SKILL.md": "---\nname: stray\n---\n",
        "skills/stray/scripts/other.py": "print(1)\n",
    }

    class _Blob:
        def __init__(self, name: str) -> None:
            self.name, self.etag = name, "etag"

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        def download_blob(self):
            return self

        def readall(self) -> bytes:
            return contents[self.name].encode("utf-8")

    class _Container:
        def list_blobs(self, name_starts_with: str = ""):
            return iter(_Blob(name) for name in contents if name.startswith(name_starts_with))

        def get_blob_client(self, name: str) -> _Client:
            return _Client(name)

    store = object.__new__(AzureBlobSkillStore)
    store.prefix, store.container, store.id = "skills", "c", "azure:test"
    store.service = type("_Service", (), {"get_container_client": lambda self, _name: _Container()})()

    listed = {entry.name: entry.has_script for entry in store.list_skills()}

    assert listed == {"inline": False, "stray": False, "with-script": True}


def test_local_store_assets_replace_as_a_set_and_survive_a_save_without_them() -> None:
    store = LocalSkillStore()
    md = "---\nname: demo\n---\n"
    store.save_skill(SkillFiles(name="demo", skill_md=md, assets={"assets/a.css": "a", "references/b.md": "b"}))

    store.save_skill(SkillFiles(name="demo", skill_md=md + "Edited.\n"))
    assert store.load_assets("demo") == {"assets/a.css": b"a", "references/b.md": b"b"}

    store.save_skill(SkillFiles(name="demo", skill_md=md, assets={"assets/c.css": "c"}))
    assert store.load_assets("demo") == {"assets/c.css": b"c"}

    store.delete_skill("demo")
    assert store.load_assets("demo") == {}


class _FakeBlobContainer:
    """Just enough of ContainerClient for save/load_assets/delete_skill."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = dict(blobs)
        self.content_types: dict[str, str] = {}

    def list_blobs(self, name_starts_with: str = ""):
        return [type("_B", (), {"name": n, "etag": "e"})() for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name: str) -> None:
        del self.blobs[name]

    def get_blob_client(self, name: str):
        container = self

        class _Client:
            def upload_blob(self, data, overwrite=False, content_settings=None, **_kw):
                container.blobs[name] = data
                if content_settings is not None:
                    container.content_types[name] = content_settings.content_type

            def get_blob_properties(self):
                return type("_P", (), {"etag": "etag-1"})()

            def download_blob(self):
                return self

            def readall(self) -> bytes:
                return container.blobs[name]

        return _Client()


def _azure_store(container: _FakeBlobContainer) -> AzureBlobSkillStore:
    store = object.__new__(AzureBlobSkillStore)
    store.prefix, store.container, store.id = "skills", "c", "azure:test"
    store.service = type("_Service", (), {"get_container_client": lambda self, _name: container})()
    return store


def test_azure_store_writes_assets_and_sweeps_only_its_own_prefix() -> None:
    container = _FakeBlobContainer(
        {
            "skills/demo/SKILL.md": b"old",
            "skills/demo/scripts/demo.py": b"print(1)",
            "skills/demo/assets/old.css": b"old",
            "skills/demo-bar/assets/x.css": b"x",
        }
    )
    store = _azure_store(container)

    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\n", assets={"assets/new.css": "n"}))

    assert sorted(container.blobs) == [
        "skills/demo-bar/assets/x.css",
        "skills/demo/SKILL.md",
        "skills/demo/assets/new.css",
        "skills/demo/scripts/demo.py",
    ]
    assert container.content_types["skills/demo/assets/new.css"] == "text/css; charset=utf-8"
    assert store.load_assets("demo") == {"assets/new.css": b"n"}


def test_azure_store_save_without_assets_leaves_them_alone() -> None:
    container = _FakeBlobContainer({"skills/demo/SKILL.md": b"old", "skills/demo/assets/a.css": b"a"})

    _azure_store(container).save_skill(SkillFiles(name="demo", skill_md="new"))

    assert container.blobs["skills/demo/assets/a.css"] == b"a"


def test_azure_store_delete_removes_the_whole_prefix_only() -> None:
    container = _FakeBlobContainer(
        {
            "skills/demo/SKILL.md": b"m",
            "skills/demo/assets/a.css": b"a",
            "skills/demo/references/b.md": b"b",
            "skills/demo-bar/SKILL.md": b"m",
        }
    )

    _azure_store(container).delete_skill("demo")

    assert list(container.blobs) == ["skills/demo-bar/SKILL.md"]
