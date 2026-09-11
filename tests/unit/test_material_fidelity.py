from __future__ import annotations

from backend.material_fidelity import (
    MATERIAL_PROMPT_MAX_CHARS,
    MATERIALS_PROMPT_TOTAL_MAX_CHARS,
    materials_for_prompt,
    scan_material_fidelity,
)
from backend.models import Material, MaterialKind

# Condensed from the reference implementation a user pasted as a material. The
# generator replaced all of it with invented SQL and invented payload fields,
# which is the regression this scanner exists to catch.
MATERIAL_CODE = '''
import os
import pyodbc

BLOCKING_STATUSES = {"accepted", "tentative", "organizer"}


def validate_event_ids(event_ids):
    """Refuse ids the caller fabricated instead of reading from the calendar."""
    return [e for e in event_ids if e]


def _safe_fetch_first_result(cursor):
    """A batch returns several result sets; fetchone() alone grabs the wrong one."""
    while True:
        row = cursor.fetchone()
        if row is not None:
            return row
        if not cursor.nextset():
            return None


def detect_conflicts(events, start_date, end_date):
    for event in events:
        if event.get("response_status") in BLOCKING_STATUSES:
            yield event


def submit_leave(conn, upn, leave_type, start_date, end_date):
    cursor = conn.cursor()
    cursor.execute(
        """
        DECLARE @id INT = NEXT VALUE FOR hr.seq_leave_request;
        INSERT INTO hr.leave_request (id, upn, leave_type)
        VALUES (@id, ?, ?);
        SELECT @id;
        """,
        upn,
        leave_type,
    )
    return _safe_fetch_first_result(cursor)
'''

# What the generator actually produced: a stored procedure that exists nowhere,
# a payload field that exists nowhere, and none of the material's helpers.
BAD_DRAFT = """---
name: hr-leave-system
---

## API Reference / Sample Code

```python
import os
import pyodbc


def main():
    events = fetch_events()
    conflicts = [e for e in events if e.get("is_conflict") is True]
    conn = pyodbc.connect(os.environ["HR_SQL_CONN"])
    cursor = conn.cursor()
    cursor.execute("EXEC hr.submit_self_leave_request ?, ?", "annual", "2024-01-01")
    row = cursor.fetchone()
    print(row)
```
"""


def _material(content: str, kind: MaterialKind = MaterialKind.CODE) -> Material:
    return Material(id=f"m-{kind.value}", kind=kind, content=content)


def _rules(issues) -> set[str]:
    return {issue.rule for issue in issues}


def _details(issues, rule: str) -> set[str]:
    return {issue.detail for issue in issues if issue.rule == rule}


def test_it_catches_the_drift_that_motivated_the_check() -> None:
    issues = scan_material_fidelity(BAD_DRAFT, [_material(MATERIAL_CODE)])

    assert "hr.submit_self_leave_request" in _details(issues, "invented_sql_object")
    assert "is_conflict" in _details(issues, "invented_payload_field")
    dropped = _details(issues, "dropped_from_material")
    assert {"validate_event_ids", "_safe_fetch_first_result", "detect_conflicts"} <= dropped
    assert all(issue.severity == "warning" for issue in issues)


def test_a_draft_that_reuses_the_material_is_clean() -> None:
    faithful = f"## API Reference / Sample Code\n\n```python\n{MATERIAL_CODE}\n```\n"

    assert scan_material_fidelity(faithful, [_material(MATERIAL_CODE)]) == []


def test_prose_materials_are_never_a_source_of_code_detail() -> None:
    context_only = [
        _material("Employees may take up to 14 days of annual leave.", MaterialKind.TEXT),
        _material("https://example.invalid/hr-policy", MaterialKind.URL),
    ]

    assert scan_material_fidelity(BAD_DRAFT, context_only) == []


def test_no_materials_means_nothing_to_check() -> None:
    assert scan_material_fidelity(BAD_DRAFT, []) == []
    assert scan_material_fidelity(BAD_DRAFT, None) == []


def test_a_draft_without_python_code_is_not_scanned() -> None:
    assert scan_material_fidelity("# just prose", [_material(MATERIAL_CODE)]) == []


