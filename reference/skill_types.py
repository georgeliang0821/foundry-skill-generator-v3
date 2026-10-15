"""Pure skill classification and package contract shared by platform consumers."""

from dataclasses import dataclass
from collections.abc import Sequence

import yaml

SKILL_TYPES = ("instruction-only", "scenario-orchestration", "inline", "script")
SKILL_TYPE_CONTRACT_VERSION = "1.0"
GENERATOR_SKILL_TYPES = ("scenario-orchestration", "inline", "script")


@dataclass(frozen=True)
class SkillClassification:
    skill_type: str | None
    children: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def classify_skill(
    content: str, *, skill_name: str | None = None,
    scripts: Sequence[str] | None = None,
) -> SkillClassification:
    """Classify without I/O; scripts=None means the inventory is not yet known."""
    errors: list[str] = []
    warnings: list[str] = []
    frontmatter: dict = {}
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) != 3:
            errors.append("unterminated YAML frontmatter")
        else:
            try:
                parsed = yaml.safe_load(parts[1])
                if not isinstance(parsed, dict):
                    errors.append("frontmatter must be a mapping")
                else:
                    frontmatter = parsed
            except yaml.YAMLError:
                errors.append("invalid YAML frontmatter")
    if "skill_type" in frontmatter:
        errors.append("skill_type must be declared under metadata.skill_type")
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        errors.append("metadata must be a mapping")
        metadata = {}
    children_value = metadata.get("children", [])
    if isinstance(children_value, str):
        children_value = [children_value]
    children: tuple[str, ...] = ()
    if not isinstance(children_value, list) or any(
        not isinstance(child, str) or not child.strip() for child in children_value
    ):
        errors.append("metadata.children must be a name or a list of nonempty names")
    else:
        children = tuple(children_value)
    declared = metadata.get("skill_type")
    if "skill_type" not in metadata:
        resolved = "scenario-orchestration" if children else "script" if scripts else "inline"
        warnings.append("metadata.skill_type is missing; inferred " + resolved)
    elif declared == "code-generation":
        resolved = "inline"
        warnings.append("code-generation is a compatibility alias; write inline")
    elif isinstance(declared, str) and declared in SKILL_TYPES:
        resolved = declared
    else:
        resolved = None
        errors.append("metadata.skill_type must be one of: " + ", ".join(SKILL_TYPES))
    if resolved == "scenario-orchestration" and not children:
        errors.append("scenario-orchestration requires nonempty metadata.children")
    if scripts is not None:
        inventory = sorted(path.replace("\\", "/") for path in scripts)
        if resolved == "script":
            name = skill_name or frontmatter.get("name")
            expected = f"scripts/{name}.py"
            if not name or inventory != [expected]:
                errors.append(f"script skill must contain exactly {expected}; found {inventory}")
            if skill_name and frontmatter.get("name", skill_name) != skill_name:
                errors.append("script frontmatter name must equal the skill name")
        elif inventory:
            errors.append(f"{resolved or 'invalid'} skill must not bundle scripts/")
    return SkillClassification(resolved, children, tuple(errors), tuple(warnings))