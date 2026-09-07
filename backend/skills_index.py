"""DB-backed existing-skills index for PREPARE overlap detection.

Sources cards from:
  - dbo.skills (SQL) -- name, description, enabled flag
  - skill store list (Blob) -- only show skills with actual content

The keyword matcher uses Jaccard over a tokenized blob of name + description.

ACL: ``keyword_topn`` filters to skills the user has a
grant on (via ``acl.visible_skill_names(upn)``).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import skills_repo
from .blob_store import parse_frontmatter_meta
from .models import SkillKind

LOGGER = logging.getLogger(__name__)


_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
        "for", "from", "have", "i", "in", "is", "it", "of", "on", "or",
        "skill", "skills", "that", "the", "this", "to", "use", "used",
        "uses", "using", "when", "with", "you", "your", "what",
        "agent", "agents", "tool", "tools", "ai",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
_CACHE_TTL_SECONDS = 60


def _tokenize(text: str) -> set[str]:
    tokens = {t.lower() for t in _TOKEN_RE.findall(text or "")}
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class SkillCard:
    name: str
    description: str
    when_to_use: str = ""
    path: str = ""
    keywords: set[str] = field(default_factory=set)
    is_internal: bool = False
    declares_children: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "path": self.path,
            "is_internal": self.is_internal,
        }


class SkillsIndex:
    """SQL+Blob-backed cards with a tiny TTL cache."""

    def __init__(self, blob_store: Any | None = None, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._cards: list[SkillCard] = []
        self._cache_upn = ""
        self._expires_at: float = 0.0
        self._ttl = ttl_seconds
        self._blob_store = blob_store

    def attach_blob_store(self, blob_store: Any) -> None:
        self._blob_store = blob_store

    def _all_cards(self, upn: str) -> list[SkillCard]:
        now = time.time()
        with self._lock:
            if self._cards and self._cache_upn == upn and self._expires_at > now:
                return list(self._cards)
        cards: list[SkillCard] = []
        try:
            blob_descriptions: dict[str, str] = {}
            if self._blob_store is not None:
                try:
                    blob_descriptions = {
                        s.name: (s.description or "") for s in self._blob_store.list_skills()
                    }
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("skills_index.blob_list_failed: %s", str(exc)[:200])
            try:
                sql_rows = skills_repo.list_skills_for_user(upn)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("skills_index.sql_list_failed: %s", str(exc)[:200])
                sql_rows = []
            for row in sql_rows:
                name = (row.skill_name or "").strip()
                if blob_descriptions and name not in blob_descriptions:
                    # SQL row without blob content -- skip (consistent with /api/skills).
                    continue
                description = blob_descriptions.get(name, "").strip()
                declares_children = False
                if self._blob_store is not None:
                    try:
                        metadata = parse_frontmatter_meta(self._blob_store.load_skill(name).skill_md)
                        children = metadata.get("children")
                        declares_children = bool(
                            isinstance(children, list)
                            and children
                            and all(isinstance(child, str) and child.strip() for child in children)
                        )
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("skills_index.blob_load_failed name=%s: %s", name, str(exc)[:200])
                keywords = _tokenize(f"{name} {description}")
                cards.append(
                    SkillCard(
                        name=name,
                        description=description[:500],
                        when_to_use="",
                        path=f"sql:{name}",
                        keywords=keywords,
                        is_internal=bool(row.is_internal),
                        declares_children=declares_children,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("skills_index.load_failed: %s", str(exc)[:200])
        with self._lock:
            self._cards = cards
            self._cache_upn = upn
            self._expires_at = time.time() + self._ttl
        LOGGER.info("skills_index.loaded count=%d", len(cards))
        return list(cards)

    def keyword_topn(
        self,
        topic: str,
        capabilities: list[str],
        n: int = 10,
        *,
        upn: str = "",
        kind: SkillKind | None = None,
        exclude: set[str] | None = None,
    ) -> list[tuple[SkillCard, float]]:
        cards = self._all_cards(upn) if upn else []
        if kind is not None:
            resolved_kind = SkillKind(kind)
            if resolved_kind is SkillKind.SCENARIO:
                cards = [card for card in cards if not card.is_internal]
            else:
                cards = [card for card in cards if not card.declares_children]
        if exclude:
            # `is_internal` only lands once a scenario declaring the child has been
            # saved, so a freshly authored child is still caught by the kind filter.
            cards = [card for card in cards if card.name not in exclude]
        query = _tokenize(topic + " " + " ".join(capabilities))
        scored = [(card, _jaccard(query, card.keywords)) for card in cards]
        scored = [(c, s) for c, s in scored if s > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:n]

    def invalidate(self) -> None:
        with self._lock:
            self._cards = []
            self._cache_upn = ""
            self._expires_at = 0.0


_singleton: SkillsIndex | None = None
_singleton_lock = threading.Lock()


def get_skills_index() -> SkillsIndex:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SkillsIndex()
    return _singleton
