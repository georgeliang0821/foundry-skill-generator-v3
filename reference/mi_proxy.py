"""S3 — 本機 Managed Identity proxy。

只有本輪載入、且在 frontmatter 宣告 metadata.mi_scopes 的 skill,腳本才拿得到 MI token,
而且只限宣告過、又在 MI_SCOPE_ALLOWLIST 內的資源。協定同 ACA / App Service 的 MI endpoint
(GET ?resource=&api-version=2019-08-01 + X-IDENTITY-HEADER),azure-identity 不需任何改動。
2026-09-27 已在 ACA 實測(poc/poc_s3_mi.py)。

VERSION: 1.0
2026.09.27 George : 初版
"""

import logging
import os
import secrets
import socket
import urllib.parse
from typing import Collection, Dict, FrozenSet, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# 腳本拿到其中任何一個就能繞過 proxy 直接向平台 MI 取 token
MI_ENV_KEYS = (
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IDENTITY_SERVER_THUMBPRINT",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
)

# Key Vault 裡的 secret 不屬於任何單一 skill,白名單也不能開
HARD_DENIED_RESOURCES = frozenset({
    "https://vault.azure.net",
})

DEFAULT_ALLOWLIST = "https://storage.azure.com,https://ai.azure.com"

_IDENTITY_SELECTORS = ("client_id", "object_id", "principal_id", "mi_res_id", "msi_res_id")


def normalize_resource(raw) -> str:
    """'https://Storage.azure.com/.default' → 'https://storage.azure.com';不合格式回空字串。"""
    if not isinstance(raw, str):
        return ""
    r = raw.strip()
    if r.endswith("/.default"):
        r = r[: -len("/.default")]
    r = r.rstrip("/")
    u = urllib.parse.urlsplit(r)
    if u.scheme != "https" or not u.hostname or u.port or u.path or u.query or u.fragment:
        return ""
    return f"https://{u.hostname.lower()}"


def declared_mi_scopes(content: str) -> List[str]:
    """SKILL.md frontmatter 的 metadata.mi_scopes(list 或逗號字串);解析不出來回 []。"""
    if not content.startswith("---"):
        return []
    parts = content.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:
        return []
    meta = fm.get("metadata") if isinstance(fm, dict) else None
    scopes = meta.get("mi_scopes") if isinstance(meta, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split(",")
    if not isinstance(scopes, (list, tuple)):
        return []
    return [str(s).strip() for s in scopes if str(s).strip()]


def load_allowlist(raw: Optional[str] = None) -> FrozenSet[str]:
    if raw is None:
        raw = os.environ.get("MI_SCOPE_ALLOWLIST", DEFAULT_ALLOWLIST)
    allowed = set()
    for item in raw.split(","):
        if not item.strip():
            continue
        resource = normalize_resource(item)
        if not resource:
            logger.warning(f"[MIGate] MI_SCOPE_ALLOWLIST: ignoring malformed entry {item.strip()!r}")
        elif resource in HARD_DENIED_RESOURCES:
            logger.error(f"[MIGate] MI_SCOPE_ALLOWLIST: {resource} is always denied — ignored")
        else:
            allowed.add(resource)
    return frozenset(allowed)


class MiProxy:
    """127.0.0.1 上的 MI endpoint;每次 execute 發一把 header,結束即撤銷。"""

    def __init__(self, allowlist: Collection[str], credential=None):
        self.allowlist = frozenset(allowlist)
        self._credential = credential
        self._grants: Dict[str, Tuple[FrozenSet[str], str]] = {}
        self._runner = None
        self.port = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/msi/token"

    def grant(self, scopes: Collection[str], label: str) -> Optional[str]:
        """回傳本次專用的 X-IDENTITY-HEADER;沒有任何可放行的資源時回 None。"""
        resources = {normalize_resource(s) for s in scopes} - {""}
        allowed = frozenset((resources & self.allowlist) - HARD_DENIED_RESOURCES)
        dropped = sorted(resources - allowed)
        if dropped:
            logger.warning(f"[MIGate] {label}: declared but not allowed: {dropped}")
        if not allowed:
            return None
        header = secrets.token_urlsafe(32)
        self._grants[header] = (allowed, label)
        return header

    def revoke(self, header: str) -> None:
        self._grants.pop(header, None)

    async def start(self) -> None:
        from aiohttp import web

        if self._credential is None:
            from azure.identity.aio import ManagedIdentityCredential
            self._credential = ManagedIdentityCredential()
        app = web.Application()
        app.router.add_get("/msi/token", self._handle_token)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        close = getattr(self._credential, "close", None)
        if close is not None:
            await close()

    async def _handle_token(self, request):
        from aiohttp import web

        def deny(status: int, error: str, message: str):
            # azure-identity 只把 body 的 `message` 帶進例外訊息
            return web.json_response(
                {"error": error, "message": f"EAA MI proxy: {message}"}, status=status
            )

        grant = self._grants.get(request.headers.get("X-IDENTITY-HEADER", ""))
        if grant is None:
            logger.warning("[MIGate] token request with unknown or revoked header")
            return deny(401, "invalid_header", "unknown or revoked X-IDENTITY-HEADER")
        allowed, label = grant

        if any(k in request.query for k in _IDENTITY_SELECTORS):
            logger.warning(f"[MIGate] {label}: identity selector rejected")
            return deny(403, "identity_selector_not_allowed",
                        "only the platform system-assigned identity is available; "
                        "do not pass client_id / object_id / mi_res_id")

        raw = request.query.get("resource", "")
        resource = normalize_resource(raw)
        if resource not in allowed:
            logger.warning(f"[MIGate] {label}: denied resource {raw!r}")
            return deny(403, "mi_scope_not_declared",
                        f"scope {raw!r} is not declared in metadata.mi_scopes of any skill "
                        f"loaded in this turn (allowed: {', '.join(sorted(allowed))})")

        try:
            token = await self._credential.get_token(f"{resource}/.default")
        except Exception as e:
            logger.error(f"[MIGate] {label}: upstream MI failed for {resource}: {type(e).__name__}")
            return deny(502, "upstream_error", f"platform identity could not issue a token ({type(e).__name__})")

        logger.info(f"[MIGate] {label}: issued token for {resource}")
        return web.json_response({
            "access_token": token.token,
            "expires_on": str(int(token.expires_on)),
            "resource": resource,
            "token_type": "Bearer",
        })
