"""Checks for user-supplied skill assets. Files are accepted or rejected, never converted."""

from __future__ import annotations

from collections.abc import Mapping

from .blob_store import parse_frontmatter_meta
from .models import Session, SkillKind

ASSET_DIRS = ("assets", "references")
MAX_ASSETS = 9
MAX_ASSET_CHARS = 20_000
MAX_TOTAL_CHARS = 40_000
# A UTF-8 character is at most 4 bytes, so anything larger cannot fit MAX_ASSET_CHARS.
MAX_ASSET_BYTES = MAX_ASSET_CHARS * 4

BINARY_EXTENSIONS = frozenset(
    {"xlsx", "docx", "pdf", "pptx", "png", "jpg", "jpeg", "gif", "ico", "woff", "woff2", "ttf", "otf", "zip"}
)


class AssetRejected(ValueError):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def path_problem(path: str) -> str | None:
    if "\\" in path:
        return "Use `/` in asset paths, not `\\`."
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return "The path contains a control character."
    parts = path.split("/")
    if len(parts) != 2 or parts[0] not in ASSET_DIRS:
        return "Put the file directly under `assets/` or `references/`; subdirectories are not supported."
    name = parts[1]
    if not name or name in {".", ".."}:
        return "The file name is empty."
    if name.startswith("."):
        return "The file name must not start with `.`."
    if name.lower() == "skill.md":
        return "`SKILL.md` is the skill itself and cannot be an asset."
    return None


def check_asset(path: str, data: bytes | str) -> str:
    """Return the asset text, or raise AssetRejected explaining why it cannot ship."""
    problem = path_problem(path)
    if problem:
        raise AssetRejected(path, problem)
    if isinstance(data, bytes):
        name = path.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in BINARY_EXTENSIONS:
            raise AssetRejected(
                path, f"`.{ext}` is a binary format. Only UTF-8 text files can be skill assets."
            )
        if len(data) > MAX_ASSET_BYTES:
            raise AssetRejected(path, f"The file exceeds {MAX_ASSET_CHARS:,} characters.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise AssetRejected(path, "The file is not valid UTF-8 text.") from None
    else:
        text = data
    if "\x00" in text:
        raise AssetRejected(path, "The file contains NUL bytes, so it is not a text file.")
    if len(text) > MAX_ASSET_CHARS:
        raise AssetRejected(path, f"The file has {len(text):,} characters; the limit is {MAX_ASSET_CHARS:,}.")
    return text


def assets_blocked_reason(session: Session) -> str | None:
    """Why this session's skill cannot carry assets at all; None when it can."""
    if SkillKind(session.skill_kind) is SkillKind.SCENARIO:
        return "A scenario skill cannot carry assets."
    if session.skill_form == "script":
        return "A script skill ships only SKILL.md and its script, so it cannot carry assets."
    if parse_frontmatter_meta(session.current_skill.skill_md).get("children"):
        return "This SKILL.md declares `metadata.children`, so it is a scenario skill and cannot carry assets."
    return None


def check_asset_set(session: Session, assets: Mapping[str, str | bytes]) -> list[str]:
    """Every reason the full asset set cannot ship with this session's skill; empty when it can."""
    if not assets:
        return []
    problems: list[str] = []
    blocked = assets_blocked_reason(session)
    if blocked:
        problems.append(blocked)
    if len(assets) > MAX_ASSETS:
        problems.append(f"{len(assets)} assets attached; the limit is {MAX_ASSETS}.")
    total = 0
    seen: dict[str, str] = {}
    for path, data in assets.items():
        try:
            text = check_asset(path, data)
        except AssetRejected as exc:
            problems.append(str(exc))
            continue
        total += len(text)
        basename = path.rsplit("/", 1)[-1].lower()
        if basename in seen:
            problems.append(f"{path}: same file name as `{seen[basename]}`; names must be unique across both folders.")
        else:
            seen[basename] = path
    if total > MAX_TOTAL_CHARS:
        problems.append(f"Assets total {total:,} characters; the limit is {MAX_TOTAL_CHARS:,}.")
    return problems
