from __future__ import annotations

import re
import json
import hashlib
from datetime import datetime

import pytest

from backend.blob_store import (
    MAX_SKILL_NAME_LEN,
    AzureBlobSkillStore,
    CachedSkillStore,
    LocalSkillStore,
    SkillBackupError,
    VersionConflict,
    blob_path_of,
    blob_prefix_of,
    infer_skill_kind,
    parse_frontmatter,
    prepare_skill_version,
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


def _versioned_md(version: str, body: str = "Original") -> str:
    return f'---\nname: demo\nmetadata:\n  version: "{version}"\n---\n{body}'


@pytest.mark.parametrize(
    "stored, requested, expected",
    [
        ("1.6", "1.6", "1.6.1"),
        ("1.6.99", "1.6.99", "1.6.100"),
        ("1.6.3", "1.6.700", "1.6.4"),
        ("1.6.3", "1.5.0", "1.6.4"),
        ("1.6.3", "1.7", "1.7.0"),
        ("1.6.3", "2.0.0", "2.0.0"),
    ],
)
def test_prepare_skill_version_uses_stored_revision(stored, requested, expected) -> None:
    original = _versioned_md(stored)
    updated = _versioned_md(requested, "Changed")
    assert prepare_skill_version(updated, original) == _versioned_md(expected, "Changed")
    assert prepare_skill_version(_versioned_md(expected, "Changed"), original) == _versioned_md(expected, "Changed")


def test_prepare_skill_version_normalizes_first_save_and_preserves_noop() -> None:
    assert prepare_skill_version(_versioned_md("1.6")) == _versioned_md("1.6.0")
    original = _versioned_md("1.6")
    assert prepare_skill_version(original, original) == original
    assert prepare_skill_version(_versioned_md("1.6.99"), original) == original
    assert prepare_skill_version(original, original, other_files_changed=True) == _versioned_md("1.6.1")


def test_prepare_skill_version_preserves_other_bytes() -> None:
    original = '\ufeff---\r\nname: demo\r\ndescription: >\r\n  version: example\r\nmetadata: {author: test, version: 1.6} # keep\r\n---\r\nOriginal\r\n'
    updated = original.replace("Original", "Changed")
    assert prepare_skill_version(updated, original) == updated.replace('version: 1.6}', 'version: "1.6.1"}')


def test_prepare_skill_version_leaves_unversioned_skills_alone() -> None:
    original = "---\nname: demo\n---\nOriginal"
    assert prepare_skill_version(original) == original
    with pytest.raises(ValueError, match="Keep the stored"):
        prepare_skill_version(original, _versioned_md("1.6"))


@pytest.mark.parametrize("version", ["v1.6", "1.6-beta", "1.6.0.1", "1.06", ""])
def test_prepare_skill_version_rejects_invalid_versions(version) -> None:
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        prepare_skill_version(_versioned_md(version))


@pytest.mark.parametrize(
    "metadata",
    [
        'shared: &v "1.6"\nmetadata:\n  version: *v',
        'metadata:\n  version: &v "1.6"\nshared: *v',
        'shared: &meta {version: "1.6"}\nmetadata: *meta',
    ],
)
def test_prepare_skill_version_rejects_aliases_without_touching_other_fields(metadata) -> None:
    with pytest.raises(ValueError, match="alias|anchor"):
        prepare_skill_version(f"---\nname: demo\n{metadata}\n---\nOriginal")


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


def test_local_store_create_only_keeps_existing_package() -> None:
    store = LocalSkillStore()
    original = SkillFiles(name="demo", skill_md="original", script="original script", assets={"assets/a.txt": "original"})
    store.save_skill(original, create_only=True)
    with pytest.raises(VersionConflict):
        store.save_skill(SkillFiles(name="demo", skill_md="replacement", script="replacement", assets={}), create_only=True)
    assert store.load_skill("demo").skill_md == "original"
    assert store.load_skill("demo").script == "original script"
    assert store.load_assets("demo") == {"assets/a.txt": b"original"}


def test_local_store_concurrent_create_only_has_one_winner() -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = LocalSkillStore()

    def create(content):
        try:
            store.save_skill(SkillFiles(name="demo", skill_md=content), create_only=True)
            return content
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [result for result in executor.map(create, ["first", "second"]) if result is not None]
    assert len(winners) == 1
    assert store.load_skill("demo").skill_md == winners[0]


def test_local_store_expected_version_does_not_recreate_deleted_skill() -> None:
    store = LocalSkillStore()
    original = store.save_skill(SkillFiles(name="demo", skill_md="original"))
    store.delete_skill("demo")
    with pytest.raises(VersionConflict):
        store.save_skill(original, original.version_hash)


def test_azure_create_only_does_not_overwrite_package() -> None:
    container = _FakeBlobContainer({"skills/demo/SKILL.md": b"original", "skills/demo/scripts/demo.py": b"script"})
    store = _azure_store(container)
    with pytest.raises(VersionConflict):
        store.save_skill(SkillFiles(name="demo", skill_md="replacement", assets={}, script="replacement"), create_only=True)
    assert container.blobs == {"skills/demo/SKILL.md": b"original", "skills/demo/scripts/demo.py": b"script"}


def test_azure_create_only_accepts_new_skill() -> None:
    container = _FakeBlobContainer({})
    _azure_store(container).save_skill(SkillFiles(name="demo", skill_md="new"), create_only=True)
    assert container.blobs["skills/demo/SKILL.md"] == b"new"


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

    def list_blobs(self, name_starts_with: str = "", include=None):
        return [type("_B", (), {"name": n, "etag": "e", "metadata": {}})() for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name: str, **kwargs) -> None:
        if name not in self.blobs:
            from azure.core.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError("Missing")
        del self.blobs[name]

    def get_blob_client(self, name: str):
        container = self

        class _Client:
            def upload_blob(self, data, overwrite=False, content_settings=None, **_kw):
                if "etag" in _kw and _kw["etag"] != self.get_blob_properties().etag:
                    from azure.core.exceptions import ResourceModifiedError
                    raise ResourceModifiedError("Changed")
                if not overwrite and name in container.blobs:
                    from azure.core.exceptions import ResourceExistsError
                    raise ResourceExistsError("Already exists")
                container.blobs[name] = data
                if content_settings is not None:
                    container.content_types[name] = content_settings.content_type

            def get_blob_properties(self):
                if name not in container.blobs:
                    from azure.core.exceptions import ResourceNotFoundError
                    raise ResourceNotFoundError("Missing")
                return type("_P", (), {"etag": hashlib.sha256(container.blobs[name]).hexdigest()})()

            def download_blob(self, **_kw):
                if "etag" in _kw and _kw["etag"] != self.get_blob_properties().etag:
                    from azure.core.exceptions import ResourceModifiedError
                    raise ResourceModifiedError("Changed")
                return self

            def readall(self) -> bytes:
                if name not in container.blobs:
                    from azure.core.exceptions import ResourceNotFoundError
                    raise ResourceNotFoundError("Missing")
                return container.blobs[name]

        return _Client()


def _azure_store(container: _FakeBlobContainer) -> AzureBlobSkillStore:
    store = object.__new__(AzureBlobSkillStore)
    store.prefix, store.container, store.id = "skills", "c", "azure:test"
    store.service = type("_Service", (), {"get_container_client": lambda self, _name: container})()
    return store


@pytest.mark.parametrize("summary", ["Remove obsolete script and assets.", "\u79fb\u9664\u904e\u6642\u8173\u672c\u8207\u8cc7\u7522\u3002"])
def test_azure_backup_preserves_complete_old_package(summary) -> None:
    original = {
        "skills/demo/SKILL.md": b"old",
        "skills/demo/scripts/demo.py": b"old script",
        "skills/demo/assets/data.bin": b"\xff\x00",
    }
    live = _FakeHnsContainer(original)
    history = _FakeBlobContainer({})
    store = _azure_store(live)
    store.history_container = "skill-history"
    store.service = type("_Service", (), {"get_container_client": lambda self, name: history if name == "skill-history" else live})()
    store.save_skill(SkillFiles(name="demo", skill_md="new", remove_script=True, assets={}), captured_by="User@Example.com", change_summary=f" {summary} ")
    manifests = [name for name in history.blobs if name.endswith("/manifest.json")]
    assert len(manifests) == 1
    manifest_text = history.blobs[manifests[0]].decode("utf-8")
    assert f'  "change_summary": {json.dumps(summary, ensure_ascii=False)}' in manifest_text
    assert manifest_text.startswith("{\n") and manifest_text.endswith("\n")
    manifest = json.loads(manifest_text)
    assert manifest["captured_by"] == "user@example.com"
    assert manifest["change_summary"] == summary
    assert manifest["source_etag"] == hashlib.sha256(b"old").hexdigest()
    assert manifest["source_store"] == "azure:test"
    assert manifest["source_blob_path"] == "skills/demo/SKILL.md"
    assert datetime.fromisoformat(manifest["captured_at"]).utcoffset().total_seconds() == 0
    timestamp = datetime.fromisoformat(manifest["captured_at"]).strftime("%Y%m%dT%H%M%SZ")
    assert manifests[0] == f"demo/{timestamp}_{manifest['revision_id']}/manifest.json"
    for entry in manifest["files"]:
        data = original[f"skills/demo/{entry['path']}"]
        assert entry["size_bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    prefix = manifests[0].removesuffix("manifest.json")
    for path, data in original.items():
        assert history.blobs[f"{prefix}files/{path.removeprefix('skills/demo/')}"] == data
    assert live.blobs == {"skills/demo/SKILL.md": b"new"}


@pytest.mark.parametrize("azure", [False, True])
@pytest.mark.parametrize("source_version,target_version", [(None, None), ('"1.6"', '"1.6.1"'), ('"1.6.1"', '"1.6.1"'), ("1.10", '"1.10.1"')])
def test_backup_manifest_records_declared_version_transition(azure, source_version, target_version) -> None:
    def skill_md(version, body):
        metadata = f"metadata:\n  version: {version}\n" if version is not None else ""
        return f"---\nname: demo\ndescription: Demo\n{metadata}---\n{body}"

    original = skill_md(source_version, "Original")
    replacement = skill_md(target_version, "Changed")
    if azure:
        live = _FakeBlobContainer({"skills/demo/SKILL.md": original.encode("utf-8")})
        history = _FakeBlobContainer({})
        store = _history_store(live, history)
    else:
        store = LocalSkillStore(history_enabled=True)
        store.save_skill(SkillFiles(name="demo", skill_md=original))
    store.save_skill(SkillFiles(name="demo", skill_md=replacement))
    if azure:
        manifest = json.loads(next(data for path, data in history.blobs.items() if path.endswith("/manifest.json")))
    else:
        manifest = next(iter(store.history.values()))["manifest"]
    assert manifest["from_version"] == (source_version.strip('"') if source_version else None)
    assert manifest["to_version"] == (target_version.strip('"') if target_version else None)


def test_azure_backup_skips_first_creation() -> None:
    live = _FakeBlobContainer({})
    store = _azure_store(live)
    store.history_container = "skill-history"
    store.save_skill(SkillFiles(name="demo", skill_md="new"))
    assert live.blobs == {"skills/demo/SKILL.md": b"new"}


def test_azure_backup_failure_prevents_overwrite(monkeypatch) -> None:
    live = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"})
    store = _azure_store(live)
    store.history_container = "skill-history"
    def fail(*args):
        raise RuntimeError("Permission denied")
    monkeypatch.setattr(store, "_backup_skill", fail)
    with pytest.raises(SkillBackupError):
        store.save_skill(SkillFiles(name="demo", skill_md="new"))
    assert live.blobs == {"skills/demo/SKILL.md": b"old"}


def test_local_backup_retains_history_after_deletion() -> None:
    store = LocalSkillStore(history_enabled=True)
    first = store.save_skill(SkillFiles(name="demo", skill_md="old", script="script", assets={"assets/a.txt": "a"}))
    assert not store.history
    store.save_skill(SkillFiles(name="demo", skill_md="new"), first.version_hash, captured_by="test@example.com", change_summary="Update skill instructions.")
    backup = next(iter(store.history.values()))
    assert backup["manifest"]["change_summary"] == "Update skill instructions."
    assert backup["files"] == {"SKILL.md": b"old", "scripts/demo.py": b"script", "assets/a.txt": b"a"}
    store.delete_skill("demo")
    assert len(store.history) == 1


def _history_store(live, history):
    store = _azure_store(live)
    store.history_container = "skill-history"
    store.service = type("_Service", (), {"get_container_client": lambda self, name: history if name == "skill-history" else live})()
    return store


def test_delete_backup_captures_final_package_and_keeps_history() -> None:
    original = {"skills/demo/SKILL.md": b"---\nname: demo\n---\n", "skills/demo/assets/a.txt": b"data"}
    live, history = _FakeBlobContainer(original), _FakeBlobContainer({})
    store = _history_store(live, history)
    store.backup_for_delete("demo", "owner@example.com")
    manifest_path = next(path for path in history.blobs if path.endswith("/manifest.json"))
    manifest = json.loads(history.blobs[manifest_path])
    assert manifest["operation"] == "delete"
    assert manifest["captured_by"] == "owner@example.com"
    store.delete_skill("demo")
    assert not live.blobs
    assert history.blobs[manifest_path]
    assert any(path.endswith("files/assets/a.txt") for path in history.blobs)


def test_delete_backup_requires_history_container() -> None:
    store = _azure_store(_FakeBlobContainer({}))
    store.history_container = ""
    with pytest.raises(SkillBackupError):
        store.backup_for_delete("demo", "owner@example.com")


def test_delete_propagates_non_missing_blob_errors(monkeypatch) -> None:
    container = _FakeBlobContainer({"skills/demo/SKILL.md": b"content"})

    def denied(*args, **kwargs):
        from azure.core.exceptions import HttpResponseError
        raise HttpResponseError("Forbidden")

    monkeypatch.setattr(container, "delete_blob", denied)
    from azure.core.exceptions import HttpResponseError
    with pytest.raises(HttpResponseError):
        _azure_store(container).delete_skill("demo")


@pytest.mark.parametrize("fail_manifest", [False, True])
def test_history_upload_failure_leaves_live_untouched(monkeypatch, fail_manifest) -> None:
    original = {"skills/demo/SKILL.md": b"old", "skills/demo/assets/a.txt": b"a"}
    live, history = _FakeBlobContainer(original), _FakeBlobContainer({})
    store = _history_store(live, history)
    get_client = history.get_blob_client
    def failing_client(name):
        client = get_client(name)
        if name.endswith("manifest.json") == fail_manifest:
            def fail(*args, **kwargs):
                raise RuntimeError("Storage unavailable")
            client.upload_blob = fail
        return client
    monkeypatch.setattr(history, "get_blob_client", failing_client)
    with pytest.raises(SkillBackupError):
        store.save_skill(SkillFiles(name="demo", skill_md="new", assets={}))
    assert live.blobs == original
    assert not any(name.endswith("/manifest.json") for name in history.blobs)


def test_history_stale_version_has_no_backup_or_live_write() -> None:
    live, history = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"}), _FakeBlobContainer({})
    with pytest.raises(VersionConflict):
        _history_store(live, history).save_skill(SkillFiles(name="demo", skill_md="new"), "stale")
    assert not history.blobs
    assert live.blobs["skills/demo/SKILL.md"] == b"old"


@pytest.mark.parametrize("change", ["content", "add", "delete"])
def test_history_detects_source_changes_before_manifest(monkeypatch, change) -> None:
    live, history = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"}), _FakeBlobContainer({})
    store = _history_store(live, history)
    get_client = history.get_blob_client
    def changing_client(name):
        client = get_client(name)
        upload = client.upload_blob
        def write(data, **kwargs):
            upload(data, **kwargs)
            if change == "content":
                live.blobs["skills/demo/SKILL.md"] = b"external change"
            elif change == "add":
                live.blobs["skills/demo/assets/new.txt"] = b"added"
            else:
                live.blobs.pop("skills/demo/SKILL.md", None)
        client.upload_blob = write
        return client
    monkeypatch.setattr(history, "get_blob_client", changing_client)
    with pytest.raises(VersionConflict):
        store.save_skill(SkillFiles(name="demo", skill_md="replacement"))
    assert live.blobs.get("skills/demo/SKILL.md") != b"replacement"
    assert not any(name.endswith("/manifest.json") for name in history.blobs)


def test_history_cas_protects_live_after_manifest(monkeypatch) -> None:
    live, history = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"}), _FakeBlobContainer({})
    store = _history_store(live, history)
    get_client = history.get_blob_client
    def changing_client(name):
        client = get_client(name)
        upload = client.upload_blob
        def write(data, **kwargs):
            upload(data, **kwargs)
            if name.endswith("/manifest.json"):
                live.blobs["skills/demo/SKILL.md"] = b"external change"
        client.upload_blob = write
        return client
    monkeypatch.setattr(history, "get_blob_client", changing_client)
    with pytest.raises(VersionConflict):
        store.save_skill(SkillFiles(name="demo", skill_md="replacement"))
    assert live.blobs["skills/demo/SKILL.md"] == b"external change"
    assert any(name.endswith("/manifest.json") for name in history.blobs)


def test_history_repeated_saves_and_delete_preserve_isolated_revisions(monkeypatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 14, 1, 9, 54, tzinfo=tz)

    monkeypatch.setattr("backend.blob_store.datetime", FixedDatetime)
    legacy = {
        "demo/source-key/old-revision/manifest.json": b"legacy manifest",
        "demo/source-key/old-revision/files/SKILL.md": b"legacy content",
    }
    live, history = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"}), _FakeBlobContainer(legacy)
    store = _history_store(live, history)
    store.save_skill(SkillFiles(name="demo", skill_md="new"))
    store.save_skill(SkillFiles(name="demo", skill_md="new"))
    store.delete_skill("demo")
    manifests = [name for name in history.blobs if name.endswith("/manifest.json") and name not in legacy]
    assert len(manifests) == 2
    for name in manifests:
        manifest = json.loads(history.blobs[name])
        assert name == f"demo/20261014T010954Z_{manifest['revision_id']}/manifest.json"
    for name, data in legacy.items():
        assert history.blobs[name] == data
    assert not live.blobs


def test_history_create_only_conflict_does_not_capture() -> None:
    live, history = _FakeBlobContainer({"skills/demo/SKILL.md": b"old"}), _FakeBlobContainer({})
    with pytest.raises(VersionConflict):
        _history_store(live, history).save_skill(SkillFiles(name="demo", skill_md="new"), create_only=True)
    assert not history.blobs


def test_cached_store_forwards_backup_actor() -> None:
    local = LocalSkillStore(history_enabled=True)
    store = CachedSkillStore(local)
    store.save_skill(SkillFiles(name="demo", skill_md="old"))
    store.save_skill(SkillFiles(name="demo", skill_md="new"), captured_by="Author@Example.com")
    assert next(iter(local.history.values()))["manifest"]["captured_by"] == "author@example.com"


@pytest.mark.parametrize("history_name", ["skills-container", "bad/name", "UPPER", "aa", "bad--name"])
def test_azure_history_rejects_invalid_or_live_container(monkeypatch, history_name) -> None:
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://example.blob.core.windows.net")
    monkeypatch.setenv("AZURE_BLOB_CONTAINER", "skills-container")
    monkeypatch.setenv("AZURE_BLOB_HISTORY_CONTAINER", history_name)
    with pytest.raises(ValueError, match="AZURE_BLOB_HISTORY_CONTAINER"):
        AzureBlobSkillStore()


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


class _FakeHnsContainer(_FakeBlobContainer):
    """Hierarchical-namespace account: parent directories are listed and guard deletes."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        super().__init__({})
        self.dirs: set[str] = set()
        for name, data in blobs.items():
            self._add(name, data)

    def _add(self, name: str, data) -> None:
        parts = name.split("/")
        self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        self.blobs[name] = data

    def list_blobs(self, name_starts_with: str = "", include=None):
        entries = [(n, {}) for n in self.blobs] + [(d, {"hdi_isfolder": "true"}) for d in self.dirs]
        return [
            type("_B", (), {"name": n, "etag": "e", "metadata": meta if include else None})()
            for n, meta in sorted(entries)
            if n.startswith(name_starts_with)
        ]

    def delete_blob(self, name: str, **kwargs) -> None:
        if name in self.dirs:
            if any(n.startswith(f"{name}/") for n in [*self.blobs, *self.dirs]):
                raise RuntimeError("DirectoryIsNotEmpty")
            self.dirs.remove(name)
            return
        super().delete_blob(name, **kwargs)

    def get_blob_client(self, name: str):
        client = super().get_blob_client(name)
        container = self
        upload = client.upload_blob

        def upload_blob(data, **kw):
            upload(data, **kw)
            container._add(name, data)

        client.upload_blob = upload_blob
        return client


def test_azure_store_on_hns_saves_assets_without_deleting_directories() -> None:
    container = _FakeHnsContainer({"skills/demo/SKILL.md": b"old", "skills/demo/assets/old.css": b"o"})
    store = _azure_store(container)

    store.save_skill(SkillFiles(name="demo", skill_md="---\nname: demo\n---\n", assets={"assets/new.css": "n"}))

    assert sorted(container.blobs) == ["skills/demo/SKILL.md", "skills/demo/assets/new.css"]
    assert store.load_assets("demo") == {"assets/new.css": b"n"}


def test_azure_store_on_hns_delete_removes_files_and_directories() -> None:
    container = _FakeHnsContainer(
        {
            "skills/demo/SKILL.md": b"m",
            "skills/demo/assets/img/a.png": b"a",
            "skills/demo/scripts/demo.py": b"s",
            "skills/demo-bar/SKILL.md": b"m",
        }
    )

    _azure_store(container).delete_skill("demo")

    assert list(container.blobs) == ["skills/demo-bar/SKILL.md"]
    assert sorted(container.dirs) == ["skills", "skills/demo-bar"]
