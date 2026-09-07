from __future__ import annotations

import pytest

from backend.models import Mode, SkillKind
from backend.topology import (
    METADATA_SIZE_WARN_CHARS,
    Severity,
    declared_children,
    has_errors,
    validate_topology,
)

CAPABILITY_MD = """---
name: hr-leave-system
description: Submits leave requests.
metadata:
  author: e2e
---

## Overview
One line.
"""

SCENARIO_MD = """---
name: leave-workflow
description: Orchestrates the leave request scenario.
metadata:
  author: e2e
  skill_type: scenario-orchestration
  children:
    - hr-leave-system
---

## Overview
One line.
"""


def rules(issues, severity: Severity | None = None) -> set[str]:
    return {i.rule for i in issues if severity is None or i.severity is severity}


def validate(md: str, kind: SkillKind = SkillKind.SCENARIO, **kwargs):
    kwargs.setdefault("mode", Mode.NEW)
    return validate_topology(md, kind, **kwargs)


def test_clean_files_pass() -> None:
    assert validate(SCENARIO_MD) == []
    assert validate(CAPABILITY_MD, SkillKind.CAPABILITY) == []


# --- T1 / T2 / T3: conservative output shape -------------------------------


def test_t1_rejects_byte_order_mark() -> None:
    assert "T1" in rules(validate("\ufeff" + SCENARIO_MD))


def test_t1_rejects_leading_blank_line() -> None:
    assert "T1" in rules(validate("\n" + SCENARIO_MD))


def test_t1_rejects_missing_fence() -> None:
    issues = validate("name: x\ndescription: y\n")
    assert rules(issues) == {"T1"}


def test_t2_rejects_dashes_inside_a_quoted_value() -> None:
    md = SCENARIO_MD.replace(
        "description: Orchestrates the leave request scenario.",
        'description: "Leave --- workflow"',
    )
    assert "T2" in rules(validate(md))


def test_t3_rejects_unquoted_colon_value() -> None:
    md = SCENARIO_MD.replace(
        "description: Orchestrates the leave request scenario.",
        "description: answer: this breaks yaml",
    )
    assert "T3" in rules(validate(md))


def test_b_class_is_a_warning_when_editing_an_existing_skill() -> None:
    issues = validate("\ufeff" + SCENARIO_MD, mode=Mode.MODIFY)
    assert rules(issues, Severity.WARNING) == {"T1"}
    assert not has_errors(issues)


def test_b_class_is_an_error_for_a_new_skill() -> None:
    assert has_errors(validate("\ufeff" + SCENARIO_MD, mode=Mode.NEW))


# --- T4: the shape every known parser accepts ------------------------------


def test_t4_rejects_scalar_children() -> None:
    md = SCENARIO_MD.replace("  children:\n    - hr-leave-system", "  children: hr-leave-system")
    assert "T4" in rules(validate(md))


def test_t4_rejects_empty_children() -> None:
    md = SCENARIO_MD.replace("  children:\n    - hr-leave-system", "  children: []")
    assert "T4" in rules(validate(md))


def test_t4_rejects_top_level_children() -> None:
    md = SCENARIO_MD.replace("  children:\n    - hr-leave-system", "").replace(
        "metadata:", "children:\n  - hr-leave-system\nmetadata:"
    )
    assert "T4" in rules(validate(md))


def test_t4_rejects_missing_children() -> None:
    md = SCENARIO_MD.replace("  children:\n    - hr-leave-system\n", "")
    assert "T4" in rules(validate(md))


def test_t4_rejects_non_string_child_entries() -> None:
    md = SCENARIO_MD.replace("    - hr-leave-system", "    - 42")
    assert "T4" in rules(validate(md))


# --- T5 -------------------------------------------------------------------


def test_t5_rejects_children_on_a_capability_skill() -> None:
    assert "T5" in rules(validate(SCENARIO_MD, SkillKind.CAPABILITY))


# --- T6 -------------------------------------------------------------------


def test_t6_requires_frontmatter_name_to_match_the_stored_name() -> None:
    issues = validate(SCENARIO_MD, expected_name="renamed-workflow")
    assert "T6" in rules(issues)


def test_t6_passes_when_names_match() -> None:
    assert validate(SCENARIO_MD, expected_name="leave-workflow") == []


# --- T7 -------------------------------------------------------------------


def test_t7_reports_a_missing_or_inaccessible_child() -> None:
    issues = validate(SCENARIO_MD, child_resolver=lambda _name: None)
    assert "T7" in rules(issues)


def test_t7_rejects_a_grandchild() -> None:
    nested = SCENARIO_MD.replace("name: leave-workflow", "name: hr-leave-system")
    issues = validate(SCENARIO_MD, child_resolver=lambda _name: nested)
    assert "T7" in rules(issues)


def test_t7_passes_for_a_leaf_child() -> None:
    assert validate(SCENARIO_MD, child_resolver=lambda _name: CAPABILITY_MD) == []


