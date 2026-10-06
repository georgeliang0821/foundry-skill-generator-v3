"""Azure Blob-backed skill storage."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import yaml

from .diagnostics import elapsed_ms, env_flag, log_event, log_exception, now_ms
from .models import SkillFiles, SkillIndexEntry, SkillKind


# Mirrors the PERSISTED computed columns blob_path / blob_prefix on dbo.skills,
# whose formulas hard-code this root. Changing it silently orphans every blob.
BLOB_ROOT = "skills"
PRIVATE_SEGMENT = "_private"

# dbo.skills.CK_skill_name_format: [a-z0-9-] only, no leading/trailing hyphen.
MAX_SKILL_NAME_LEN = 64

# EAA runs only `scripts/<frontmatter name>.py` and refuses a skill with any other file there.
SCRIPTS_DIR = "scripts"


class VersionConflict(RuntimeError):
    """The stored SKILL.md changed since the caller last read it."""


def compute_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def safe_skill_name(name: str) -> str:
    """Sanitise to something dbo.skills.CK_skill_name_format will accept."""
    value = (name or "").strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    value = value[:MAX_SKILL_NAME_LEN].strip("-")
    return value or "skill"


def blob_prefix_of(skill_name: str, owner_upn: str | None = None) -> str:
    safe = safe_skill_name(skill_name)
    owner = (owner_upn or "").strip()
    if owner:
        return f"{BLOB_ROOT}/{PRIVATE_SEGMENT}/{owner}/{safe}/"
    return f"{BLOB_ROOT}/{safe}/"


def blob_path_of(skill_name: str, owner_upn: str | None = None) -> str:
    return f"{blob_prefix_of(skill_name, owner_upn)}SKILL.md"


def script_relpath_of(skill_name: str) -> str:
    return f"{SCRIPTS_DIR}/{safe_skill_name(skill_name)}.py"


def script_blob_path_of(skill_name: str, owner_upn: str | None = None) -> str:
    return f"{blob_prefix_of(skill_name, owner_upn)}{script_relpath_of(skill_name)}"


def managed_relpaths_of(skill_name: str) -> set[str]:
    """Files under a skill prefix that are not user assets."""
    return {"SKILL.md", script_relpath_of(skill_name)}


def _asset_content_type(relpath: str) -> str:
    guessed = mimetypes.guess_type(relpath)[0] or "text/plain"
    return f"{guessed}; charset=utf-8"


def _frontmatter_field_regex(block: str, key: str) -> str:
    """Best-effort extraction of a top-level frontmatter scalar.

    Used as a fallback when ``yaml.safe_load`` fails -- most commonly because
    an unquoted ``description`` value contains a colon+space (e.g. ``ans: x``)
    which YAML reads as an illegal nested mapping. We grab the raw remainder
    of the line so a colon in the value no longer drops the whole field.
    """
    m = re.search(rf"^{re.escape(key)}[ \t]*:[ \t]*(.*)$", block, re.MULTILINE)
    if not m:
        return ""
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def parse_frontmatter(skill_md: str) -> tuple[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", (skill_md or "").lstrip("\ufeff"), re.DOTALL)
    if not m:
        return "", ""
    block = m.group(1)
    name = ""
    description = ""
    try:
        data = yaml.safe_load(block) or {}
        if isinstance(data, dict):
            name = str(data.get("name", "") or "")
            description = str(data.get("description", "") or "")
    except yaml.YAMLError:
        # Malformed YAML (often an unquoted value containing ': '). Fall back
        # to a line-based scan below instead of dropping every field.
        pass
    if not name:
        name = _frontmatter_field_regex(block, "name")
    if not description:
        description = _frontmatter_field_regex(block, "description")
    return name, description


def replace_frontmatter_name(skill_md: str, new_name: str) -> str:
    """Rewrite only the top-level ``name:`` line of the frontmatter.

    A rename is done here rather than through a V4A patch because every other
    byte must survive untouched: CRLF endings, a folded/quoted ``description``
    the model cannot reproduce verbatim, and ``skill_name:`` occurrences in the
    body that a substring anchor would otherwise collide with.
    """
    text = skill_md or ""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    block_match = re.match(r"^---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|$)", body, re.DOTALL)
    if not block_match:
        raise ValueError("SKILL.md has no YAML frontmatter block, so it cannot be renamed.")
    block = block_match.group(1)
    # ``^`` under MULTILINE keeps this to a column-0 key, so nested `name:` under
    # `metadata:` and any `skill_name:` are left alone.
    name_match = re.search(r"^name[ \t]*:[ \t]*.*?(\r?)$", block, re.MULTILINE)
    if not name_match:
        raise ValueError("The frontmatter has no top-level `name:` key, so it cannot be renamed.")
    start, end = name_match.span()
    new_block = f"{block[:start]}name: {new_name}{name_match.group(1)}{block[end:]}"
    return bom + body[:block_match.start(1)] + new_block + body[block_match.end(1):]


def parse_frontmatter_meta(skill_md: str) -> dict:
    """Return the full ``metadata`` mapping from a SKILL.md frontmatter.

    Separate from :func:`parse_frontmatter`, whose ``(name, description)``
    signature has many callers. Returns ``{}`` when there is no frontmatter,
    it does not parse, or ``metadata`` is not a mapping -- callers treat all
    three the same way ("nothing declared").
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", (skill_md or "").lstrip("\ufeff"), re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    meta = data.get("metadata")
    return meta if isinstance(meta, dict) else {}


