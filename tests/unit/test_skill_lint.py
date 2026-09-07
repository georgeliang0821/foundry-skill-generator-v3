from __future__ import annotations

from pathlib import Path

import pytest

from backend.models import SkillKind
from backend.skill_lint import (
    _child_operations,
    _documented_values,
    _field_annotations,
    has_lint_errors,
    lint_skill,
)


REPO = Path(__file__).resolve().parents[2]
CAPABILITY = REPO / "skills" / "hr-leave-requests" / "SKILL.md"
SCENARIO = REPO / "skills" / "leave-request-orchestration" / "SKILL.md"


def _rules(issues) -> set[str]:
    return {issue.rule for issue in issues}


CLEAN_CAPABILITY = """---
name: clean-skill
description: "A clean capability skill."
metadata:
  author: a@b.c
---

## Overview
Does one thing.

## When NOT to Use This Skill
Anything else -> use `other-skill`

## Required Inputs

- `QUERY_JSON` (required): the serialized request. Missing -> `[NEEDS_INFO] missing=QUERY_JSON`.

## Environment Variables

- `API_HOST` (required): the host.

## OBO Token Scopes

- `API_ACCESS_TOKEN` (required): the user token.

## API Reference / Sample Code

```python
import os


def main() -> None:
    raw = os.environ.get("QUERY_JSON")
    if not raw:
        print("[NEEDS_INFO] missing=QUERY_JSON")
        print("Provide the request payload.")
        raise SystemExit(0)
    host = os.environ["API_HOST"]
    token = os.environ["API_ACCESS_TOKEN"]
    print(f"Asked {host} with {len(token)} chars of token for {raw}.")
```
"""


def test_clean_capability_has_no_findings() -> None:
    assert lint_skill(CLEAN_CAPABILITY, SkillKind.CAPABILITY) == []


def test_a1_reports_unparseable_code_as_an_error() -> None:
    broken = CLEAN_CAPABILITY.replace("def main() -> None:", "def main( -> None:")
    issues = lint_skill(broken, SkillKind.CAPABILITY)
    assert [i.rule for i in issues] == ["A1"]
    assert issues[0].severity == "error"


def test_a1_short_circuits_the_other_rules() -> None:
    """A syntax error makes every AST-based rule meaningless, so only A1 is reported."""
    broken = CLEAN_CAPABILITY.replace("import os", "import os\nraise ValueError(")
    assert _rules(lint_skill(broken, SkillKind.CAPABILITY)) == {"A1"}


