from __future__ import annotations

import pytest

from backend import eaa_platform
from backend.eaa_platform import (
    STATIC_OBO_REGISTRY_KEYS,
    EaaLintRejected,
    EaaLintUnavailable,
    declared_mi_scopes,
    lint_skill_package,
    mi_allowlist_note,
    normalize_mi_resource,
    obo_registry_keys,
    reserved_credentials_key_reason,
    split_eaa_allowlist_warnings,
)

SKILL = {"SKILL.md": "---\nname: demo\ndescription: Demo\n---\n\n## Overview\nDemo.\n"}


def _mcp(monkeypatch: pytest.MonkeyPatch, payload=None, error=None) -> list:
    calls: list = []
    monkeypatch.setenv("MCP_ENDPOINT", "https://eaa.example/mcp/")

    def fake(url, skill_name, files, *, timeout):
        calls.append((url, skill_name, files))
        return payload, error

    monkeypatch.setattr(eaa_platform, "lint_skill_package_jsonrpc", fake)
    return calls


def test_lint_requires_mcp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_ENDPOINT", raising=False)
    with pytest.raises(EaaLintUnavailable, match="MCP_ENDPOINT"):
        lint_skill_package("demo", SKILL)


def test_clean_report_passes_and_carries_the_ruleset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mcp(monkeypatch, {"status": "completed", "valid": True, "errors": [], "warnings": ["w"], "ruleset_version": "1.0"})
    result = lint_skill_package("demo", SKILL)
    assert (result.valid, result.errors, result.warnings, result.ruleset_version) == (True, [], ["w"], "1.0")
    assert calls == [("https://eaa.example/mcp", "demo", SKILL)]


def test_errors_make_the_report_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _mcp(monkeypatch, {"status": "completed", "valid": False, "errors": ["reads OBO_CLIENT_SECRET"], "warnings": []})
    result = lint_skill_package("demo", SKILL)
    assert not result.valid and result.errors == ["reads OBO_CLIENT_SECRET"]


def test_listed_errors_are_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _mcp(monkeypatch, {"status": "completed", "valid": True, "errors": ["e"], "warnings": []})
    assert not lint_skill_package("demo", SKILL).valid


def test_failed_status_is_a_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    _mcp(monkeypatch, {"status": "failed", "error": "files_json must include SKILL.md"})
    with pytest.raises(EaaLintRejected, match="must include SKILL.md"):
        lint_skill_package("demo", SKILL)


@pytest.mark.parametrize(("payload", "error"), [
    (None, "HTTP 401 Unauthorized"),
    ({"status": "running"}, None),
    ({"status": "completed"}, None),
    ({"status": "completed", "valid": "yes"}, None),
    ({"status": "completed", "valid": True, "errors": "e"}, None),
])
def test_no_trustworthy_verdict_fails_closed(monkeypatch: pytest.MonkeyPatch, payload, error) -> None:
    _mcp(monkeypatch, payload, error)
    with pytest.raises(EaaLintUnavailable):
        lint_skill_package("demo", SKILL)


@pytest.mark.parametrize(("raw", "expected"), [
    ("https://Storage.azure.com/.default", "https://storage.azure.com"),
    ("https://ai.azure.com/", "https://ai.azure.com"),
    ("https://storage.azure.com/container", ""),
    ("http://storage.azure.com", ""),
    ("https://storage.azure.com:443", ""),
    ("storage.azure.com", ""),
    (None, ""),
])
def test_normalize_mi_resource_matches_the_mi_proxy(raw, expected) -> None:
    assert normalize_mi_resource(raw) == expected


@pytest.mark.parametrize(("frontmatter", "expected"), [
    ("metadata:\n  mi_scopes:\n    - https://storage.azure.com\n", ["https://storage.azure.com"]),
    ("metadata:\n  mi_scopes: https://storage.azure.com, https://ai.azure.com\n", ["https://storage.azure.com", "https://ai.azure.com"]),
    ("metadata:\n  author: x\n", []),
    ("metadata: [broken\n", []),
])
def test_declared_mi_scopes(frontmatter: str, expected: list[str]) -> None:
    assert declared_mi_scopes(f"---\nname: demo\n{frontmatter}---\n\nbody\n") == expected


def test_allowlist_warnings_are_split_from_other_warnings() -> None:
    warnings = [
        "SKILL.md: installs packages at runtime",
        "metadata.mi_scopes: https://search.azure.com is not in MI_SCOPE_ALLOWLIST ['https://ai.azure.com']",
        "uses Managed Identity but declares no metadata.mi_scopes — MI is denied when MI_GATE_ENABLED=true",
    ]
    others, resources = split_eaa_allowlist_warnings(warnings)
    assert others == [warnings[0], warnings[2]]
    assert resources == ["https://search.azure.com"]
    assert "Search Index Data Reader" in mi_allowlist_note(resources[0])


@pytest.mark.parametrize(("aca_env_result", "expected"), [
    ({"architectural_config": {"OBO_SCOPE_REGISTRY": {"A_TOKEN": "s1", "B_TOKEN": "s2"}}}, {"A_TOKEN", "B_TOKEN"}),
    ({"architectural_config": {"OBO_SCOPE_REGISTRY": '{"A_TOKEN": "s1"}'}}, {"A_TOKEN"}),
    ({"architectural_config": {"OBO_SCOPE_REGISTRY": "not json"}}, set(STATIC_OBO_REGISTRY_KEYS)),
    ({"variables": []}, set(STATIC_OBO_REGISTRY_KEYS)),
    (None, set(STATIC_OBO_REGISTRY_KEYS)),
])
def test_registry_keys_come_from_mcp_with_a_static_fallback(aca_env_result, expected) -> None:
    assert obo_registry_keys(aca_env_result) == expected


def test_static_fallback_names_the_known_registry_keys() -> None:
    assert STATIC_OBO_REGISTRY_KEYS == {"AZURE_SQL_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"}


@pytest.mark.parametrize(("name", "reserved"), [
    ("PATH", True),
    ("PYTHONPATH", True),
    ("LD_LIBRARY_PATH", True),
    ("EAA_VERIFIED_USER_UPN", True),
    ("EAA_VERIFIED_ANYTHING", True),
    ("AZURE_SQL_ACCESS_TOKEN", True),
    ("QUERY_JSON", False),
    ("MY_PATH", False),
    ("python_version", False),
])
def test_reserved_credentials_keys_match_eaa(name: str, reserved: bool) -> None:
    assert (reserved_credentials_key_reason(name, STATIC_OBO_REGISTRY_KEYS) is not None) is reserved