def infer_skill_kind(skill_md: str) -> SkillKind:
    """Classify a SKILL.md the same way ``create_session`` does.

    Deliberately keyed on ``metadata.children`` alone and never raising, so the
    kind shown in the skill list always predicts the kind a modify session will
    actually get. The ``metadata.skill_type`` consistency check stays in
    ``create_session``, which is allowed to reject the skill outright.
    """
    children = parse_frontmatter_meta(skill_md).get("children")
    if isinstance(children, list) and any(isinstance(c, str) and c.strip() for c in children):
        return SkillKind.SCENARIO
    return SkillKind.CAPABILITY


def _normalize_prefix(prefix: str) -> str:
    return "/".join(part for part in (prefix or "").strip("/").split("/") if part)


def _split_container_and_prefix(value: str) -> tuple[str, str]:
    parts = [part for part in (value or "").strip("/").split("/") if part]
    if not parts:
        return "", ""
    return parts[0], _normalize_prefix("/".join(parts[1:]))


class SkillStore(Protocol):
    def store_id(self) -> str: ...
    def list_skills(self) -> list[SkillIndexEntry]: ...
    def load_skill(self, name: str) -> SkillFiles: ...
    def save_skill(self, files: SkillFiles, expected_version_hash: str = "") -> SkillFiles: ...
    def delete_skill(self, name: str) -> None: ...
    def has_script(self, name: str) -> bool: ...
    def load_assets(self, name: str) -> dict[str, bytes]: ...


class LocalSkillStore:
    """Process-local store used only when SGV2_SKILL_STORE=local is explicit."""

    def __init__(self) -> None:
        self._skills: dict[str, SkillFiles] = {}
        # Raw bytes so a test can plant a file that would fail the asset checks.
        self._assets: dict[str, dict[str, bytes]] = {}
        self.id = "local"
        log_event("skill_store.local.ready", store_id=self.id)

    def store_id(self) -> str:
        return self.id

    def list_skills(self) -> list[SkillIndexEntry]:
        started = now_ms()
        entries: list[SkillIndexEntry] = []
        for name, files in sorted(self._skills.items()):
            parsed_name, description = parse_frontmatter(files.skill_md)
            entries.append(
                SkillIndexEntry(
                    name=parsed_name or name,
                    description=description,
                    version_hash=files.version_hash,
                    blob_store_id=self.id,
                    skill_kind=infer_skill_kind(files.skill_md),
                    has_script=files.script is not None,
                )
            )
        log_event("skill_store.local.list.done", count=len(entries), store_id=self.id, duration_ms=elapsed_ms(started))
        return entries

    def load_skill(self, name: str) -> SkillFiles:
        started = now_ms()
        safe = safe_skill_name(name)
        if safe not in self._skills:
            log_event("skill_store.local.load.missing", level="warning", skill_name=safe)
            raise FileNotFoundError(f"Skill not found: {safe}")
        files = self._skills[safe].model_copy(deep=True)
        log_event("skill_store.local.load.done", skill_name=safe, duration_ms=elapsed_ms(started))
        return files

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "") -> SkillFiles:
        started = now_ms()
        safe = safe_skill_name(files.name)
        current = self._skills.get(safe)
        if expected_version_hash and current and current.version_hash != expected_version_hash:
            log_event("skill_store.local.save.version_mismatch", level="warning", skill_name=safe)
            raise VersionConflict("Version hash mismatch; reload latest skill before saving.")
        saved = files.model_copy(deep=True)
        saved.name = safe
        saved.version_hash = compute_hash(saved.skill_md)
        saved.blob_path = blob_path_of(safe)
        if saved.script is None and current is not None:
            saved.script = current.script
        if saved.assets is not None:
            self._assets[safe] = {path: text.encode("utf-8") for path, text in saved.assets.items()}
        self._skills[safe] = saved
        log_event(
            "skill_store.local.save.done",
            skill_name=safe,
            version_hash=saved.version_hash,
            duration_ms=elapsed_ms(started),
        )
        return saved.model_copy(deep=True)

    def delete_skill(self, name: str) -> None:
        safe = safe_skill_name(name)
        existed = self._skills.pop(safe, None) is not None
        self._assets.pop(safe, None)
        log_event("skill_store.local.delete.done", skill_name=safe, existed=existed)

    def has_script(self, name: str) -> bool:
        current = self._skills.get(safe_skill_name(name))
        return current is not None and current.script is not None

    def load_assets(self, name: str) -> dict[str, bytes]:
        return dict(self._assets.get(safe_skill_name(name), {}))


