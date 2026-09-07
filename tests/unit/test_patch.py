from __future__ import annotations

import pytest
import yaml

from backend.patch import PatchError, apply_v4a_to_content, version_hash


def test_apply_update_patch() -> None:
    content = "## Ground Rules\n- Old rule\n"
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ ## Ground Rules
- Old rule
+ New rule
*** End Patch"""

    updated, new_hash = apply_v4a_to_content(content, patch, expected_version_hash=version_hash(content))

    assert "- New rule" in updated
    assert new_hash == version_hash(updated)


def test_apply_add_file_patch() -> None:
    patch = """*** Begin Patch
*** Add File: SKILL.md
+---
+name: demo
+---
+Body
*** End Patch"""

    updated, new_hash = apply_v4a_to_content("", patch)

    assert updated == "---\nname: demo\n---\nBody"
    assert new_hash == version_hash(updated)


def test_apply_delete_file_patch() -> None:
    patch = """*** Begin Patch
*** Delete File: SKILL.md
*** End Patch"""

    updated, new_hash = apply_v4a_to_content("content", patch)

    assert updated == ""
    assert new_hash == version_hash("")


def test_apply_multi_hunk_update_patch() -> None:
    content = "## A\nold a\n\n## B\nold b\n"
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ ## A
-## A
-old a
+## A
+new a
@@ ## B
-## B
-old b
+## B
+new b
*** End Patch"""

    updated, _ = apply_v4a_to_content(content, patch)

    assert "new a" in updated
    assert "new b" in updated


def test_apply_patch_normalizes_crlf_anchor() -> None:
    content = "## A\r\nold\r\n"
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ ## A
-## A
-old
+## A
+new
*** End Patch"""

    updated, _ = apply_v4a_to_content(content, patch)

    assert updated == "## A\nnew\r\n"


def test_no_match_anchor_raises() -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ missing
-missing
+new
*** End Patch"""

    with pytest.raises(PatchError, match="not found"):
        apply_v4a_to_content("hello", patch)


def test_ambiguous_anchor_raises() -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ same
-same
+new
*** End Patch"""

    with pytest.raises(PatchError, match="ambiguous"):
        apply_v4a_to_content("same\nsame", patch)


def test_version_mismatch_raises() -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ hello
-hello
+hi
*** End Patch"""

    with pytest.raises(PatchError, match="Version hash mismatch"):
        apply_v4a_to_content("hello", patch, expected_version_hash="bad")


def test_invalid_patch_format_raises() -> None:
    with pytest.raises(PatchError, match="must start"):
        apply_v4a_to_content("hello", "*** Update File: SKILL.md")


FRONTMATTER_MD = '---\nname: demo\ndescription: "old desc"\nmetadata:\n  author: a\n---\n\n## Overview\n'


def test_marker_space_does_not_indent_frontmatter_key() -> None:
    # "+ description:" (unified-diff style, space after the marker) used to
    # splice a leading space in front of the key, which makes the whole
    # frontmatter unparseable YAML.
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ name: demo
- description: "old desc"
+ description: "new desc"
*** End Patch"""

    updated, _ = apply_v4a_to_content(FRONTMATTER_MD, patch)

    assert '\ndescription: "new desc"\n' in updated
    assert " description:" not in updated
    block = updated.split("---\n")[1]
    assert yaml.safe_load(block)["description"] == "new desc"


def test_lenient_match_restores_the_files_own_indentation() -> None:
    # The model wrote the hunk flush-left; the replacement must land at the
    # file's indentation rather than being flattened to the model's.
    content = "class A:\n    def f(self):\n        return 1\n"
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ class A
-def f(self):
-    return 1
+def f(self):
+    return 2
*** End Patch"""

    updated, _ = apply_v4a_to_content(content, patch)

    assert updated == "class A:\n    def f(self):\n        return 2\n"


def test_bare_frontmatter_fence_is_context_not_a_deletion() -> None:
    patch = """*** Begin Patch
*** Update File: SKILL.md
@@ frontmatter
---
name: demo
-description: "old desc"
+description: "new desc"
*** End Patch"""

    updated, _ = apply_v4a_to_content(FRONTMATTER_MD, patch)

    assert updated.startswith('---\nname: demo\ndescription: "new desc"\n')
