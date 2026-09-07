from __future__ import annotations

import re

import pytest

from backend.blob_store import (
    MAX_SKILL_NAME_LEN,
    AzureBlobSkillStore,
    LocalSkillStore,
    blob_path_of,
    blob_prefix_of,
    parse_frontmatter,
    replace_frontmatter_name,
    safe_skill_name,
)
from backend.models import SkillFiles


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
    monkeypatch.setenv("AZURE_BLOB_CONTAINER", "skills-container")
    monkeypatch.setenv("AZURE_BLOB_PREFIX", "not-skills")
    monkeypatch.delenv("SKILL_BLOB_PREFIX", raising=False)
    with pytest.raises(ValueError, match="AZURE_BLOB_PREFIX"):
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

    with pytest.raises(RuntimeError, match="Version hash mismatch"):
        store.save_skill(SkillFiles(name="demo", skill_md="changed"), expected_version_hash="stale")
