from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.material_fidelity import scan_material_fidelity
from backend.models import Material, MaterialKind, Mode, SkillKind
from backend.models import TestResult as RunResult
from backend.skill_lint import DEPLOYMENT_SECTION, has_lint_errors, lint_skill, script_only_errors
from backend.testing import _lint_prepared_code
from backend.topology import validate_topology

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "skills" / "ms-graph-room-finder"
MD = (FIXTURE / "SKILL.md").read_text(encoding="utf-8")
PY = (FIXTURE / "scripts" / "ms-graph-room-finder.py").read_text(encoding="utf-8")
BASELINE = json.loads((Path(__file__).parent / "inline_lint_baseline.json").read_text(encoding="utf-8"))

PARSE_GUARD = (
    "    try:\n"
    "        ns = p.parse_args(argv)\n"
    "    except SystemExit:\n"
    "        # argparse exits 2, which EAA reports as a failure; ask for the argument instead.\n"
    '        raise NeedsInfo("arguments", "unrecognized or malformed arguments") from None\n'
)
GRAPH_ERROR_OUT = '"hint": hint}, ensure_ascii=False))\n        return EXIT_DOWNSTREAM\n'
FINAL_OUT = "    print(json.dumps(summarize(result, limit), ensure_ascii=False))\n    return EXIT_OK\n"


def _swap(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def _drop_line(text: str, pattern: str) -> str:
    out, count = re.subn(rf"^.*{pattern}.*\n", "", text, count=1, flags=re.MULTILINE)
    assert count == 1, pattern
    return out


def _lint(md: str = MD, py: str = PY):
    return lint_skill(md, SkillKind.CAPABILITY, script=py)


def _hits(issues, rule: str) -> list[tuple[str, str]]:
    return [(i.severity, i.detail) for i in issues if i.rule == rule]


# --- golden fixture ---------------------------------------------------------


def test_golden_fixture_is_clean() -> None:
    issues = _lint()

    assert not has_lint_errors(issues)
    # Warning snapshot: the golden fixture raises nothing at all.
    assert [(i.rule, i.severity, i.detail) for i in issues] == []


def test_golden_fixture_passes_topology() -> None:
    assert validate_topology(MD, SkillKind.CAPABILITY, mode=Mode.NEW, expected_name=FIXTURE.name, script=PY) == []


def test_script_form_drops_the_step0_false_positives() -> None:
    # Before Phase 4 the same pair produced A2 x2, A3, A9, A11 x2 and an A15 error.
    before = {i["rule"] for i in BASELINE["ms-graph-room-finder+code_override"]["lint"]}
    assert before == {"A2", "A3", "A9", "A11", "A15"}
    assert {i.rule for i in _lint()} & before == set()


@pytest.mark.parametrize("name", sorted(BASELINE))
def test_inline_lint_is_unchanged(name: str) -> None:
    expected = BASELINE[name]
    kind = SkillKind(expected["kind"])
    if name.endswith("+code_override"):
        issues = lint_skill(MD, kind, code_override=PY)
    else:
        md = (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        issues = lint_skill(md, kind)
        topology = validate_topology(md, kind, mode=Mode.NEW, expected_name=name)
        assert [
            {"rule": i.rule, "severity": i.severity.value, "message": i.message} for i in topology
        ] == expected["topology"]
    assert [i.to_dict() for i in issues] == expected["lint"]


# --- existing rules, script form --------------------------------------------


def test_a2_reads_declarations_from_prerequisites() -> None:
    md = _drop_line(MD, r"^- `DEFAULT_BUILDING`")
    assert _hits(_lint(md=md), "A2") == [("warning", "DEFAULT_BUILDING")]


def test_a3_ignores_raises_the_script_catches_itself() -> None:
    py = _swap(PY, 'raise GraphError(0, "TooManyPages"', 'raise RuntimeError(0, "TooManyPages"')
    assert _hits(_lint(py=py), "A3") == [("warning", "RuntimeError")]


def test_a4_and_c2_require_only_the_needs_info_row() -> None:
    md = _drop_line(MD, r"`status` = `needs_info` \|")

    assert _hits(_lint(md=md), "A4") == [("error", "Reading the Result")]
    issues = validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW, script=PY)
    assert [i.rule for i in issues] == ["C2"]
    assert validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW) == []


