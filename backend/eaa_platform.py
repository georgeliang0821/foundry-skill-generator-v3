"""Rules the EAA runtime enforces on a skill's execution environment, and its lint.

The names here mirror the EAA repo's ``code_executor.py`` (S1, commit bd13560)
and ``mi_proxy.py`` (S3, the Managed Identity gate). They are restated rather
than imported: the EAA checkout is an external tool, and ``reference/`` must
never be imported by ``backend/``. ``reference/manifest.json`` (``restated``)
fails the reference check when upstream and these copies drift apart.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from .diagnostics import log_event

# Stripped from the skill subprocess environment by EAA (SUBPROCESS_ENV_DENYLIST).
PLATFORM_SECRET_DENYLIST = frozenset({
    "OBO_CLIENT_SECRET",
    "TEAMS_NOTIFY_WEBHOOK_URL",
    "LOGIC_APP_SKILL_REVIEW_URL",
})

# Used when the live OBO_SCOPE_REGISTRY could not be read through MCP.
STATIC_OBO_REGISTRY_KEYS = frozenset({"AZURE_SQL_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"})

_RESERVED_CREDENTIAL_NAMES = frozenset({"PATH"})
# Case-sensitive, exactly as EAA's filter_caller_env matches them.
_RESERVED_CREDENTIAL_PREFIXES = ("PYTHON", "LD_", "EAA_VERIFIED_")


# --- S3 Managed Identity gate (mi_proxy.py) ---------------------------------

# Removed from the skill environment; reading one means bypassing the MI proxy.
MI_ENV_KEYS = frozenset({
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IDENTITY_SERVER_THUMBPRINT",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
})
# The platform has only a system-assigned MI; the proxy rejects any selector.
MI_IDENTITY_SELECTORS = frozenset({
    "managed_identity_client_id", "client_id", "object_id", "principal_id", "mi_res_id", "msi_res_id",
})
MI_HARD_DENIED_RESOURCES = frozenset({"https://vault.azure.net"})
DEFAULT_MI_SCOPE_ALLOWLIST = "https://storage.azure.com,https://ai.azure.com"

# Least-privilege RBAC hint for the deployment note of a non-default resource.
MI_RESOURCE_ROLES = {
    "https://storage.azure.com": "Storage Blob/Table/Queue Data Reader（需寫入時改對應的 Data Contributor）",
    "https://ai.azure.com": "Azure AI User",
    "https://search.azure.com": "Search Index Data Reader（需寫入時改 Search Index Data Contributor）",
    "https://cognitiveservices.azure.com": "Cognitive Services OpenAI User",
    "https://servicebus.azure.net": "Azure Service Bus Data Sender 或 Azure Service Bus Data Receiver",
    "https://eventhubs.azure.net": "Azure Event Hubs Data Sender 或 Azure Event Hubs Data Receiver",
    "https://database.windows.net": "資料庫內以 CREATE USER ... FROM EXTERNAL PROVIDER 建立的使用者＋最小資料庫角色（例如 db_datareader）",
    "https://management.azure.com": "Reader（範圍限縮到實際需要的資源）",
}
# Allowlisting these hands every declaring skill the platform MI's full rights there.
MI_BROAD_RESOURCES = frozenset({"https://database.windows.net", "https://management.azure.com"})

_EAA_ALLOWLIST_WARNING_RE = re.compile(r"^WARN metadata\.mi_scopes: (\S+) is not in MI_SCOPE_ALLOWLIST\b")


def normalize_mi_resource(raw: object) -> str:
    """'https://Storage.azure.com/.default' -> 'https://storage.azure.com'; '' when malformed."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if value.endswith("/.default"):
        value = value[: -len("/.default")]
    value = value.rstrip("/")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or port or parts.path or parts.query or parts.fragment:
        return ""
    return f"https://{parts.hostname.lower()}"


def declared_mi_scopes(skill_md: str) -> list[str]:
    """``metadata.mi_scopes`` exactly as EAA reads it (list or comma string); [] when absent."""
    content = skill_md or ""
    if not content.startswith("---"):
        return []
    parts = content.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return []
    meta = frontmatter.get("metadata") if isinstance(frontmatter, dict) else None
    scopes = meta.get("mi_scopes") if isinstance(meta, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    if not isinstance(scopes, (list, tuple)):
        return []
    return [str(s).strip() for s in scopes if str(s).strip()]


def mi_scope_allowlist() -> frozenset[str]:
    """The ACA's MI_SCOPE_ALLOWLIST when mirrored into .env, else the platform default."""
    raw = os.getenv("MI_SCOPE_ALLOWLIST") or DEFAULT_MI_SCOPE_ALLOWLIST
    resources = {normalize_mi_resource(item) for item in raw.split(",")}
    return frozenset(resources - {""} - MI_HARD_DENIED_RESOURCES)


def mi_allowlist_note(resource: str) -> str:
    role = MI_RESOURCE_ROLES.get(resource, "該資源上最小的 data-plane 角色")
    note = (
        f"部署前需把 `{resource}` 加入 ACA 環境變數 `MI_SCOPE_ALLOWLIST`，"
        f"並替平台 MI 指派 `{role}`。"
    )
    if resource in MI_BROAD_RESOURCES:
        note += (
            "加入後，宣告它的 skill 會取得平台 MI 在該資源上的全部權限；除非該資源本來就不以"
            "使用者身分授權，否則改用 OBO 注入的 `*_ACCESS_TOKEN`。"
        )
    return note


def split_eaa_allowlist_warnings(problems: Iterable[str]) -> tuple[list[str], list[str]]:
    """Separate EAA's 'not in MI_SCOPE_ALLOWLIST' warnings, a deployment step rather than a defect."""
    blocking: list[str] = []
    resources: list[str] = []
    for problem in problems:
        match = _EAA_ALLOWLIST_WARNING_RE.match(problem)
        if match:
            resources.append(match.group(1))
        else:
            blocking.append(problem)
    return blocking, list(dict.fromkeys(resources))


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
    if os.getenv("MI_SCOPE_ALLOWLIST"):
        env["MI_SCOPE_ALLOWLIST"] = os.environ["MI_SCOPE_ALLOWLIST"]
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
    log_event(
        "eaa_lint.done",
        skill_name=skill_name,
        exit_code=proc.returncode,
        problems=len(problems),
        eaa_commit=_checkout_commit(repo),
    )
    return problems


def _checkout_commit(repo: Path) -> str:
    # Observability only: `sync_reference.py check` compares this against the pin.
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"