class AzureBlobSkillStore:
    """Azure Blob implementation using ETag as the SKILL.md version hash."""

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "").strip().rstrip("/")
        if not account_url:
            raise ValueError("Azure Blob is not configured. Set AZURE_STORAGE_ACCOUNT_URL.")

        container_config = (
            os.getenv("AZURE_BLOB_CONTAINER", "").strip()
            or os.getenv("SKILL_BLOB_CONTAINER", "").strip()
        )
        container, embedded_prefix = _split_container_and_prefix(container_config)
        if not container:
            raise ValueError("Azure Blob container is not configured. Set AZURE_BLOB_CONTAINER.")
        self.container = container
        if embedded_prefix and embedded_prefix != BLOB_ROOT:
            raise ValueError(
                f"AZURE_BLOB_CONTAINER must be a bare container name (got prefix '{embedded_prefix}'). "
                f"dbo.skills.blob_path is a computed column that hard-codes the '{BLOB_ROOT}' "
                "root, so any other prefix makes SQL point at blobs this app "
                "never writes -- and the mismatch fails silently."
            )
        self.prefix = BLOB_ROOT

        # Blob auth uses DefaultAzureCredential but EXCLUDES the Environment
        # credential on purpose: the AZURE_TENANT_ID/AZURE_CLIENT_* service
        # principal in .env is for Azure SQL ONLY and has no blob data role, so
        # if DefaultAzureCredential picked it first every blob read/write would
        # return 403 (AuthorizationPermissionMismatch). Excluding it falls back
        # to ``az login`` locally / the managed identity in Azure, which MUST
        # have the "Storage Blob Data Contributor" role on the storage account.
        credential = DefaultAzureCredential(exclude_environment_credential=True)
        self.service = BlobServiceClient(account_url=account_url, credential=credential)
        # Stable identifier for *which* blob store this is, so a session can be
        # pinned to the store it modified and skill lists can be filtered to it.
        host = account_url.split("://", 1)[-1].split("/", 1)[0]
        self.id = f"azure:{host}/{self.container}/{self.prefix}"
        log_event(
            "skill_store.azure.ready",
            store_id=self.id,
            account_url=account_url,
            container=self.container,
            prefix=self.prefix,
        )

    def store_id(self) -> str:
        return self.id

    def _container(self):
        return self.service.get_container_client(self.container)

    def _blob_name(self, *parts: str) -> str:
        path = "/".join(part.strip("/") for part in parts if part and part.strip("/"))
        return f"{self.prefix}/{path}" if self.prefix else path

    def _skill_root(self, name: str) -> str:
        return self._blob_name(safe_skill_name(name))

    def _strip_prefix(self, blob_name: str) -> str:
        name = str(blob_name).strip("/")
        if self.prefix and name.startswith(f"{self.prefix}/"):
            return name[len(self.prefix) + 1 :]
        return name

    def _skill_path(self, name: str) -> str:
        safe = safe_skill_name(name)
        return self._blob_name(safe, "SKILL.md")

    def list_skills(self) -> list[SkillIndexEntry]:
        started = now_ms()
        entries: list[SkillIndexEntry] = []
        container = self._container()
        blobs = list(container.list_blobs(name_starts_with=f"{self.prefix}/") if self.prefix else container.list_blobs())
        relative_names = {self._strip_prefix(str(blob.name)) for blob in blobs}
        for blob in blobs:
            name = str(blob.name)
            if not name.endswith("/SKILL.md"):
                continue
            client = container.get_blob_client(name)
            content = client.download_blob().readall().decode("utf-8")
            parsed_name, description = parse_frontmatter(content)
            relative_name = self._strip_prefix(name)
            skill_dir = relative_name.rsplit("/", 1)[0]
            entries.append(
                SkillIndexEntry(
                    name=parsed_name or relative_name.split("/", 1)[0],
                    description=description,
                    version_hash=str(getattr(blob, "etag", "") or ""),
                    blob_store_id=self.id,
                    skill_kind=infer_skill_kind(content),
                    has_script=f"{skill_dir}/{script_relpath_of(skill_dir.rsplit('/', 1)[-1])}" in relative_names,
                )
            )
        sorted_entries = sorted(entries, key=lambda e: e.name)
        log_event(
            "skill_store.azure.list.done",
            store_id=self.id,
            container=self.container,
            prefix=self.prefix,
            count=len(sorted_entries),
            skill_names=[e.name for e in sorted_entries],
            duration_ms=elapsed_ms(started),
        )
        return sorted_entries

    def load_skill(self, name: str) -> SkillFiles:
        started = now_ms()
        container = self._container()
        safe = safe_skill_name(name)
        skill_path = self._blob_name(safe, "SKILL.md")
        skill_blob = container.get_blob_client(skill_path)
        skill_md = skill_blob.download_blob().readall().decode("utf-8")
        props = skill_blob.get_blob_properties()

        files = SkillFiles(
            name=safe,
            skill_md=skill_md,
            version_hash=str(props.etag),
            script=self._read_script(safe),
        )
        log_event(
            "skill_store.azure.load.done",
            skill_name=safe,
            version_hash=files.version_hash,
            has_script=files.script is not None,
            duration_ms=elapsed_ms(started),
        )
        return files

    def _script_path(self, name: str) -> str:
        return self._blob_name(safe_skill_name(name), script_relpath_of(name))

    def _read_script(self, safe: str) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = self._container().get_blob_client(self._script_path(safe)).download_blob().readall()
        except ResourceNotFoundError:
            return None
        return data.decode("utf-8")

    def has_script(self, name: str) -> bool:
        return bool(self._container().get_blob_client(self._script_path(name)).exists())

    def _skill_dir(self, name: str) -> str:
        # Trailing slash keeps skills/foo/ from matching skills/foo-bar/.
        return f"{self._skill_root(name)}/"

    def load_assets(self, name: str) -> dict[str, bytes]:
        safe = safe_skill_name(name)
        skill_dir = self._skill_dir(safe)
        managed = managed_relpaths_of(safe)
        container = self._container()
        assets: dict[str, bytes] = {}
        for blob in container.list_blobs(name_starts_with=skill_dir):
            relpath = str(blob.name)[len(skill_dir):]
            if relpath in managed:
                continue
            assets[relpath] = container.get_blob_client(str(blob.name)).download_blob().readall()
        log_event("skill_store.azure.load_assets.done", skill_name=safe, count=len(assets))
        return assets

    def _write_assets(self, safe: str, assets: dict[str, str]) -> None:
        from azure.storage.blob import ContentSettings

        container = self._container()
        skill_dir = self._skill_dir(safe)
        for relpath, text in assets.items():
            container.get_blob_client(f"{skill_dir}{relpath}").upload_blob(
                text.encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type=_asset_content_type(relpath)),
            )
        keep = managed_relpaths_of(safe) | set(assets)
        stale = [
            str(blob.name)
            for blob in container.list_blobs(name_starts_with=skill_dir)
            if str(blob.name)[len(skill_dir):] not in keep
        ]
        for blob_name in stale:
            container.delete_blob(blob_name)
        log_event("skill_store.azure.save_assets.done", skill_name=safe, written=len(assets), removed=len(stale))

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "") -> SkillFiles:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError
        from azure.storage.blob import ContentSettings

        safe = safe_skill_name(files.name)
        skill_path = self._skill_path(safe)
        container = self._container()
        skill_blob = container.get_blob_client(skill_path)

        kwargs = {}
        if expected_version_hash:
            kwargs = {
                "etag": expected_version_hash,
                "match_condition": MatchConditions.IfNotModified,
            }
        started = now_ms()
        try:
            skill_blob.upload_blob(
                files.skill_md.encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
                **kwargs,
            )
            props = skill_blob.get_blob_properties()
            if files.assets is not None:
                self._write_assets(safe, files.assets)
            # Last write before updated_at bumps: EAA caches SKILL.md but fetches the script every turn.
            if files.script is not None:
                container.get_blob_client(self._script_path(safe)).upload_blob(
                    files.script.encode("utf-8"),
                    overwrite=True,
                    content_settings=ContentSettings(content_type="text/x-python; charset=utf-8"),
                )
        except ResourceModifiedError as exc:
            log_event("skill_store.azure.save.version_mismatch", level="warning", skill_name=safe)
            raise VersionConflict("SKILL.md changed on Blob since it was loaded; reload latest skill before saving.") from exc
        except Exception as exc:
            log_exception(
                "skill_store.azure.save.failed",
                exc,
                skill_name=safe,
                skill_path=skill_path,
                duration_ms=elapsed_ms(started),
            )
            raise
        saved = files.model_copy(deep=True)
        saved.name = safe
        saved.version_hash = str(props.etag)
        saved.blob_path = skill_path
        log_event(
            "skill_store.azure.save.done",
            skill_name=safe,
            version_hash=saved.version_hash,
            skill_path=skill_path,
            duration_ms=elapsed_ms(started),
        )
        return saved

    def delete_skill(self, name: str) -> None:
        started = now_ms()
        safe = safe_skill_name(name)
        container = self._container()
        paths = [str(blob.name) for blob in container.list_blobs(name_starts_with=self._skill_dir(safe))]
        for path in paths:
            try:
                container.delete_blob(path)
                log_event(
                    "skill_store.azure.delete.done",
                    skill_name=safe,
                    skill_path=path,
                    duration_ms=elapsed_ms(started),
                )
            except Exception as exc:  # noqa: BLE001 -- missing blob is a no-op.
                log_event(
                    "skill_store.azure.delete.missing",
                    level="warning",
                    skill_name=safe,
                    skill_path=path,
                    error=str(exc),
                )
        if not paths:
            log_event("skill_store.azure.delete.missing", level="warning", skill_name=safe, skill_path=self._skill_dir(safe))