def test_a2_reports_a_declared_variable_the_code_never_reads() -> None:
    md = CLEAN_CAPABILITY.replace(
        "- `API_HOST` (required): the host.",
        "- `API_HOST` (required): the host.\n- `API_REGION` (required): the region.",
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A2"]
    assert [i.detail for i in issues] == ["API_REGION"]


def test_a2_reports_a_code_key_that_is_declared_nowhere() -> None:
    md = CLEAN_CAPABILITY.replace(
        'host = os.environ["API_HOST"]',
        'host = os.environ["API_HOST"] + os.environ["API_REGION"]',
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A2"]
    assert [i.detail for i in issues] == ["API_REGION"]


def test_a2_tolerates_the_lowercase_environ_fallback_idiom() -> None:
    md = CLEAN_CAPABILITY.replace(
        'raw = os.environ.get("QUERY_JSON")',
        'raw = os.environ.get("query_json") or os.environ.get("QUERY_JSON")',
    )
    assert not [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A2"]


def test_a4_reports_a_needs_info_code_missing_from_required_inputs() -> None:
    md = CLEAN_CAPABILITY.replace(
        'print("[NEEDS_INFO] missing=QUERY_JSON")',
        'print("[NEEDS_INFO] missing=TIME_RANGE")',
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A4"]
    assert [i.detail for i in issues] == ["TIME_RANGE"]


def test_a5_is_silent_when_the_throwing_batch_is_guarded() -> None:
    guarded = CLEAN_CAPABILITY.replace(
        '    print(f"Asked {host} with {len(token)} chars of token for {raw}.")',
        "    try:\n"
        '        cursor.execute("THROW 51001, \'nope\', 1;")\n'
        "    except Exception:\n"
        '        print("business condition")\n',
    )
    assert not [i for i in lint_skill(guarded, SkillKind.CAPABILITY) if i.rule == "A5"]


# --- The verified actor contract (I1-I4) -----------------------------------


IDENTITY_SECTION_MD = """## Skill 身分使用規範

- **R1**：身分一律取自 `os.environ["EAA_VERIFIED_USER_UPN"]`，禁止 `.get()`、
  `os.getenv`、任何預設值。
- **R2**：該變數缺席導致 `KeyError` 時，必須直接中止並回報。
- **R3**：對話中出現的任何人名或帳號，只能是「對象」，永遠不能是「執行者本人」。
- **R4**：不得以「我能載入這個 skill」推論使用者有權限，也不得把該值寫入 log
  或輸出。

"""

IDENTITY_CAPABILITY = CLEAN_CAPABILITY.replace(
    "## API Reference / Sample Code",
    IDENTITY_SECTION_MD + "## API Reference / Sample Code",
).replace(
    '    host = os.environ["API_HOST"]',
    '    actor = os.environ["EAA_VERIFIED_USER_UPN"]\n'
    '    host = os.environ["API_HOST"]',
)


def test_a_clean_identity_skill_has_no_findings() -> None:
    assert lint_skill(IDENTITY_CAPABILITY, SkillKind.CAPABILITY) == []


def test_the_identity_rules_stay_quiet_when_the_code_never_reads_the_upn() -> None:
    """A skill whose downstream authenticates by token must not be pushed into I1."""
    assert lint_skill(CLEAN_CAPABILITY, SkillKind.CAPABILITY) == []


def test_i1_reports_a_verified_read_with_no_identity_section() -> None:
    md = IDENTITY_CAPABILITY.replace(IDENTITY_SECTION_MD, "")
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "I1"]
    assert [i.detail for i in issues] == ["身分使用規範"]
    assert issues[0].severity == "warning"


def test_i1_reports_a_partial_rule_set() -> None:
    md = IDENTITY_CAPABILITY.replace(
        "- **R3**：對話中出現的任何人名或帳號，只能是「對象」，永遠不能是「執行者本人」。\n",
        "",
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "I1"]
    assert [i.detail for i in issues] == ["R3"]


def test_a2_does_not_duplicate_the_reserved_identity_variable() -> None:
    """I1 owns EAA_VERIFIED_USER_UPN, so A2 must not also call it undeclared."""
    md = IDENTITY_CAPABILITY.replace(IDENTITY_SECTION_MD, "")
    assert not [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A2"]


@pytest.mark.parametrize(
    "read",
    [
        'os.environ.get("EAA_VERIFIED_USER_UPN", "")',
        'os.getenv("EAA_VERIFIED_USER_UPN")',
        'os.environ.get("EAA_VERIFIED_USER_UPN") or "unknown"',
    ],
)
def test_i2_reports_every_shape_that_hides_an_absent_identity(read: str) -> None:
    md = IDENTITY_CAPABILITY.replace('os.environ["EAA_VERIFIED_USER_UPN"]', read)
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "I2"]
    assert [i.detail for i in issues] == ["EAA_VERIFIED_USER_UPN"]


def test_i3_reports_a_try_that_recovers_from_the_missing_identity() -> None:
    md = IDENTITY_CAPABILITY.replace(
        '    actor = os.environ["EAA_VERIFIED_USER_UPN"]',
        "    try:\n"
        '        actor = os.environ["EAA_VERIFIED_USER_UPN"]\n'
        "    except KeyError:\n"
        '        actor = "anonymous"',
    )
    assert "I3" in _rules(lint_skill(md, SkillKind.CAPABILITY))


def test_i3_allows_a_try_that_guards_something_else() -> None:
    md = IDENTITY_CAPABILITY.replace(
        '    host = os.environ["API_HOST"]',
        "    try:\n"
        '        host = os.environ["API_HOST"]\n'
        "    except KeyError:\n"
        '        host = "localhost"',
    )
    assert "I3" not in _rules(lint_skill(md, SkillKind.CAPABILITY))


def test_i4_reports_the_identity_reaching_a_print() -> None:
    md = IDENTITY_CAPABILITY.replace(
        '    print(f"Asked {host} with {len(token)} chars of token for {raw}.")',
        '    print(f"Asked {host} as {actor}.")',
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "I4"]
    assert [i.detail for i in issues] == ["print"]


def test_i4_reports_the_identity_reaching_a_logger() -> None:
    md = IDENTITY_CAPABILITY.replace(
        '    host = os.environ["API_HOST"]',
        '    logger.info("actor=%s", actor)\n'
        '    host = os.environ["API_HOST"]',
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "I4"]
    assert [i.detail for i in issues] == ["info"]


def test_i4_allows_using_the_identity_as_a_downstream_field() -> None:
    md = IDENTITY_CAPABILITY.replace(
        '    host = os.environ["API_HOST"]',
        '    payload = {"submitted_by": actor}\n'
        '    host = os.environ["API_HOST"]',
    )
    assert "I4" not in _rules(lint_skill(md, SkillKind.CAPABILITY))


# --- The caller-facing value domain (A6-A8) --------------------------------


VALUE_DOMAIN_MD = """---
name: value-domain-skill
description: "A skill with one caller field that reaches a query."
metadata:
  author: a@b.c
---

## Overview
Reads a balance.

## When NOT to Use This Skill
Anything else -> use `other-skill`

## Required Inputs

- `QUERY_JSON` (required): the serialized request. Missing -> `[NEEDS_INFO] missing=QUERY_JSON`.
  It must carry `operation`, `leave_type`（字串）、`start_date`（`YYYY-MM-DD`）。

## Environment Variables

- `API_HOST` (required): the host.

## API Reference / Sample Code

```python
import json
import os


def main() -> None:
    payload = json.loads(os.environ["QUERY_JSON"])
    host = os.environ["API_HOST"]
    leave_type = payload["leave_type"]
    start_date = payload["start_date"]
    cursor = connect(host).cursor()
    cursor.execute(
        "SELECT days FROM hr.v_balance WHERE leave_type = ? AND start_date = ?",
        (leave_type, start_date),
    )
    print(f"You have {cursor.fetchone()} days left.")
```
"""

DOCUMENTED_ENUM = "`leave_type`（`annual`、`sick`）"
TYPE_ONLY = "`leave_type`（字串）"


def _a(issues, rule: str) -> list[str]:
    return [i.detail for i in issues if i.rule == rule]


def test_a7_reports_a_field_documented_by_type_alone() -> None:
    """The RCA in one assertion: `leave_type`（字串）is a shape, not a contract."""
    issues = lint_skill(VALUE_DOMAIN_MD, SkillKind.CAPABILITY)
    assert _a(issues, "A7") == ["leave_type"]
    assert "VALUE DOMAIN" in next(i.message for i in issues if i.rule == "A7")


def test_a7_accepts_a_stated_format() -> None:
    """`start_date` reaches the same query but its contract states the format."""
    assert "start_date" not in _a(lint_skill(VALUE_DOMAIN_MD, SkillKind.CAPABILITY), "A7")


def test_a7_accepts_a_field_declared_as_free_text() -> None:
    md = VALUE_DOMAIN_MD.replace(TYPE_ONLY, "`leave_type`（字串，自由文字）")
    assert _a(lint_skill(md, SkillKind.CAPABILITY), "A7") == []


def test_a7_ignores_a_field_that_never_reaches_a_query() -> None:
    md = VALUE_DOMAIN_MD.replace(
        '    start_date = payload["start_date"]',
        '    start_date = payload["start_date"]\n    print(payload.get("note"))',
    )
    assert "note" not in _a(lint_skill(md, SkillKind.CAPABILITY), "A7")


def test_a7_ignores_a_field_the_code_coerces_to_a_number() -> None:
    md = VALUE_DOMAIN_MD.replace(
        '    start_date = payload["start_date"]',
        '    start_date = payload["start_date"]\n    days = float(payload["days"])',
    ).replace("(leave_type, start_date),", "(leave_type, start_date, days),")
    assert "days" not in _a(lint_skill(md, SkillKind.CAPABILITY), "A7")


def test_a7_reports_a_query_field_the_contract_never_names() -> None:
    md = VALUE_DOMAIN_MD.replace(
        "(leave_type, start_date),", '(leave_type, start_date, payload["status"]),'
    )
    issues = [i for i in lint_skill(md, SkillKind.CAPABILITY) if i.rule == "A7"]
    assert "status" in {i.detail for i in issues}
    assert "never names it" in next(i.message for i in issues if i.detail == "status")


def test_a8_reports_a_documented_enum_the_code_never_checks() -> None:
    md = VALUE_DOMAIN_MD.replace(TYPE_ONLY, DOCUMENTED_ENUM)
    issues = lint_skill(md, SkillKind.CAPABILITY)
    assert _a(issues, "A7") == []
    assert _a(issues, "A8") == ["leave_type"]


def test_a8_is_silent_once_the_code_checks_membership() -> None:
    md = VALUE_DOMAIN_MD.replace(TYPE_ONLY, DOCUMENTED_ENUM).replace(
        '    leave_type = payload["leave_type"]',
        '    leave_type = payload["leave_type"]\n'
        '    if leave_type not in {"annual", "sick"}:\n'
        '        print("[NEEDS_INFO] missing=QUERY_JSON")\n'
        "        raise SystemExit(0)",
    )
    assert _rules(lint_skill(md, SkillKind.CAPABILITY)) & {"A6", "A7", "A8"} == set()


def test_a6_reports_a_value_the_code_accepts_but_the_contract_omits() -> None:
    md = VALUE_DOMAIN_MD.replace(TYPE_ONLY, DOCUMENTED_ENUM).replace(
        '    leave_type = payload["leave_type"]',
        '    leave_type = payload["leave_type"]\n'
        '    if leave_type not in {"annual", "sick", "welfare"}:\n'
        '        print("[NEEDS_INFO] missing=QUERY_JSON")\n'
        "        raise SystemExit(0)",
    )
    assert _a(lint_skill(md, SkillKind.CAPABILITY), "A6") == ["leave_type: welfare"]


def test_a6_resolves_a_membership_test_against_a_module_constant() -> None:
    md = VALUE_DOMAIN_MD.replace(TYPE_ONLY, DOCUMENTED_ENUM).replace(
        "def main() -> None:",
        'LEGAL_TYPES = {"annual", "sick", "welfare"}\n\n\ndef main() -> None:',
    ).replace(
        '    leave_type = payload["leave_type"]',
        '    leave_type = payload["leave_type"]\n'
        "    if leave_type not in LEGAL_TYPES:\n"
        "        raise SystemExit(0)",
    )
    assert _a(lint_skill(md, SkillKind.CAPABILITY), "A6") == ["leave_type: welfare"]


def test_a_field_annotation_keeps_its_bracketed_value_list() -> None:
    """The load-bearing parse: values in brackets belong to the field before them."""
    body = "- `submit_request`: `leave_type` (`annual`, `sick`), `start_date` (`YYYY-MM-DD`)\n"
    assert _documented_values(" ".join(_field_annotations(body, "leave_type"))) == [
        "annual",
        "sick",
    ]
    assert _documented_values(" ".join(_field_annotations(body, "start_date"))) == [
        "YYYY-MM-DD"
    ]


# --- The two authored skills are the known-bad corpus ----------------------


@pytest.fixture()
def capability_md() -> str:
    return CAPABILITY.read_text(encoding="utf-8")


@pytest.fixture()
def scenario_md() -> str:
    return SCENARIO.read_text(encoding="utf-8")


def test_authored_capability_reports_its_known_defects(capability_md: str) -> None:
    issues = lint_skill(capability_md, SkillKind.CAPABILITY)
    rules = _rules(issues)
    # A3: parse_payload raises ValueError for a caller-payload problem.
    assert "A3" in rules
    assert "ValueError" in next(i.detail for i in issues if i.rule == "A3")
    # A4: the CALENDAR_EVENTS needs-info code is never documented as a code.
    assert "CALENDAR_EVENTS" in {i.detail for i in issues if i.rule == "A4"}
    # A5: the submit batch's THROW 51001 is not wrapped in a try.
    assert "A5" in rules
    # A7: `leave_type` goes into the balance query with no legal values stated,
    # which is how a caller's 「福利假」 became a no_entitlement answer.
    assert "leave_type" in {i.detail for i in issues if i.rule == "A7"}
    # Its variable declarations are closed, so A2 must stay quiet.
    assert "A2" not in rules


def test_authored_scenario_reports_the_step_table_contradiction(
    scenario_md: str, capability_md: str
) -> None:
    issues = lint_skill(
        scenario_md,
        SkillKind.SCENARIO,
        child_full_md={"hr-leave-requests": capability_md},
        host_capabilities=["Work IQ Calendar MCP occurrences"],
    )
    b1 = [i for i in issues if i.rule == "B1"]
    assert len(b1) == 1
    assert b1[0].detail == "step 2: Work IQ Calendar MCP occurrences"


def test_authored_scenario_return_branch_table_is_currently_exhaustive(
    scenario_md: str, capability_md: str
) -> None:
    """Regression guard: every branch the child can emit has a row today."""
    issues = lint_skill(
        scenario_md,
        SkillKind.SCENARIO,
        child_full_md={"hr-leave-requests": capability_md},
    )
    assert [i.detail for i in issues if i.rule == "B2"] == []


def test_b2_reports_a_branch_with_no_row(scenario_md: str, capability_md: str) -> None:
    child = capability_md.replace('"error": "past_date"', '"error": "employee_not_found"')
    issues = lint_skill(
        scenario_md, SkillKind.SCENARIO, child_full_md={"hr-leave-requests": child}
    )
    assert [i.detail for i in issues if i.rule == "B2"] == [
        "hr-leave-requests: employee_not_found"
    ]


def test_child_operations_reads_the_membership_test(capability_md: str) -> None:
    assert _child_operations(capability_md) == [
        "get_balance",
        "submit_request",
        "list_requests",
    ]


@pytest.mark.parametrize(
    "code, expected",
    [
        ('if operation not in {"a", "b"}:\n    pass\n', ["a", "b"]),
        ('if operation in ["a", "b"]:\n    pass\n', ["a", "b"]),
        ('if operation == "solo":\n    pass\n', ["solo"]),
        ('if payload["operation"] == "sub":\n    pass\n', ["sub"]),
        ('if payload.get("operation") == "got":\n    pass\n', ["got"]),
        ('if mode == "not_an_operation":\n    pass\n', []),
        ("if operation ==\n", []),
    ],
)
def test_child_operations_shapes(code: str, expected: list[str]) -> None:
    assert _child_operations(f"```python\n{code}```") == expected


def test_child_operations_without_python_is_silent() -> None:
    assert _child_operations("## Overview\n\nNo code here.\n") == []


def test_authored_scenario_covers_every_child_operation(
    scenario_md: str, capability_md: str
) -> None:
    """Regression guard: no child operation is unreachable through the scenario today."""
    issues = lint_skill(
        scenario_md, SkillKind.SCENARIO, child_full_md={"hr-leave-requests": capability_md}
    )
    assert [i.detail for i in issues if i.rule == "B4"] == []


def test_b4_reports_an_operation_the_scenario_never_mentions(
    scenario_md: str, capability_md: str
) -> None:
    parent = scenario_md.replace("get_balance", "查餘額")
    issues = lint_skill(
        parent, SkillKind.SCENARIO, child_full_md={"hr-leave-requests": capability_md}
    )
    assert [i.detail for i in issues if i.rule == "B4"] == ["hr-leave-requests: get_balance"]


def test_b4_is_a_warning(scenario_md: str, capability_md: str) -> None:
    parent = scenario_md.replace("get_balance", "查餘額")
    issues = lint_skill(
        parent, SkillKind.SCENARIO, child_full_md={"hr-leave-requests": capability_md}
    )
    assert not has_lint_errors(issues)


def test_capability_rules_never_run_against_a_scenario(scenario_md: str) -> None:
    assert not [
        i for i in lint_skill(scenario_md, SkillKind.SCENARIO) if i.rule.startswith("A")
    ]


def test_empty_skill_md_is_not_linted() -> None:
    assert lint_skill("   ", SkillKind.CAPABILITY) == []
