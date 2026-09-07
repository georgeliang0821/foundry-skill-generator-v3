from __future__ import annotations

import pytest

from backend.sections import (
    ambiguous_titles,
    h2_sections,
    h2_titles,
    match_section,
    normalize_section,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("`[NEEDS_INFO]` 契約", "needsinfo契約"),
        ("⚠️ 身分來源：唯一且不可協商", "身分來源唯一且不可協商"),
        ("API Reference / Sample Code", "apireferencesamplecode"),
        ("Step 1 — 前置", "step1前置"),
        ("Required Inputs", "requiredinputs"),
    ],
)
def test_normalize_matches_the_published_table(title, expected):
    assert normalize_section(title) == expected


def test_lowercase_happens_before_stripping():
    """Stripping first would leave the uppercase letters intact and never match."""
    assert normalize_section("OBO Token Scopes") == "obotokenscopes"


@pytest.mark.parametrize(
    "requested",
    ["[NEEDS_INFO] 契約", "NEEDS_INFO契約", "needsinfo 契約"],
)
def test_decoration_is_optional_when_naming_a_section(requested):
    assert match_section(requested, ["`[NEEDS_INFO]` 契約"]) == ["`[NEEDS_INFO]` 契約"]


def test_match_is_containment_not_equality():
    assert match_section("Required", ["Required Inputs"]) == ["Required Inputs"]
    assert match_section("Required Inputs Extra", ["Required Inputs"]) == []


def test_matches_are_returned_in_document_order():
    titles = ["Zebra 對照", "Alpha 對照"]
    assert match_section("對照", titles) == titles


def test_h2_only_h3_folds_into_its_parent():
    md = """# Title
Opening prose.

## Overview
Body one.

### Detail
Nested body.

## Required Inputs
Body two.
"""
    assert h2_titles(md) == ["Overview", "Required Inputs"]
    overview = dict(h2_sections(md))["Overview"]
    assert "Nested body." in overview
    assert "### Detail" in overview


def test_content_before_the_first_h2_is_unreachable():
    md = """# Title
This prose can never be requested.

## Overview
Body.
"""
    bodies = "\n".join(body for _title, body in h2_sections(md))
    assert "can never be requested" not in bodies


def test_heading_inside_a_code_fence_is_not_a_heading():
    md = """## Overview
```python
## Fake heading
print("x")
```
Still overview.
"""
    assert h2_titles(md) == ["Overview"]
    assert "## Fake heading" in dict(h2_sections(md))["Overview"]


def test_substring_titles_are_ambiguous():
    assert ambiguous_titles(["費用分類對照", "費用分類對照表"]) == [
        ("費用分類對照", "費用分類對照表")
    ]


def test_titles_differing_only_by_decoration_are_ambiguous():
    assert ambiguous_titles(["⚠️ 注意", "注意"]) == [("⚠️ 注意", "注意")]


def test_distinct_titles_are_not_ambiguous():
    assert ambiguous_titles(["Overview", "Required Inputs", "`[NEEDS_INFO]` 契約"]) == []