@dataclass
class CachedSkillStore:
    store: SkillStore
    ttl_seconds: int = 300
    _index: list[SkillIndexEntry] | None = None
    _expires_at: datetime | None = None

    def store_id(self) -> str:
        return self.store.store_id()

    def list_skills(self) -> list[SkillIndexEntry]:
        now = datetime.now(timezone.utc)
        if self._index is not None and self._expires_at and now < self._expires_at:
            log_event("skill_store.cache.hit", count=len(self._index), expires_at=self._expires_at.isoformat())
            return list(self._index)
        started = now_ms()
        self._index = self.store.list_skills()
        self._expires_at = now + timedelta(seconds=self.ttl_seconds)
        log_event("skill_store.cache.refresh.done", count=len(self._index), duration_ms=elapsed_ms(started))
        return list(self._index)

    def load_skill(self, name: str) -> SkillFiles:
        return self.store.load_skill(name)

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "") -> SkillFiles:
        saved = self.store.save_skill(files, expected_version_hash)
        self._index = None
        self._expires_at = None
        return saved

    def delete_skill(self, name: str) -> None:
        self.store.delete_skill(name)
        self._index = None
        self._expires_at = None

    def has_script(self, name: str) -> bool:
        return self.store.has_script(name)

    def load_assets(self, name: str) -> dict[str, bytes]:
        return self.store.load_assets(name)


def make_skill_store() -> CachedSkillStore:
    mode = os.getenv("SGV2_SKILL_STORE", "azure").strip().lower()
    if mode == "local":
        log_event("skill_store.select.local", explicit=True)
        return CachedSkillStore(LocalSkillStore())
    if mode != "azure":
        raise RuntimeError("Invalid SGV2_SKILL_STORE. Use 'azure' or explicit 'local'.")
    if not os.getenv("AZURE_STORAGE_ACCOUNT_URL", "").strip():
        raise RuntimeError(
            "Azure Blob storage is not configured. Set AZURE_STORAGE_ACCOUNT_URL "
            "(its DefaultAzureCredential identity needs the 'Storage Blob Data "
            "Contributor' role), or explicitly set SGV2_SKILL_STORE=local."
        )
    log_event(
        "skill_store.select.azure",
        account_url=env_flag("AZURE_STORAGE_ACCOUNT_URL"),
    )
    try:
        return CachedSkillStore(AzureBlobSkillStore())
    except Exception as exc:
        log_exception("skill_store.select.azure_failed", exc)
        raise