def test_an_exit_0_row_that_names_needs_info_also_counts() -> None:
    md = _drop_line(MD, r"`status` = `needs_info` \|")
    md = _swap(
        md,
        "| 0, `status` = `ok` | Success, possibly with zero candidates |",
        "| `0` | Success, possibly with zero candidates, or a caller-input request using `[NEEDS_INFO]` |",
    )

    assert _hits(_lint(md=md), "A4") == []
    assert [i.rule for i in validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW, script=PY)] == []

    other_code = md.replace("| `0` | Success", "| `3` | Success")
    assert _hits(_lint(md=other_code), "A4") == [("error", "Reading the Result")]


def test_c2_allows_a_needs_info_subheading_in_script_form() -> None:
    md = _swap(MD, "## Composability", "### `needs_info` output\n\nSee the table above.\n\n## Composability")

    assert validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW, script=PY) == []


def test_c2_still_rejects_required_inputs_below_h2_in_script_form() -> None:
    md = _swap(MD, "## Required Inputs", "### Required Inputs")

    issues = validate_topology(md, SkillKind.CAPABILITY, mode=Mode.NEW, script=PY)
    assert "C2" in [i.rule for i in issues]


def test_d1_allows_a_literal_default_only_for_an_optional_variable() -> None:
    md = _swap(MD, "- `DEFAULT_BUILDING` — optional ACA variable;", "- `DEFAULT_BUILDING` — ACA variable;")
    issues = _lint(md=md)

    assert _hits(issues, "D1") == [("warning", "DEFAULT_BUILDING")]
    assert not [i for i in issues if i.detail == DEPLOYMENT_SECTION]


def test_d1_never_relaxes_an_access_token() -> None:
    md = _swap(MD, "- `GRAPH_ACCESS_TOKEN` — Microsoft", "- `GRAPH_ACCESS_TOKEN` — optional Microsoft")
    py = _swap(PY, 'os.environ["GRAPH_ACCESS_TOKEN"]', 'os.environ.get("GRAPH_ACCESS_TOKEN", "")')

    assert ("warning", "GRAPH_ACCESS_TOKEN") in _hits(_lint(md=md, py=py), "D1")


def test_excluded_inline_rules_stay_silent() -> None:
    rules = {i.rule for i in _lint()}
    assert not rules & {"A9", "A10", "A11", "A14", "A15"}


# --- S rules ----------------------------------------------------------------


def test_s2_python_fence() -> None:
    fence = "```python\nprint(1)\n```\n\n"
    assert _hits(_lint(md=_swap(MD, "## Required Inputs\n", fence + "## Required Inputs\n")), "S2") == [
        ("error", "python fence")
    ]
    assert _hits(_lint(md=MD + "\n## Gatekeeper Addendum\n\n" + fence), "S2") == []


def test_s3_flag_named_in_prose_must_exist() -> None:
    md = _swap(MD, '"--capacity", "8"]', '"--floor", "8"]')
    assert _hits(_lint(md=md), "S3") == [("error", "--floor")]


def test_s3_table_and_add_argument_agree() -> None:
    dropped = _swap(PY, '    p.add_argument("--limit", default=str(SUMMARY_ROOMS))\n', "")
    assert _hits(_lint(py=dropped), "S3") == [("error", "--limit")]

    extra = _swap(PY, '    p.add_argument("--start")\n', '    p.add_argument("--start")\n    p.add_argument("--debug")\n')
    assert _hits(_lint(py=extra), "S3") == [("warning", "--debug")]


def test_s4_stdout_carries_json_only() -> None:
    py = _swap(
        PY,
        '    token = os.environ["GRAPH_ACCESS_TOKEN"]\n',
        '    token = os.environ["GRAPH_ACCESS_TOKEN"]\n    print("scanning rooms")\n',
    )
    issues = [i for i in _lint(py=py) if i.rule == "S4"]
    assert [i.severity for i in issues] == ["error"]
    assert "machine-readable result" in issues[0].message
    assert "successful run count as a failure" in issues[0].message
    assert _hits(_lint(), "S4") == []


CLEANUP = Path(__file__).parent / "fixtures"
CLEANUP_MATERIAL = (CLEANUP / "storage_cleanup_material.py").read_text(encoding="utf-8")
CLEANUP_SCRIPT = (CLEANUP / "storage_cleanup_script.py").read_text(encoding="utf-8")