def test_t7_is_skipped_without_a_resolver() -> None:
    assert "T7" not in rules(validate(SCENARIO_MD))


# --- T8 -------------------------------------------------------------------


def test_t8_warns_on_oversized_metadata() -> None:
    md = SCENARIO_MD.replace("  author: e2e", f"  author: {'x' * (METADATA_SIZE_WARN_CHARS + 100)}")
    issues = validate(md)
    assert rules(issues, Severity.WARNING) == {"T8"}
    assert not has_errors(issues)


# --- T9 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "heading",
    ["## Environment Variables", "## OBO Token Scopes", "## API Reference / Sample Code"],
)
def test_t9_rejects_capability_only_sections(heading: str) -> None:
    assert "T9" in rules(validate(SCENARIO_MD + f"\n{heading}\n\n- FOO\n"))


def test_t9_does_not_apply_to_capability_skills() -> None:
    md = CAPABILITY_MD + "\n## Environment Variables\n\n- FOO\n"
    assert "T9" not in rules(validate(md, SkillKind.CAPABILITY))


# --- T10: the only rule that catches children deleted during REFINE --------


def test_t10_rejects_skill_type_without_children() -> None:
    md = SCENARIO_MD.replace("  children:\n    - hr-leave-system\n", "")
    assert "T10" in rules(validate(md))


def test_t10_rejects_children_without_skill_type() -> None:
    md = SCENARIO_MD.replace("  skill_type: scenario-orchestration\n", "")
    assert "T10" in rules(validate(md))


def test_t10_stays_quiet_for_a_plain_capability_skill() -> None:
    assert "T10" not in rules(validate(CAPABILITY_MD, SkillKind.CAPABILITY))


# --- P class: the scenario layer's pointer contract ------------------------


POINTER_CHILD_MD = """---
name: hr-leave-system
description: Submits leave requests.
metadata:
  author: e2e
---

## Overview
One line.

## Required Inputs
- `HR_LEAVE_JSON` (required) -- the serialized request.

## `[NEEDS_INFO]` 契約
- `HR_LEAVE_JSON` -- rebuild the payload and retry.
"""

PEER_MD = CAPABILITY_MD.replace("name: hr-leave-system", "name: ms-graph-calendar")

POINTER_PARENT_MD = SCENARIO_MD + """
## Fetching the dependency
These skills do not appear in your `list_skills` catalog. Not being listed does
not mean you may not retrieve their body. Fetch each one exactly once.

fetch_skill(skill_name="hr-leave-system", sections="Required Inputs, [NEEDS_INFO] 契約")
"""


def child_of(**overrides):
    table = {"hr-leave-system": POINTER_CHILD_MD, "ms-graph-calendar": PEER_MD}
    table.update(overrides)
    return lambda name: table.get(name)


def test_a_well_formed_pointer_passes() -> None:
    assert validate(POINTER_PARENT_MD, child_resolver=child_of()) == []


def test_p_rules_are_skipped_without_a_resolver() -> None:
    assert not {r for r in rules(validate(POINTER_PARENT_MD)) if r.startswith("P")}


def test_p1_rejects_a_pointer_at_an_undeclared_skill() -> None:
    md = POINTER_PARENT_MD.replace('skill_name="hr-leave-system"', 'skill_name="html-ppt"')
    assert "P1" in rules(validate(md, child_resolver=child_of()))


def test_p2_rejects_an_unknown_section_and_lists_the_real_ones() -> None:
    md = POINTER_PARENT_MD.replace("Required Inputs,", "Payload Fields,")
    issues = validate(md, child_resolver=child_of())
    p2 = [i for i in issues if i.rule == "P2"]
    assert len(p2) == 1
    assert "Payload Fields" in p2[0].message
    assert "`Required Inputs`" in p2[0].message
    assert "`Overview`" in p2[0].message


def test_p2_accepts_a_section_named_without_its_decoration() -> None:
    md = POINTER_PARENT_MD.replace("[NEEDS_INFO] 契約", "NEEDS_INFO契約")
    assert "P2" not in rules(validate(md, child_resolver=child_of()))


def test_p2_is_silent_when_no_sections_are_requested() -> None:
    md = POINTER_PARENT_MD.replace(
        ', sections="Required Inputs, [NEEDS_INFO] 契約"', ""
    )
    assert "P2" not in rules(validate(md, child_resolver=child_of()))


def test_p3_rejects_fetching_the_same_child_twice() -> None:
    md = POINTER_PARENT_MD + '\nfetch_skill(skill_name="hr-leave-system", sections="Overview")\n'
    assert "P3" in rules(validate(md, child_resolver=child_of()))


def test_p4_requires_the_authorization_paragraph() -> None:
    md = POINTER_PARENT_MD.replace("`list_skills` catalog", "catalog")
    assert "P4" in rules(validate(md, child_resolver=child_of()))