def test_api_spec_identifiers_are_authoritative_but_its_symbols_are_not_required() -> None:
    spec = _material("def documented_helper(): ...\nhr.leave_request", MaterialKind.API_SPEC)

    issues = scan_material_fidelity(BAD_DRAFT, [spec])

    assert "dropped_from_material" not in _rules(issues)
    assert "invented_sql_object" in _rules(issues)


def test_an_oversized_material_is_truncated_with_an_explicit_marker() -> None:
    material = _material("x" * (MATERIAL_PROMPT_MAX_CHARS + 500))

    (_m, text, truncated), = materials_for_prompt([material])

    assert truncated is True
    assert "<<<TRUNCATED:" in text
    assert "Do NOT invent the missing part" in text
    assert len(text) < len(material.content)


def test_the_total_budget_bounds_the_whole_set() -> None:
    materials = [_material("y" * MATERIAL_PROMPT_MAX_CHARS) for _ in range(5)]

    prepared = materials_for_prompt(materials)
    visible = sum(len(text) for _m, text, _t in prepared)

    # Only the truncation markers exceed the budget, and each is short.
    assert visible < MATERIALS_PROMPT_TOTAL_MAX_CHARS + 5 * 300


def test_truncation_is_reported_so_findings_can_be_read_correctly() -> None:
    material = _material(MATERIAL_CODE + "z" * MATERIAL_PROMPT_MAX_CHARS)

    issues = scan_material_fidelity(BAD_DRAFT, [material])

    assert "material_truncated" in _rules(issues)
    assert all(i.severity == "info" for i in issues if i.rule == "material_truncated")


# --- Closed value sets the material states and the contract omits -----------

DDL_MATERIAL = """
CREATE TABLE hr.leave_request (
    request_id  NVARCHAR(30)  NOT NULL,
    leave_type  NVARCHAR(20)  NOT NULL
        CONSTRAINT CK_leave_type CHECK (leave_type IN (N'annual', N'welfare', N'sick')),
    start_date  DATE          NOT NULL
);
"""

SPEC_MATERIAL = """
components:
  schemas:
    LeaveRequest:
      properties:
        leave_type:
          type: string
          enum: [annual, welfare, sick]
"""


def _draft(contract: str) -> str:
    return f"""## Required Inputs

- `HR_JSON` (required): the request. {contract}

## API Reference / Sample Code

```python
def main():
    leave_type = payload["leave_type"]
    cursor.execute("SELECT 1 FROM hr.leave_request WHERE leave_type = ?", leave_type)
```
"""


def test_a_check_constraint_the_contract_never_lists_is_reported() -> None:
    issues = scan_material_fidelity(
        _draft("必填 `leave_type`（字串）。"), [_material(DDL_MATERIAL, MaterialKind.API_SPEC)]
    )

    assert _details(issues, "undocumented_enum") == {
        "leave_type: annual, welfare, sick"
    }


def test_an_api_spec_enum_block_is_read_the_same_way() -> None:
    issues = scan_material_fidelity(
        _draft("必填 `leave_type`（字串）。"),
        [_material(SPEC_MATERIAL, MaterialKind.API_SPEC)],
    )

    assert "leave_type: annual, welfare, sick" in _details(issues, "undocumented_enum")


def test_a_contract_that_lists_the_values_is_clean() -> None:
    contract = "必填 `leave_type`（`annual`、`welfare`、`sick`）。"

    issues = scan_material_fidelity(
        _draft(contract), [_material(DDL_MATERIAL, MaterialKind.API_SPEC)]
    )

    assert "undocumented_enum" not in _rules(issues)


def test_an_enum_on_a_column_the_skill_never_reads_is_not_reported() -> None:
    other = DDL_MATERIAL.replace("leave_type  NVARCHAR", "approver_state  NVARCHAR").replace(
        "CHECK (leave_type IN", "CHECK (approver_state IN"
    )

    issues = scan_material_fidelity(
        _draft("必填 `leave_type`（字串）。"), [_material(other, MaterialKind.API_SPEC)]
    )

    assert "undocumented_enum" not in _rules(issues)


def test_a_retired_file_kind_is_still_scanned_as_tier_2() -> None:
    issues = scan_material_fidelity(
        _draft("必填 `leave_type`（字串）。"), [_material(DDL_MATERIAL, MaterialKind.FILE)]
    )

    assert "undocumented_enum" in _rules(issues)