def test_s4_quotes_each_line_and_names_text_after_needs_info() -> None:
    # Mirrored by EAA's lint_skill_package stdout rule: same material, same lines.
    issues = [i for i in script_only_errors(CLEANUP_MATERIAL) if i.rule == "S4"]

    assert [i.detail for i in issues] == [f"line {n}" for n in (34, 48, 79, 91, 109, 111, 113)]
    assert issues[0].message.startswith(
        "Line 34 (`print(\"I need the target table/container name(s) and retention rule before I can delete Azure Sto...`) "
        "prints plain text after the `[NEEDS_INFO]` line."
    )
    assert "`reason`" in issues[0].message
    assert issues[1].message.startswith('Line 48 (`print(f"cutoff={cutoff.isoformat()} dry_run={DRY_RUN}")`) writes to stdout')
    assert issues[-1].message.startswith('Line 113 (`print( f"Done. tables=')


def test_s4_treats_a_bare_stdout_import_as_stdout() -> None:
    py = 'from sys import stderr, stdout\nprint("a", file=stdout)\nstdout.write("b")\nprint("c", file=stderr)\nstderr.write("d")\n'
    assert [i.detail for i in script_only_errors(py) if i.rule == "S4"] == ["line 2", "line 3"]


def test_s13_globals_never_hold_a_bundled_scripts_inputs() -> None:
    issues = [i for i in script_only_errors(CLEANUP_MATERIAL) if i.rule == "S13"]

    assert [(i.severity, i.detail) for i in issues] == [("error", "line 17")]
    assert "(`return globals().get(name) or os.environ.get(name)`)" in issues[0].message


def test_the_adapted_cleanup_script_is_clean() -> None:
    assert script_only_errors(CLEANUP_SCRIPT) == []


def test_s3_every_required_inputs_row_names_a_flag() -> None:
    md = _swap(MD, "| `--capacity` |", "| `capacity` |")
    assert ("error", "`capacity`") in _hits(_lint(md=md), "S3")
    assert [d for s, d in _hits(_lint(), "S3") if s == "error"] == []


def test_s5_traces_tuple_constants_through_main() -> None:
    py = _swap(PY, "EXIT_OK, EXIT_DOWNSTREAM = 0, 3", "EXIT_OK, EXIT_DOWNSTREAM = 0, 4")
    assert _hits(_lint(py=py), "S5") == [("error", "4")]


def test_s5_exit_codes_must_be_documented() -> None:
    md = _drop_line(MD, r"^\| 3 \|")
    assert _hits(_lint(md=md), "S5") == [("warning", "3")]


def test_s5_and_s10_reject_argparse_exit_2() -> None:
    py = _swap(PY, 'raise NeedsInfo("arguments", "unrecognized or malformed arguments") from None', "sys.exit(2)")
    issues = _lint(py=py)

    assert _hits(issues, "S5") == [("error", "2")]
    assert [severity for severity, _ in _hits(issues, "S10")] == ["error"]


def test_s6_ignores_prints_in_handlers_that_return() -> None:
    assert _hits(_lint(), "S6") == []

    py = _swap(PY, GRAPH_ERROR_OUT, '"hint": hint}, ensure_ascii=False))\n        result = {"status": "ok"}\n')
    issues = [i for i in _lint(py=py) if i.rule == "S6"]
    assert len(issues) == 1 and "not the first stdout line" in issues[0].message


def test_s6_at_most_one_json_after_the_marker() -> None:
    py = _swap(PY, FINAL_OUT, FINAL_OUT.replace("    return EXIT_OK\n", '    print(json.dumps({"status": "ok"}))\n    return EXIT_OK\n'))
    issues = [i for i in _lint(py=py) if i.rule == "S6"]
    assert len(issues) == 1 and "2 more" in issues[0].message


def test_s6_needs_info_path_exits_zero() -> None:
    py = _swap(PY, FINAL_OUT, FINAL_OUT.replace("return EXIT_OK", "return EXIT_DOWNSTREAM"))
    issues = [i for i in _lint(py=py) if i.rule == "S6"]
    assert len(issues) == 1 and "exits 3" in issues[0].message


def test_s7_token_is_indexed_not_defaulted() -> None:
    py = _swap(PY, 'os.environ["GRAPH_ACCESS_TOKEN"]', 'os.environ.get("GRAPH_ACCESS_TOKEN")')
    assert _hits(_lint(py=py), "S7") == [("error", "GRAPH_ACCESS_TOKEN")]


def test_s7_no_token_argument() -> None:
    py = _swap(PY, '    p.add_argument("--start")\n', '    p.add_argument("--start")\n    p.add_argument("--token")\n')
    assert _hits(_lint(py=py), "S7") == [("error", "--token")]


