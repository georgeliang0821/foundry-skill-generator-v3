"""Differential check against the EAA runtime snapshot in ``reference/``.

Dev-only. ``reference/`` is a read-only copy of an external runtime and may be
absent or stale, so this test skips rather than fails in that case. Re-run it
after re-syncing ``reference/skills_provider_factory.py`` to confirm the
conservative-shape assumption still holds.

The assertion is deliberately ONE-DIRECTIONAL. We emit the strictest shape, so
"anything we accept, the runtime also accepts" must hold. The converse must not
be asserted: the runtime is more lenient than us on purpose (it accepts tuples,
for instance), and tightening ourselves to match would defeat the point.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from backend.models import Mode, SkillKind
from backend.topology import validate_topology

REFERENCE_FILE = Path(__file__).resolve().parents[2] / "reference" / "skills_provider_factory.py"
SHAPE_RULES = {"T1", "T2", "T3", "T4"}

pytestmark = pytest.mark.skipif(
    not REFERENCE_FILE.exists(),
    reason="reference/ snapshot is not present; see backend/topology.py for why it is optional",
)


def _reference_version() -> str:
    match = re.search(r"^VERSION:\s*(\S+)", REFERENCE_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else "unknown"


def _reference_declares_children():
    """Extract just ``_declares_children``; the module itself needs aioodbc et al."""
    tree = ast.parse(REFERENCE_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_declares_children":
            namespace: dict = {"yaml": yaml}
            module = ast.Module(body=[node], type_ignores=[])
            exec(compile(module, str(REFERENCE_FILE), "exec"), namespace)  # noqa: S102
            return namespace["_declares_children"]
    pytest.skip(f"_declares_children not found in {REFERENCE_FILE.name}")


def _scenario(children_yaml: str, *, prefix: str = "", description: str = "Orchestrates leave.") -> str:
    return f"""{prefix}---
name: leave-workflow
description: {description}
metadata:
  skill_type: scenario-orchestration
{children_yaml}
---

## Overview
One line.
"""


CONSERVATIVE = _scenario("  children:\n    - hr-leave-system")

CORPUS = {
    "conservative_list": CONSERVATIVE,
    "scalar_children": _scenario("  children: hr-leave-system"),
    "empty_list": _scenario("  children: []"),
    "tuple_children": _scenario("  children: !!python/tuple [a, b]"),
    "bom": "\ufeff" + CONSERVATIVE,
    "leading_blank_line": "\n" + CONSERVATIVE,
    "dashes_in_description": _scenario(
        "  children:\n    - hr-leave-system", description='"Leave --- workflow"'
    ),
    "broken_yaml": _scenario(
        "  children:\n    - hr-leave-system", description="answer: this breaks yaml"
    ),
    "top_level_children": f"""---
name: leave-workflow
description: Orchestrates leave.
children:
  - hr-leave-system
metadata:
  skill_type: scenario-orchestration
---

## Overview
One line.
""",
}


@pytest.mark.parametrize("sample_name", sorted(CORPUS))
def test_anything_we_accept_the_runtime_also_treats_as_a_parent(sample_name: str) -> None:
    sample = CORPUS[sample_name]
    reference_declares = _reference_declares_children()

    shape_errors = {
        issue.rule
        for issue in validate_topology(sample, SkillKind.SCENARIO, mode=Mode.NEW)
        if issue.rule in SHAPE_RULES
    }
    if shape_errors:
        pytest.skip(f"{sample_name} is rejected by {sorted(shape_errors)}; nothing to assert")

    assert reference_declares(sample), (
        f"{sample_name} passes our shape rules but reference/{REFERENCE_FILE.name} "
        f"(VERSION {_reference_version()}) does not read it as declaring children. "
        "The conservative-shape assumption in backend/topology.py no longer holds."
    )


def test_a_capability_skill_is_not_read_as_a_parent() -> None:
    capability = """---
name: hr-leave-system
description: Submits leave requests.
metadata:
  author: e2e
---

## Overview
One line.
"""
    assert validate_topology(capability, SkillKind.CAPABILITY, mode=Mode.NEW) == []
    assert not _reference_declares_children()(capability)
