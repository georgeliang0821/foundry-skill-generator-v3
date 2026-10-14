"""Azure Blob-backed skill storage."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

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


class SkillBackupError(RuntimeError):
    """The pre-overwrite backup failed; live content was not written."""


def _backup_manifest(store_id: str, source_path: str, name: str, etag: str, captured_by: str, contents: dict[str, bytes], change_summary: str = "") -> dict:
    return {
        "schema_version": 1,
        "revision_id": uuid4().hex,
        "skill_name": name,
        "source_store": store_id,
        "source_blob_path": source_path,
        "source_etag": etag,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "captured_by": captured_by.strip().lower(),
        "change_summary": change_summary.strip(),
        "files": [
            {"path": path, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(contents.items())
        ],
    }


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


def _metadata_version_span(skill_md: str) -> tuple[int, int, str] | None:
    match = re.match(r"^\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", skill_md, re.DOTALL)
    if not match:
        return None
    try:
        root = yaml.compose(match.group(1), Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError("metadata.version requires valid YAML frontmatter.") from exc
    if not isinstance(root, yaml.MappingNode):
        return None
    for key, metadata in root.value:
        if key.value != "metadata" or not isinstance(metadata, yaml.MappingNode):
            continue
        if metadata.start_mark.index < key.end_mark.index:
            raise ValueError("metadata must not use a YAML alias when versioning a skill.")
        for field, value in metadata.value:
            if field.value == "version":
                if not isinstance(value, yaml.ScalarNode) or value.style in ("|", ">"):
                    raise ValueError("metadata.version must be a single numeric version string.")
                if value.start_mark.index < field.end_mark.index or match.group(1)[value.start_mark.index:].startswith("&"):
                    raise ValueError("metadata.version must not use YAML aliases or anchors.")
                start = match.start(1) + value.start_mark.index
                end = match.start(1) + value.end_mark.index
                return start, end, value.value
    return None


def prepare_skill_version(skill_md: str, previous_skill_md: str | None = None, *, other_files_changed: bool = False) -> str:
    """Version new content against the stored revision, leaving backups untouched."""
    current = _metadata_version_span(skill_md)
    previous = _metadata_version_span(previous_skill_md) if previous_skill_md is not None else None
    if current is None:
        if previous is not None:
            raise ValueError("Keep the stored skill's metadata.version when editing it.")
        return skill_md

    def parts(value: str) -> tuple[int, int, int]:
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?", value):
            raise ValueError("metadata.version must use MAJOR.MINOR.PATCH (a stored MAJOR.MINOR is also accepted).")
        numbers = [int(part) for part in value.split(".")]
        return numbers[0], numbers[1], numbers[2] if len(numbers) == 3 else 0

    def replace(text: str, span: tuple[int, int, str], value: str) -> str:
        return text[:span[0]] + f'"{value}"' + text[span[1]:]

    requested = parts(current[2])
    chosen = requested
    if previous is not None:
        stored = parts(previous[2])
        changed = other_files_changed or replace(skill_md, current, "0.0.0") != replace(previous_skill_md or "", previous, "0.0.0")
        if requested[:2] > stored[:2]:
            chosen = requested
        elif changed:
            chosen = stored[0], stored[1], stored[2] + 1
        else:
            return skill_md if current[2] == previous[2] else replace(skill_md, current, previous[2])
    return replace(skill_md, current, ".".join(str(part) for part in chosen))


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
    def save_skill(self, files: SkillFiles, expected_version_hash: str = "", *, create_only: bool = False, captured_by: str = "", change_summary: str = "") -> SkillFiles: ...
    def delete_skill(self, name: str) -> None: ...
    def has_script(self, name: str) -> bool: ...
    def load_assets(self, name: str) -> dict[str, bytes]: ...


class LocalSkillStore:
    """Process-local store used only when SGV2_SKILL_STORE=local is explicit."""

    def __init__(self, *, history_enabled: bool = False) -> None:
        self._skills: dict[str, SkillFiles] = {}
        self._lock = threading.RLock()
        self.history_enabled = history_enabled
        self.history: dict[str, dict] = {}
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

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "", *, create_only: bool = False, captured_by: str = "", change_summary: str = "") -> SkillFiles:
        with self._lock:
            return self._save_skill(files, expected_version_hash, create_only=create_only, captured_by=captured_by, change_summary=change_summary)

    def _save_skill(self, files: SkillFiles, expected_version_hash: str, *, create_only: bool, captured_by: str, change_summary: str) -> SkillFiles:
        started = now_ms()
        safe = safe_skill_name(files.name)
        current = self._skills.get(safe)
        if create_only and current is not None:
            raise VersionConflict("Skill already exists; choose a different name.")
        if expected_version_hash and (current is None or current.version_hash != expected_version_hash):
            log_event("skill_store.local.save.version_mismatch", level="warning", skill_name=safe)
            raise VersionConflict("Version hash mismatch; reload latest skill before saving.")
        if self.history_enabled and current is not None:
            contents = dict(self._assets.get(safe, {}))
            contents["SKILL.md"] = current.skill_md.encode("utf-8")
            if current.script is not None:
                contents[script_relpath_of(safe)] = current.script.encode("utf-8")
            manifest = _backup_manifest(self.id, blob_path_of(safe), safe, current.version_hash, captured_by, contents, change_summary)
            self.history[manifest["revision_id"]] = {"manifest": manifest, "files": contents}
        saved = files.model_copy(deep=True)
        saved.name = safe
        saved.version_hash = compute_hash(saved.skill_md)
        saved.blob_path = blob_path_of(safe)
        if saved.script is None and current is not None and not saved.remove_script:
            saved.script = current.script
        saved.remove_script = False
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
        self.history_container = os.getenv("AZURE_BLOB_HISTORY_CONTAINER", "").strip()
        if self.history_container and (
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.history_container)
            or not 3 <= len(self.history_container) <= 63
            or self.history_container == self.container
        ):
            raise ValueError("AZURE_BLOB_HISTORY_CONTAINER must be a separate, valid bare container name.")
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

    def load_frontmatter(self, name: str, owner_upn: str | None = None) -> tuple[str, str]:
        path = blob_path_of(name, owner_upn)
        data = self._container().get_blob_client(path).download_blob().readall().decode("utf-8")
        return parse_frontmatter(data)

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

    def _list_entries(self, prefix: str) -> tuple[list[str], list[str]]:
        """Return (files, directories) under prefix; directories only exist on HNS accounts."""
        files: list[str] = []
        dirs: list[str] = []
        for blob in self._container().list_blobs(name_starts_with=prefix, include=["metadata"]):
            is_dir = (getattr(blob, "metadata", None) or {}).get("hdi_isfolder") == "true"
            (dirs if is_dir else files).append(str(blob.name))
        return files, dirs

    def load_assets(self, name: str) -> dict[str, bytes]:
        safe = safe_skill_name(name)
        skill_dir = self._skill_dir(safe)
        managed = managed_relpaths_of(safe)
        container = self._container()
        assets: dict[str, bytes] = {}
        for blob_name in self._list_entries(skill_dir)[0]:
            relpath = blob_name[len(skill_dir):]
            if relpath in managed:
                continue
            assets[relpath] = container.get_blob_client(blob_name).download_blob().readall()
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
        stale = [blob_name for blob_name in self._list_entries(skill_dir)[0] if blob_name[len(skill_dir):] not in keep]
        for blob_name in stale:
            container.delete_blob(blob_name)
        log_event("skill_store.azure.save_assets.done", skill_name=safe, written=len(assets), removed=len(stale))

    def _backup_skill(self, safe: str, expected_version_hash: str, captured_by: str, change_summary: str = "") -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
        from azure.storage.blob import ContentSettings

        container = self._container()
        skill_path = self._skill_path(safe)
        prefix = self._skill_dir(safe)
        skill_blob = container.get_blob_client(skill_path)
        try:
            source_etag = str(skill_blob.get_blob_properties().etag)
        except ResourceNotFoundError:
            if expected_version_hash:
                raise VersionConflict("SKILL.md was deleted since it was loaded.")
            return ""
        if expected_version_hash and source_etag != expected_version_hash:
            raise VersionConflict("SKILL.md changed since it was loaded.")

        contents: dict[str, bytes] = {}
        etags: dict[str, str] = {}
        try:
            names = sorted(self._list_entries(prefix)[0])
            if skill_path not in names:
                raise VersionConflict("SKILL.md disappeared during backup.")
            for name in names:
                client = container.get_blob_client(name)
                etag = source_etag if name == skill_path else str(client.get_blob_properties().etag)
                contents[name[len(prefix):]] = client.download_blob(
                    etag=etag, match_condition=MatchConditions.IfNotModified,
                ).readall()
                etags[name] = etag
        except (ResourceModifiedError, ResourceNotFoundError) as exc:
            raise VersionConflict("Skill files changed during backup; reload before saving.") from exc

        manifest = _backup_manifest(self.id, skill_path, safe, source_etag, captured_by, contents, change_summary)
        captured_at = datetime.fromisoformat(manifest["captured_at"])
        timestamp = captured_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        revision_prefix = f"{safe}/{timestamp}_{manifest['revision_id']}/"
        history = self.service.get_container_client(self.history_container)
        for path, data in sorted(contents.items()):
            history.get_blob_client(f"{revision_prefix}files/{path}").upload_blob(data, overwrite=False)
        try:
            if sorted(self._list_entries(prefix)[0]) != names:
                raise VersionConflict("Skill file set changed during backup.")
            for name, etag in etags.items():
                if str(container.get_blob_client(name).get_blob_properties().etag) != etag:
                    raise VersionConflict("Skill files changed during backup.")
        except ResourceNotFoundError as exc:
            raise VersionConflict("Skill files disappeared during backup.") from exc
        history.get_blob_client(f"{revision_prefix}manifest.json").upload_blob(
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json; charset=utf-8"),
        )
        log_event("skill_store.backup.done", skill_name=safe, revision_id=manifest["revision_id"], history_container=self.history_container)
        return source_etag

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "", *, create_only: bool = False, captured_by: str = "", change_summary: str = "") -> SkillFiles:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        from azure.storage.blob import ContentSettings

        safe = safe_skill_name(files.name)
        skill_path = self._skill_path(safe)
        container = self._container()
        skill_blob = container.get_blob_client(skill_path)

        if getattr(self, "history_container", "") and not create_only:
            try:
                expected_version_hash = self._backup_skill(safe, expected_version_hash, captured_by, change_summary)
            except VersionConflict:
                raise
            except Exception as exc:
                log_exception("skill_store.backup.failed", exc, skill_name=safe)
                raise SkillBackupError("Skill backup failed; live content was not saved. Contact the administrator.") from exc
            if not expected_version_hash:
                create_only = True
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
                overwrite=not create_only,
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
            elif files.remove_script:
                try:
                    container.delete_blob(self._script_path(safe))
                except ResourceNotFoundError:
                    pass
        except (ResourceModifiedError, ResourceExistsError) as exc:
            log_event("skill_store.azure.save.version_mismatch", level="warning", skill_name=safe)
            raise VersionConflict("SKILL.md changed on Blob since it was loaded; reload latest skill before saving.") from exc
        except ResourceNotFoundError as exc:
            if expected_version_hash:
                raise VersionConflict("SKILL.md was deleted since it was loaded.") from exc
            raise
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
        files, dirs = self._list_entries(self._skill_dir(safe))
        # HNS refuses to delete a non-empty directory, so files go first, then dirs deepest-first.
        if dirs:
            dirs = sorted(dirs, key=lambda d: d.count("/"), reverse=True) + [self._skill_root(safe)]
        paths = files + dirs
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
        if not files:
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

    def load_frontmatter(self, name: str, owner_upn: str | None = None) -> tuple[str, str]:
        if isinstance(self.store, LocalSkillStore):
            if owner_upn:
                raise FileNotFoundError("Scoped local skill content is unavailable.")
            return parse_frontmatter(self.store.load_skill(name).skill_md)
        return self.store.load_frontmatter(name, owner_upn)

    def save_skill(self, files: SkillFiles, expected_version_hash: str = "", *, create_only: bool = False, captured_by: str = "", change_summary: str = "") -> SkillFiles:
        saved = self.store.save_skill(files, expected_version_hash, create_only=create_only, captured_by=captured_by, change_summary=change_summary)
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