def test_s8_eaa_runs_mention_outside_the_addendum() -> None:
    md = _swap(MD, "The script writes no files.", "The script writes no files under eaa_runs.")
    assert _hits(_lint(md=md), "S8") == [("warning", "eaa_runs")]
    assert _hits(_lint(md=MD + "\n## Gatekeeper Addendum\n\nRuns land in eaa_runs/.\n"), "S8") == []


def test_s9_output_keys_are_documented() -> None:
    py = _swap(PY, '"hint": hint}, ensure_ascii=False))', '"hint": hint, "debug_info": 1}, ensure_ascii=False))')
    assert _hits(_lint(py=py), "S9") == [("warning", "debug_info")]


def test_s10_accepts_the_needs_info_exception_and_a_direct_print() -> None:
    assert _hits(_lint(), "S10") == []

    direct = _swap(
        PY,
        '        raise NeedsInfo("arguments", "unrecognized or malformed arguments") from None\n',
        '        print("[NEEDS_INFO] missing=arguments")\n'
        '        print(json.dumps({"status": "needs_info", "needs_info": "arguments"}))\n'
        "        sys.exit(0)\n",
    )
    issues = _lint(py=direct)
    assert _hits(issues, "S10") == [] and _hits(issues, "S6") == []


def test_s10_parse_args_outside_try() -> None:
    py = _swap(PY, PARSE_GUARD, "    ns = p.parse_args(argv)\n")
    issues = [i for i in _lint(py=py) if i.rule == "S10"]
    assert len(issues) == 1 and "not inside" in issues[0].message


def test_s10b_help_disabled() -> None:
    py = _swap(PY, ', add_help=False)', ")")
    assert [severity for severity, _ in _hits(_lint(py=py), "S10b")] == ["error"]


def test_s11_error_key_on_the_exit_zero_path() -> None:
    assert _hits(_lint(), "S11") == []

    py = _swap(PY, '        "status": "ok",\n        "scope": scope,', '        "status": "ok",\n        "error": None,\n        "scope": scope,')
    details = [detail for _, detail in _hits(_lint(py=py), "S11")]
    assert any(detail.endswith('"error":') for detail in details)


def test_s11_status_pattern_stays_inside_the_status_value() -> None:
    # `"status":\s*"[^"]*error"` cannot run past the status value into a later key.
    py = _swap(PY, '        "scope": scope,\n', '        "scope": scope,\n        "last_error": None,\n')
    assert _hits(_lint(py=py), "S11") == []

    py = _swap(PY, '        "status": "ok",', '        "status": "partial_error",')
    details = [detail for _, detail in _hits(_lint(py=py), "S11")]
    assert details and all('"status":' in detail for detail in details)


def test_s12_inline_request_template_cannot_ship_as_a_script() -> None:
    assert _hits(_lint(), "S12") == []

    template = (
        "import json\n\n\n"
        "def main():\n"
        '    request_inputs = {"TARGET_TABLES": ["jobs"], "DRY_RUN": False}\n'
        '    print(json.dumps({"status": "ok", "tables": request_inputs.get("TARGET_TABLES")}))\n'
    )
    assert [(i.rule, i.detail) for i in script_only_errors(template)] == [("S12", "line 5")]
    annotated = template.replace("request_inputs = {", "request_inputs: dict = {")
    assert [i.rule for i in script_only_errors(annotated)] == ["S12"]


# --- test runner and material fidelity --------------------------------------


def test_prepared_code_lint_is_skipped_for_script_form() -> None:
    results = [RunResult(query="find a room", expected_skill="x", apim_response=PY)]

    _lint_prepared_code(MD, results, script_form=True)
    assert results[0].prepared_code_lint == []

    _lint_prepared_code(MD, results)
    assert results[0].prepared_code_lint


def test_script_fidelity_compares_the_script_with_the_code_material() -> None:
    code = Material(kind=MaterialKind.CODE, content=PY.replace("\n", "\r\n"))

    assert scan_material_fidelity(MD, [code], script=PY) == []
    assert scan_material_fidelity(MD, [Material(kind=MaterialKind.TEXT, content="x")], script=PY) == []
    (issue,) = scan_material_fidelity(MD, [code], script=PY + "\nprint(1)\n")
    assert issue.rule == "script_not_verbatim" and issue.detail == code.id
