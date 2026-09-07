"""Section-name mechanics for the two-tier skill topology.

A capability skill's ``##`` headings are a public API: a scenario skill points
at them by name through ``fetch_skill(sections=...)``. This module is the one
place that decides what a heading *is* and when a requested name *matches* it.
``topology.py`` and ``skill_lint.py`` both import from here so a rule and the
thing it validates can never drift apart.

Three properties are load-bearing and must not be "tidied up":

* Normalization lowercases FIRST and strips SECOND. Swapping the order leaves
  uppercase ASCII in the result, which silently breaks every match.
* Matching is substring containment, not equality, so a caller may omit
  decorative emoji and punctuation but never invent words.
* Only ``##`` splits. Deeper headings belong to the ``##`` above them, and
  content before the first ``##`` belongs to no section at all -- it cannot be
  requested, so nothing load-bearing may live there.
"""

from __future__ import annotations

import re

# Everything outside [0-9a-z] and CJK is decoration: whitespace, emoji,
# backticks, half- and full-width punctuation, parentheses, hyphens,
# underscores, full-width alphanumerics, kana, hangul.
SECTION_STRIP_RE = re.compile(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+")

_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")


def normalize_section(name: str) -> str:
    """Fold a section name to its comparable form. Lowercase first, then strip."""
    return SECTION_STRIP_RE.sub("", (name or "").lower())


def h2_sections(skill_md: str) -> list[tuple[str, str]]:
    """Return ``(title, body)`` per ``##`` heading, in original document order.

    ``###`` and below stay inside the ``##`` they follow. A ``##`` line inside a
    fenced code block is content, not a heading. Text before the first ``##`` is
    dropped, because no ``sections`` request can ever reach it.
    """
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in (skill_md or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if out:
                out[-1][1].append(line)
            continue
        match = None if in_fence else _H2_RE.match(line)
        if match:
            out.append((match.group(1).strip(), []))
        elif out:
            out[-1][1].append(line)
    return [(title, "\n".join(body)) for title, body in out]


def h2_titles(skill_md: str) -> list[str]:
    return [title for title, _body in h2_sections(skill_md)]


def match_section(requested: str, titles: list[str]) -> list[str]:
    """Titles matching ``requested``, in document order rather than request order."""
    wanted = normalize_section(requested)
    if not wanted:
        return []
    return [title for title in titles if wanted in normalize_section(title)]


def ambiguous_titles(titles: list[str]) -> list[tuple[str, str]]:
    """Title pairs that cannot be addressed independently.

    A pair is ambiguous when one normalized form contains the other: naming the
    shorter one always returns both, so the longer one is unreachable alone.
    Equal forms are the degenerate case of the same problem.
    """
    pairs: list[tuple[str, str]] = []
    folded = [(title, normalize_section(title)) for title in titles]
    for i, (title_a, norm_a) in enumerate(folded):
        for title_b, norm_b in folded[i + 1 :]:
            if not norm_a or not norm_b:
                continue
            if norm_a in norm_b or norm_b in norm_a:
                pairs.append((title_a, title_b))
    return pairs
