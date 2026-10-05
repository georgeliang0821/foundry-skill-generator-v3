"""Rules the EAA runtime enforces on a skill's execution environment, and its lint.

The names here mirror the EAA repo's ``code_executor.py`` (S1, commit bd13560)
and ``mi_proxy.py`` (S3, the Managed Identity gate). They are restated rather
than imported: ``reference/`` must never be imported by ``backend/``.
``reference/manifest.json`` (``restated``) fails the reference check when
upstream and these copies drift apart. The authoritative lint runs inside EAA
and is reached through its MCP tool ``lint_skill_package``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from .mcp_jsonrpc import lint_skill_package_jsonrpc

# Stripped from the skill subprocess environment by EAA (SUBPROCESS_ENV_DENYLIST).
PLATFORM_SECRET_DENYLIST = frozenset({
    "OBO_CLIENT_SECRET",
    "TEAMS_NOTIFY_WEBHOOK_URL",
    "LOGIC_APP_SKILL_REVIEW_URL",
})

# Used when the live OBO_SCOPE_REGISTRY could not be read through MCP.
STATIC_OBO_REGISTRY_KEYS = frozenset({"AZURE_SQL_ACCESS_TOKEN", "GRAPH_ACCESS_TOKEN"})

# Scanned case-insensitively over the stdout of an exit-0 run (keys of HARD_ERROR_PATTERNS);
# any hit downgrades the run to content_error unless a line starts with [NEEDS_INFO].
CONTENT_ERROR_HARD_PATTERNS = (
    r'"error":',
    r'"status":\s*"[^"]*error"',
    r'\bHTTP[/ ]\d(?:\.\d)?\s+(?:4\d{2}|5\d{2})\b',
    r'\b(?:status[_ ]?code|statusCode|status|code)\s*[:=]\s*(?:4\d{2}|5\d{2})\b',
    r'\b(?:4\d{2}|5\d{2})\s+(?:Unauthorized|Forbidden|Not\s+Found|Internal\s+Server\s+Error|Bad\s+Request|Bad\s+Gateway|Service\s+Unavailable|Gateway\s+Timeout|Conflict|Too\s+Many\s+Requests)\b',
    r'Missing dependencies?:',
    r'ModuleNotFoundError',
    r'ImportError',
)

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

_EAA_ALLOWLIST_WARNING_RE = re.compile(r"metadata\.mi_scopes: (\S+) is not in MI_SCOPE_ALLOWLIST\b")


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
    """The platform default; EAA's lint reports against the ACA's real MI_SCOPE_ALLOWLIST at save."""
    resources = {normalize_mi_resource(item) for item in DEFAULT_MI_SCOPE_ALLOWLIST.split(",")}
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


def split_eaa_allowlist_warnings(warnings: Iterable[str]) -> tuple[list[str], list[str]]:
    """Separate EAA's 'not in MI_SCOPE_ALLOWLIST' warnings, a deployment step rather than a defect."""
    others: list[str] = []
    resources: list[str] = []
    for warning in warnings:
        match = _EAA_ALLOWLIST_WARNING_RE.search(warning)
        if match:
            resources.append(match.group(1))
        else:
            others.append(warning)
    return others, list(dict.fromkeys(resources))


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


# Both must be on for EAA to load a script-based skill (core_handler.startup / skills_sync).
SCRIPT_SKILL_FLAGS = ("DYNAMIC_SKILLS_ENABLED", "SKILL_SCRIPTS_ENABLED")


def script_flags_off(aca_env_result: Mapping[str, Any] | None) -> list[str]:
    """The script-skill flags that are not on in ``architectural_config``; a missing flag is off."""
    config = aca_env_result.get("architectural_config") if isinstance(aca_env_result, Mapping) else None
    config = config if isinstance(config, Mapping) else {}
    # EAA reads these as os.environ.get(NAME, "false").lower() == "true".
    return [name for name in SCRIPT_SKILL_FLAGS if str(config.get(name, "false")).lower() != "true"]


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
# EAA MCP lint_skill_package
# ---------------------------------------------------------------------------


class EaaLintUnavailable(RuntimeError):
    """The EAA lint could not produce a verdict. Saving must fail closed."""


class EaaLintRejected(RuntimeError):
    """EAA refused the request or package itself (``status == "failed"``)."""


@dataclass(frozen=True)
class EaaLintResult:
    valid: bool
    errors: list[str]
    warnings: list[str]
    ruleset_version: str


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EaaLintUnavailable(f"lint_skill_package returned a non-list `{field}`.")
    return [str(item) for item in value]


def lint_skill_package(skill_name: str, files: Mapping[str, str], *, timeout: float = 60) -> EaaLintResult:
    """Lint the whole skill package with EAA's own rules through MCP.

    Raises ``EaaLintUnavailable`` whenever no trustworthy verdict exists and
    ``EaaLintRejected`` when EAA rejects the request.
    """
    mcp_url = os.getenv("MCP_ENDPOINT", "").strip().rstrip("/")
    if not mcp_url:
        raise EaaLintUnavailable(
            "MCP_ENDPOINT is not set. Saving requires EAA's lint_skill_package MCP tool."
        )
    payload, error = lint_skill_package_jsonrpc(mcp_url, skill_name, dict(files), timeout=timeout)
    if error is not None or payload is None:
        raise EaaLintUnavailable(f"lint_skill_package could not be called: {error}")
    status = payload.get("status")
    if status == "failed":
        raise EaaLintRejected(str(payload.get("error") or "lint_skill_package reported status=failed."))
    if status != "completed" or not isinstance(payload.get("valid"), bool):
        raise EaaLintUnavailable(f"lint_skill_package returned an unexpected report (status={status!r}).")
    errors = _strings(payload.get("errors"), "errors")
    return EaaLintResult(
        # A report listing errors is never a pass, whatever `valid` says.
        valid=payload["valid"] and not errors,
        errors=errors,
        warnings=_strings(payload.get("warnings"), "warnings"),
        ruleset_version=str(payload.get("ruleset_version") or ""),
    )
