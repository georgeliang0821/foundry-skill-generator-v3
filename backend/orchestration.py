"""Shared scenario ownership contract; no runtime or store dependencies."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

import yaml

OrchestrationOwner = Literal["host", "eaa"]
_FRONTMATTER = re.compile(r"^\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


def orchestration_errors(frontmatter: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    for field in ("orchestration_owner", "orchestration", "orchestration_ref"):
        if field in frontmatter:
            errors.append(f"{field} must be declared under metadata, not at the top level.")
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        return errors + ["metadata must be a mapping."]
    for field in ("orchestration", "orchestration_ref"):
        if field in metadata:
            errors.append(f"metadata.{field} is unsupported; write the flow in the SKILL.md body.")
    if "orchestration_owner" in metadata:
        if metadata["orchestration_owner"] not in ("host", "eaa"):
            errors.append("metadata.orchestration_owner must be host or eaa.")
        if metadata.get("skill_type") != "scenario-orchestration":
            errors.append("metadata.orchestration_owner is only valid for scenario-orchestration.")
    return errors


def skill_orchestration_owner(skill_md: str) -> OrchestrationOwner:
    """Resolve a valid declaration, or the legacy Host default; never coerce an invalid owner."""
    match = _FRONTMATTER.match(skill_md or "")
    if not match:
        raise ValueError("A YAML frontmatter mapping is required to resolve orchestration ownership.")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter; orchestration ownership cannot be resolved.") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("The frontmatter must be a mapping.")
    errors = orchestration_errors(frontmatter)
    if errors:
        raise ValueError(" ".join(errors))
    metadata = frontmatter.get("metadata", {})
    return "eaa" if metadata.get("orchestration_owner") == "eaa" else "host"


def is_eaa_scenario(skill_md: str) -> bool:
    return skill_orchestration_owner(skill_md) == "eaa"


def is_eaa_metadata(metadata: Mapping[str, object]) -> bool:
    return metadata.get("skill_type") == "scenario-orchestration" and metadata.get("orchestration_owner") == "eaa"
