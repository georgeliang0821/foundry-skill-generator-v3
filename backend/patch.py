"""Small V4A patch parser and apply harness."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class PatchError(ValueError):
    """Raised when a V4A patch cannot be applied safely."""


@dataclass
class Hunk:
    anchor: str = ""
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    operation: str
    path: str
    hunks: list[Hunk] = field(default_factory=list)


def version_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def parse_v4a(patch: str) -> list[FilePatch]:
    lines = (patch or "").splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise PatchError("Patch must start with '*** Begin Patch' and end with '*** End Patch'.")

    files: list[FilePatch] = []
    current: FilePatch | None = None
    current_hunk: Hunk | None = None
    for raw in lines[1:-1]:
        if raw.startswith("*** Update File: "):
            current = FilePatch("update", raw.removeprefix("*** Update File: ").strip())
            files.append(current)
            current_hunk = None
            continue
        if raw.startswith("*** Add File: "):
            current = FilePatch("add", raw.removeprefix("*** Add File: ").strip())
            files.append(current)
            current_hunk = Hunk()
            current.hunks.append(current_hunk)
            continue
        if raw.startswith("*** Delete File: "):
            current = FilePatch("delete", raw.removeprefix("*** Delete File: ").strip())
            files.append(current)
            current_hunk = None
            continue
        if current is None:
            if raw.strip():
                raise PatchError(f"Patch content appears before a file header: {raw}")
            continue
        if raw.startswith("@@"):
            anchor = raw[2:].strip()
            current_hunk = Hunk(anchor=anchor)
            current.hunks.append(current_hunk)
            continue
        if current_hunk is None:
            if raw.strip():
                raise PatchError(f"Hunk line appears before an @@ anchor: {raw}")
            continue
        current_hunk.lines.append(raw)

    if not files:
        raise PatchError("Patch does not contain any file operation.")
    return files


def _find_unique(content: str, needle: str) -> tuple[int, str]:
    if not needle:
        return 0, ""
    matches = [i for i in range(len(content)) if content.startswith(needle, i)]
    if len(matches) == 1:
        return matches[0], needle
    if len(matches) > 1:
        raise PatchError("Anchor is ambiguous; provide more context.")

    normalized_content = content.replace("\r\n", "\n")
    normalized_needle = needle.replace("\r\n", "\n")
    matches = [i for i in range(len(normalized_content)) if normalized_content.startswith(normalized_needle, i)]
    if len(matches) == 1:
        original_start, original_end = _normalized_span_to_original_span(
            content,
            matches[0],
            matches[0] + len(normalized_needle),
        )
        return original_start, content[original_start:original_end]
    if len(matches) > 1:
        raise PatchError("Anchor is ambiguous after EOL normalization.")
    raise PatchError("Anchor not found.")


def _normalized_span_to_original_span(content: str, start: int, end: int) -> tuple[int, int]:
    index_map: list[int] = []
    i = 0
    while i < len(content):
        if content.startswith("\r\n", i):
            index_map.append(i)
            i += 2
        else:
            index_map.append(i)
            i += 1
    if start > len(index_map) or end > len(index_map):
        raise PatchError("Anchor not found after EOL normalization.")
    original_start = index_map[start] if start < len(index_map) else len(content)
    original_end = index_map[end] if end < len(index_map) else len(content)
    return original_start, original_end


def _old_new_blocks(hunk: Hunk) -> tuple[str, str]:
    old: list[str] = []
    new: list[str] = []
    for line in hunk.lines:
        if line.startswith("+"):
            new.append(line[1:])
        elif line.rstrip() == "---":
            # A frontmatter fence copied as context without its leading space,
            # not a deletion of "--".
            old.append(line)
            new.append(line)
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith(" "):
            text = line[1:]
            old.append(text)
            new.append(text)
        elif line == "":
            old.append("")
            new.append("")
        else:
            old.append(line)
            new.append(line)
    return "\n".join(old), "\n".join(new)


def apply_v4a_to_content(content: str, patch: str, *, expected_version_hash: str = "") -> tuple[str, str]:
    if expected_version_hash and expected_version_hash != version_hash(content):
        raise PatchError("Version hash mismatch; sync the latest content before applying patch.")
    file_patches = parse_v4a(patch)
    if len(file_patches) != 1:
        raise PatchError("This endpoint applies exactly one target file patch at a time.")
    fp = file_patches[0]
    if fp.operation == "delete":
        return "", version_hash("")
    if fp.operation == "add":
        body_lines: list[str] = []
        for hunk in fp.hunks:
            for line in hunk.lines:
                if line.startswith("+"):
                    body_lines.append(line[1:])
                elif line.startswith(" "):
                    body_lines.append(line[1:])
                elif line:
                    body_lines.append(line)
        updated = "\n".join(body_lines)
        return updated, version_hash(updated)
    if fp.operation != "update":
        raise PatchError(f"Unsupported operation: {fp.operation}")

    updated = content
    for hunk in fp.hunks:
        old, new = _old_new_blocks(hunk)
        search = old or hunk.anchor
        if hunk.anchor and old and not old.startswith(hunk.anchor):
            search = old
        try:
            idx, matched = _find_unique(updated, search)
            updated = updated[:idx] + new + updated[idx + len(matched):]
        except PatchError as exc:
            # The agent's context lines may not reproduce the file's exact
            # indentation, which would make an otherwise valid edit fail. Fall
            # back to the same WHITESPACE-TOLERANT line match used for direct
            # replacements so the V4A patch still applies. An AMBIGUOUS anchor is
            # a real problem the agent must fix (it needs more context), so only
            # the "not found" case is retried; the hunk body (old -> new) is also
            # required, since anchor-only hunks cannot be salvaged this way.
            if "ambiguous" in str(exc).lower() or not old:
                raise
            updated = apply_find_replace(updated, old, new)
    return updated, version_hash(updated)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _reindent(replace_lines: list[str], find_indent: str, content_indent: str) -> list[str]:
    """Shift ``replace_lines`` by the indent the model got wrong on the find side.

    The whitespace-tolerant match already accepted that the model's indentation
    does not reproduce the file's. Splicing its replacement in verbatim would
    then import that same wrong indentation -- which is how a stray space in
    front of a frontmatter key ends up corrupting the YAML.
    """
    if find_indent == content_indent:
        return replace_lines
    shifted = []
    for line in replace_lines:
        if not line.strip():
            shifted.append(line)
        elif line.startswith(find_indent):
            shifted.append(content_indent + line[len(find_indent):])
        else:
            shifted.append(content_indent + line.lstrip())
    return shifted


def apply_find_replace(content: str, find: str, replace: str) -> str:
    """Replace the unique occurrence of ``find`` in ``content`` with ``replace``.

    Matching tries an exact substring first, then a WHITESPACE-TOLERANT line match
    (per-line leading/trailing whitespace is ignored) so the model does not have
    to perfectly reproduce YAML/Markdown indentation. In that fallback the
    replacement is re-indented to the file's own indentation. Raises PatchError
    when ``find`` is empty, not found, or matches more than once.
    """
    if not find.strip():
        raise PatchError("find text is empty.")
    exact = content.count(find)
    if exact == 1:
        return content.replace(find, replace, 1)
    if exact > 1:
        raise PatchError("find text matches more than once; provide a more unique snippet.")
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    while find_lines and find_lines[0].strip() == "":
        find_lines.pop(0)
    while find_lines and find_lines[-1].strip() == "":
        find_lines.pop()
    if not find_lines:
        raise PatchError("find text is empty after trimming.")
    norm_content = [ln.strip() for ln in content_lines]
    norm_find = [ln.strip() for ln in find_lines]
    span = len(norm_find)
    matches = [
        i for i in range(0, len(norm_content) - span + 1)
        if norm_content[i:i + span] == norm_find
    ]
    if len(matches) > 1:
        raise PatchError("find text matches more than once (whitespace-normalized); provide more context.")
    if not matches:
        raise PatchError("find text not found in the current SKILL.md.")
    i = matches[0]
    # find_lines[0] is non-blank (leading blanks were trimmed), so it is the
    # reference point for how far off the model's indentation was.
    replace_lines = _reindent(
        replace.split("\n"),
        _indent_of(find_lines[0]),
        _indent_of(content_lines[i]),
    )
    new_lines = content_lines[:i] + replace_lines + content_lines[i + span:]
    return "\n".join(new_lines)
