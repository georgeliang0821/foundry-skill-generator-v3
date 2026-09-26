from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.eaa_platform import (
    STATIC_OBO_REGISTRY_KEYS,
    EaaLintUnavailable,
    obo_registry_keys,
    reserved_credentials_key_reason,
    run_eaa_skill_lint,
)

SKILL = {"SKILL.md": "---\nname: demo\ndescription: Demo\n---\n\n## Overview\nDemo.\n"}


def _fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    repo = tmp_path / "eaa"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "skill_lint.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("EAA_REPO_DIR", str(repo))
    return repo


def _printing(stdout: object, code: int = 0) -> str:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return f"import sys\nsys.stdout.write({text!r})\nsys.exit({code})\n"


def test_lint_requires_eaa_repo_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAA_REPO_DIR", raising=False)
    with pytest.raises(EaaLintUnavailable, match="EAA_REPO_DIR"):
        run_eaa_skill_lint("demo", SKILL)


def test_lint_requires_the_tool_in_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAA_REPO_DIR", str(tmp_path))
    with pytest.raises(EaaLintUnavailable, match="bd13560"):
        run_eaa_skill_lint("demo", SKILL)


def test_clean_report_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_repo(tmp_path, monkeypatch, _printing([{"skill": "demo", "errors": [], "warnings": []}]))
    assert run_eaa_skill_lint("demo", SKILL) == []


def test_errors_and_warnings_are_both_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = [{"skill": "demo", "errors": ["reads OBO_CLIENT_SECRET"], "warnings": ["credential=... placeholder"]}]
    _fake_repo(tmp_path, monkeypatch, _printing(report, code=1))
    assert run_eaa_skill_lint("demo", SKILL) == [
        "ERROR reads OBO_CLIENT_SECRET",
        "WARN credential=... placeholder",
    ]


def test_warnings_alone_still_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_repo(tmp_path, monkeypatch, _printing([{"skill": "demo", "errors": [], "warnings": ["w"]}]))
    assert run_eaa_skill_lint("demo", SKILL) == ["WARN w"]


@pytest.mark.parametrize("body", [
    _printing([{"skill": "demo", "errors": [], "warnings": []}], code=2),
    _printing("not json"),
    _printing([]),
    _printing([{"skill": "demo", "errors": [], "warnings": []}], code=1),
    "raise SystemExit(3)\n",
])
def test_no_trustworthy_verdict_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    _fake_repo(tmp_path, monkeypatch, body)
    with pytest.raises(EaaLintUnavailable):
        run_eaa_skill_lint("demo", SKILL)


def test_tool_gets_the_skill_folder_and_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "do-not-leak")
    _fake_repo(tmp_path, monkeypatch, (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "folder = Path(sys.argv[1])\n"
        "warnings = [] if sys.argv[2:] == ['--json'] else ['missing --json']\n"
        "if os.environ.get('AZURE_CLIENT_SECRET'): warnings.append('secret leaked')\n"
        "if folder.name != 'demo': warnings.append('wrong folder ' + folder.name)\n"
        "if 'name: demo' not in (folder / 'SKILL.md').read_text(encoding='utf-8'): warnings.append('no SKILL.md')\n"
        "print(json.dumps([{'skill': folder.name, 'errors': [], 'warnings': warnings}]))\n"
    ))
    assert run_eaa_skill_lint("demo", SKILL) == []


def test_files_outside_the_skill_folder_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_repo(tmp_path, monkeypatch, _printing([{"skill": "demo", "errors": [], "warnings": []}]))
    with pytest.raises(EaaLintUnavailable, match="outside"):
        run_eaa_skill_lint("demo", {"../escape.md": "x"})


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