def test_p5_reports_a_real_skill_missing_from_children() -> None:
    md = POINTER_PARENT_MD + "\n## Step 1\nSend the invite with `ms-graph-calendar`.\n"
    assert "P5" in rules(validate(md, child_resolver=child_of()))


def test_p5_ignores_a_skill_named_as_a_routing_alternative() -> None:
    md = POINTER_PARENT_MD + "\n## Not applicable\nBooking a meeting -> use `ms-graph-calendar`.\n"
    assert "P5" not in rules(validate(md, child_resolver=child_of()))


def test_p5_ignores_a_hyphenated_token_that_is_not_a_skill() -> None:
    md = POINTER_PARENT_MD + "\n## Step 1\nUse the `end-to-end` flow.\n"
    assert "P5" not in rules(validate(md, child_resolver=child_of()))


def test_p6_warns_when_the_parent_restates_a_child_field() -> None:
    md = POINTER_PARENT_MD + "\n## Step 1\nPut the payload in `HR_LEAVE_JSON`.\n"
    issues = validate(md, child_resolver=child_of())
    assert rules(issues, Severity.WARNING) == {"P6"}
    assert not has_errors(issues)


def test_p6_does_not_fire_on_the_return_branch_table() -> None:
    md = POINTER_PARENT_MD + (
        "\n## Return-branch table\n"
        "| `[NEEDS_INFO] missing=HR_LEAVE_JSON` | Rebuild and retry. |\n"
    )
    assert "P6" not in rules(validate(md, child_resolver=child_of()))


# --- C class: a capability's section names are a published API --------------


def capability(body: str) -> str:
    return CAPABILITY_MD + body


def test_c1_rejects_a_section_name_contained_in_another() -> None:
    md = capability("\n## 費用分類對照\nx\n\n## 費用分類對照表\ny\n")
    assert "C1" in rules(validate(md, SkillKind.CAPABILITY))


def test_c1_rejects_sections_separated_only_by_decoration() -> None:
    md = capability("\n## ⚠️ 注意\nx\n\n## 注意\ny\n")
    assert "C1" in rules(validate(md, SkillKind.CAPABILITY))


def test_c1_also_applies_to_a_scenario_skill() -> None:
    md = SCENARIO_MD + "\n## Step 1\nx\n\n## Step 1 — 前置\ny\n"
    assert "C1" in rules(validate(md))


def test_c2_rejects_a_pointable_section_below_h2() -> None:
    md = capability("\n### Required Inputs\n- `FOO`\n")
    assert "C2" in rules(validate(md, SkillKind.CAPABILITY))


def test_c2_requires_a_dedicated_needs_info_section() -> None:
    md = capability(
        '\n## Required Inputs\n- `FOO`\n\n## Sample\n```python\nprint("[NEEDS_INFO] missing=FOO")\n```\n'
    )
    assert "C2" in rules(validate(md, SkillKind.CAPABILITY))


def test_c2_requires_that_section_to_list_every_code() -> None:
    md = capability(
        "\n## `[NEEDS_INFO]` 契約\n- `FOO`\n\n"
        '## Sample\n```python\nprint("[NEEDS_INFO] missing=FOO,BAR")\n```\n'
    )
    issues = validate(md, SkillKind.CAPABILITY)
    assert "C2" in rules(issues)
    assert "`BAR`" in next(i for i in issues if i.rule == "C2").message


def test_c2_passes_when_the_contract_is_complete() -> None:
    md = capability(
        "\n## `[NEEDS_INFO]` 契約\n- `FOO`\n- `BAR`\n\n"
        '## Sample\n```python\nprint("[NEEDS_INFO] missing=FOO,BAR")\n```\n'
    )
    assert "C2" not in rules(validate(md, SkillKind.CAPABILITY))


def test_c3_warns_about_content_above_the_first_section() -> None:
    md = CAPABILITY_MD.replace("\n## Overview", "\nThis prose is unreachable.\n\n## Overview")
    issues = validate(md, SkillKind.CAPABILITY)
    assert rules(issues, Severity.WARNING) == {"C3"}
    assert not has_errors(issues)


def test_c3_ignores_an_h1_title() -> None:
    md = CAPABILITY_MD.replace("\n## Overview", "\n# hr-leave-system\n\n## Overview")
    assert "C3" not in rules(validate(md, SkillKind.CAPABILITY))


# --- helper ---------------------------------------------------------------


@pytest.mark.parametrize(
    "children, expected",
    [
        (["a", "b"], ["a", "b"]),
        ("a", []),
        ([], []),
        (("a", "b"), []),
        ([""], []),
        (None, []),
    ],
)
def test_declared_children_accepts_only_a_non_empty_list_of_strings(children, expected) -> None:
    assert declared_children({"metadata": {"children": children}}) == expected


def test_declared_children_ignores_a_top_level_key() -> None:
    assert declared_children({"children": ["a"]}) == []
