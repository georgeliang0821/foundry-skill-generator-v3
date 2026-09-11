from __future__ import annotations

import ast
import json
from pathlib import Path

from scripts.sync_reference import (
    changed_symbols,
    check_response_coverage,
    check_wire_coverage,
    diff_surface,
    explain_symbol,
    extract_surface,
    impact_index,
    impact_tokens,
    inspect_reference,
    sha256_bytes,
    validate_contracts,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "reference" / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_snapshot_hashes_match_manifest() -> None:
    manifest = _manifest()
    mismatches = []
    for spec in manifest["files"]:
        content = (ROOT / spec["destination"]).read_bytes()
        if sha256_bytes(content) != spec["sha256"]:
            mismatches.append(spec["destination"])
    assert mismatches == [], f"Stale reference snapshots: {mismatches}; run scripts/sync_reference.py sync"


def test_known_contracts_match_snapshot() -> None:
    manifest = _manifest()
    contents = {
        spec["source"]: (ROOT / spec["destination"]).read_bytes()
        for spec in manifest["files"]
    }
    assert validate_contracts(contents, manifest["contracts"]) == []


def test_contract_validator_names_review_targets() -> None:
    contents = {"sample.py": b"def route(request, extra):\n    return request\n"}
    contracts = [{
        "source": "sample.py",
        "symbol": "route",
        "parameters": ["request"],
        "review": ["backend/testing.py"],
    }]
    assert validate_contracts(contents, contracts) == [{
        "source": "sample.py",
        "symbol": "route",
        "problem": "parameters changed",
        "expected": ["request"],
        "actual": ["request", "extra"],
        "review": ["backend/testing.py"],
    }]


def test_wire_coverage_flags_a_parameter_the_generator_never_sends(tmp_path) -> None:
    consumer = tmp_path / "backend" / "testing.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text('payload = {"request": q, "mode": m}\n', encoding="utf-8")
    contracts = [{
        "source": "core_handler.py",
        "symbol": "run_workflow",
        "wire": {
            "file": "backend/testing.py",
            "match": "dict_key",
            "params": {"user_input": "request", "mode": "mode", "scenario": "scenario"},
        },
    }]
    findings = check_wire_coverage(tmp_path, contracts)
    assert [f["parameter"] for f in findings] == ["scenario"]


def test_wire_coverage_accepts_a_parameter_named_inside_a_pattern() -> None:
    contracts = [{
        "source": "core_handler.py",
        "symbol": "fetch_skill",
        "wire": {
            "file": "backend/topology.py",
            "match": "text",
            "params": {"skill_name": "skill_name", "sections": "sections"},
        },
    }]
    assert check_wire_coverage(ROOT, contracts) == []


def test_surface_ignores_function_bodies_but_keeps_the_contract() -> None:
    before = b'WHITELIST = frozenset({"a", "b"})\ndef route(request):\n    return 1\n'
    after = b'WHITELIST = frozenset({"a", "b", "c"})\ndef route(request):\n    return 2\n'
    deltas = diff_surface(extract_surface(before, "m.py"), extract_surface(after, "m.py"))
    assert deltas == [{
        "kind": "collections",
        "token": "WHITELIST",
        "change": "changed",
        "before": ["a", "b"],
        "after": ["a", "b", "c"],
    }]
    assert impact_tokens(deltas) == {"c"}


def test_impact_index_locates_the_changed_token_in_generator_code(tmp_path) -> None:
    consumer = tmp_path / "backend" / "testing.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text('def read(data):\n    return data.get("needs_input")\n', encoding="utf-8")
    assert impact_index(tmp_path, {"needs_input"}) == {"needs_input": ["backend/testing.py:2"]}


def test_impact_index_stays_silent_for_private_runtime_helpers(tmp_path) -> None:
    consumer = tmp_path / "backend" / "testing.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text("x = 1\n", encoding="utf-8")
    assert impact_index(tmp_path, {"_project_sections"}) == {}


def test_explain_symbol_returns_the_declaration_source() -> None:
    content = b"def route(request):\n    return request\n"
    assert explain_symbol(content, "route") == "def route(request):\n    return request"
    assert explain_symbol(content, "missing") is None


_WHITELIST = b'WHITELIST = frozenset({"response", "status", "job_id"})\n'


def _response_manifest(required: list[str], ignored: list[str]) -> dict:
    return {
        "response_wire": {
            "source": "core_handler.py",
            "symbol": "WHITELIST",
            "file": "backend/testing.py",
            "required": required,
            "ignored": {name: "not needed" for name in ignored},
        }
    }


def _reader(tmp_path, body: str):
    target = tmp_path / "backend" / "testing.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return {"core_handler.py": _WHITELIST}


def test_classified_and_read_response_fields_are_silent(tmp_path) -> None:
    upstream = _reader(tmp_path, 'def read(d):\n    return d.get("response"), d.get("status")\n')
    manifest = _response_manifest(["response", "status"], ["job_id"])
    assert check_response_coverage(tmp_path, upstream, manifest) == []


def test_a_new_runtime_response_field_must_be_classified(tmp_path) -> None:
    upstream = _reader(tmp_path, 'def read(d):\n    return d.get("response")\n')
    manifest = _response_manifest(["response"], [])
    problems = [f["problem"] for f in check_response_coverage(tmp_path, upstream, manifest)]
    assert any("`status`" in p and "neither required nor ignored" in p for p in problems)
    assert any("`job_id`" in p and "neither required nor ignored" in p for p in problems)


def test_a_required_field_that_is_never_read_is_reported(tmp_path) -> None:
    upstream = _reader(tmp_path, 'def read(d):\n    return d.get("response")\n')
    manifest = _response_manifest(["response", "status"], ["job_id"])
    problems = [f["problem"] for f in check_response_coverage(tmp_path, upstream, manifest)]
    assert problems == ["`status` is classified required, but it is never read there."]


def test_a_classification_the_runtime_dropped_is_reported(tmp_path) -> None:
    upstream = _reader(tmp_path, 'def read(d):\n    return d.get("response")\n')
    manifest = _response_manifest(["response"], ["status", "job_id", "uploads"])
    problems = [f["problem"] for f in check_response_coverage(tmp_path, upstream, manifest)]
    assert problems == [
        "core_handler.py::WHITELIST no longer carries `uploads`, but the manifest still "
        "classifies it. Drop the stale entry."
    ]


def test_building_a_dict_key_does_not_count_as_reading_the_field(tmp_path) -> None:
    # `{"status": exc.reason}` builds an error envelope; it reads nothing.
    upstream = _reader(tmp_path, 'def fail(exc):\n    return {"status": exc.reason}\n')
    manifest = _response_manifest(["status"], ["response", "job_id"])
    problems = [f["problem"] for f in check_response_coverage(tmp_path, upstream, manifest)]
    assert problems == ["`status` is classified required, but it is never read there."]


def test_offline_check_needs_no_upstream_checkout(tmp_path) -> None:
    manifest = {
        "files": [{"source": "core_handler.py", "destination": "reference/core_handler.py", "sha256": ""}],
        "contracts": [],
    }
    snapshot = tmp_path / "reference" / "core_handler.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("def run_workflow(user_input):\n    return user_input\n", encoding="utf-8")
    report, upstream, local = inspect_reference(manifest, None, None, tmp_path)
    assert report["offline"] is True
    assert [item["status"] for item in report["files"]] == ["pinned"]
    assert upstream["core_handler.py"] == local["core_handler.py"]


def test_body_only_change_does_not_report_symbol_drift() -> None:
    before = b"def route(request):\n    return request\n"
    after = b"def route(request):\n    return request.strip()\n"
    assert changed_symbols(before, after, "sample.py") == {
        "added": [],
        "removed": [],
        "signature_changed": [],
    }


def test_backend_does_not_import_reference_snapshot() -> None:
    violations = []
    for path in (ROOT / "backend").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "reference" or name.startswith("reference.") for name in names):
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []