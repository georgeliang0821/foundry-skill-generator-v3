"""Rules the EAA runtime enforces on a skill's execution environment, and its lint.

The names here mirror the EAA repo's ``code_executor.py`` (S1, commit bd13560).
They are restated rather than imported: the EAA checkout is an external tool,
and ``reference/`` must never be imported by ``backend/``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from .diagnostics import log_event

# Stripped from the skill subprocess environment by EAA (SUBPROCESS_ENV_DENYLIST).
PLATFORM_SECRET_DENYLIST = frozenset({
    "OBO_CLIENT_SECRET",
    "TEAMS_NOTIFY_WEBHOOK_URL",
    "LOGIC_APP_SKILL_REVIEW_URL",
    "AZURE_STORAGE_ACCOUNT_KEY",
})

# Used when the live OBO_SCOPE_REGISTRY could not be read through MCP.
STATIC_OBO_REGISTRY_KEYS = frozenset({"AZURE_SQL_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"})

_RESERVED_CREDENTIAL_NAMES = frozenset({"PATH"})
# Case-sensitive, exactly as EAA's filter_caller_env matches them.
_RESERVED_CREDENTIAL_PREFIXES = ("PYTHON", "LD_", "EAA_VERIFIED_")


def obo_registry_mapping(aca_env_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """The OBO_SCOPE_REGISTRY from the MCP ACA lookup; the value may arrive as a JSON string."""
    if not isinstance(aca_env_result, Mapping):
        return {}
    config = aca_env_result.get("architectural_config")
    registry = config.get("OBO_SCOPE_REGISTRY") if isinstance(config, Mapping) else None
    if isinstance(registry, str):
        try:
            registry = json.loads(registry)
        except json.JSONDecodeError:
            return {}
    return dict(registry) if isinstance(registry, Mapping) else {}


def obo_registry_keys(aca_env_result: Mapping[str, Any] | None) -> frozenset[str]:
    keys = frozenset(str(key) for key in obo_registry_mapping(aca_env_result))
    return keys or STATIC_OBO_REGISTRY_KEYS


def reserved_credentials_key_reason(name: str, registry_keys: Collection[str]) -> str | None:
    """Why EAA would drop a caller-supplied ``credentials`` key, or None if it survives."""
    if name in _RESERVED_CREDENTIAL_NAMES:
        return f"`{name}` is reserved for the interpreter search path"
    for prefix in _RESERVED_CREDENTIAL_PREFIXES:
        if name.startswith(prefix):
            return f"names starting with `{prefix}` are reserved by the platform"
    if name in registry_keys:
        return f"`{name}` is an OBO_SCOPE_REGISTRY key, injected by the platform's OBO exchange"
    return None


# ---------------------------------------------------------------------------
# EAA tools/skill_lint.py
# ---------------------------------------------------------------------------


class EaaLintUnavailable(RuntimeError):
    """The EAA lint could not produce a verdict. Saving must fail closed."""


def _lint_env() -> dict[str, str]:
    # The lint needs no credentials; do not hand it this process's secrets.
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_eaa_skill_lint(skill_name: str, files: Mapping[str, str], *, timeout: float = 60) -> list[str]:
    """Run ``python tools/skill_lint.py <dir> --json`` inside the EAA checkout.

    Returns every error and warning; an empty list is the only passing result.
    Raises ``EaaLintUnavailable`` whenever no trustworthy verdict exists.
    """
    repo_value = os.getenv("EAA_REPO_DIR", "").strip()
    if not repo_value:
        raise EaaLintUnavailable(
            "EAA_REPO_DIR is not set. Saving requires the EAA skill lint; point EAA_REPO_DIR "
            "at an EAA repo checkout at commit bd13560 or newer."
        )
    repo = Path(repo_value)
    script = repo / "tools" / "skill_lint.py"
    if not script.is_file():
        raise EaaLintUnavailable(
            f"{script} was not found. EAA_REPO_DIR must be an EAA repo checkout at commit "
            "bd13560 or newer, where tools/skill_lint.py was introduced."
        )

    with tempfile.TemporaryDirectory(prefix="sgv2-eaa-lint-") as tmp:
        skill_dir = Path(tmp) / skill_name
        root = skill_dir.resolve()
        for relative, text in files.items():
            target = (skill_dir / relative).resolve()
            if root not in target.parents:
                raise EaaLintUnavailable(f"Refusing to lint a file outside the skill folder: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [sys.executable, str(script), str(skill_dir), "--json"],
                cwd=str(repo),
                env=_lint_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EaaLintUnavailable(f"The EAA skill lint could not run: {exc}") from exc

    stderr_tail = (proc.stderr or "").strip()[-800:]
    if proc.returncode not in (0, 1):
        raise EaaLintUnavailable(
            f"The EAA skill lint exited with code {proc.returncode}. {stderr_tail}".strip()
        )
    try:
        reports = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise EaaLintUnavailable(
            f"The EAA skill lint did not return JSON (exit {proc.returncode}). {stderr_tail}".strip()
        ) from exc
    if not isinstance(reports, list) or not reports or not all(isinstance(r, dict) for r in reports):
        raise EaaLintUnavailable("The EAA skill lint returned no report for the skill folder.")

    problems = [f"ERROR {msg}" for report in reports for msg in report.get("errors") or []]
    problems += [f"WARN {msg}" for report in reports for msg in report.get("warnings") or []]
    if proc.returncode == 1 and not any(p.startswith("ERROR") for p in problems):
        raise EaaLintUnavailable("The EAA skill lint exited 1 but reported no errors.")
    log_event("eaa_lint.done", skill_name=skill_name, exit_code=proc.returncode, problems=len(problems))
    return problems
