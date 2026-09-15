from __future__ import annotations

import os
import re

import yaml

from .models import InputBinding, Session, SkillKind
from .sections import h2_sections, normalize_section


class _BindingLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise ValueError("input-bindings requires unique string mapping keys.")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def request_inputs_enabled() -> bool:
    return os.getenv("SGV2_ENABLE_REQUEST_INPUTS", "").strip().lower() in {"1", "true", "yes"}


def parse_input_bindings(skill_md: str) -> list[InputBinding] | None:
    bodies = [body for title, body in h2_sections(skill_md) if normalize_section(title) == "requiredinputs"]
    blocks = [block for body in bodies for block in re.findall(
        r"^```input-bindings[ \t]*\r?\n(.*?)^```[ \t]*$", body, re.MULTILINE | re.DOTALL,
    )]
    if not blocks:
        if any("```input-bindings" in body for body in bodies):
            raise ValueError("Unclosed input-bindings block in Required Inputs.")
        return None
    if len(blocks) != 1:
        raise ValueError("Required Inputs must contain exactly one input-bindings block.")
    try:
        raw = yaml.load(blocks[0], Loader=_BindingLoader)
        if not isinstance(raw, list) or not raw:
            raise ValueError("input-bindings must be a non-empty YAML list.")
        bindings = [InputBinding.model_validate(item) for item in raw]
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid input-bindings contract: {exc}") from exc
    names = [binding.name for binding in bindings]
    if len(names) != len(set(names)):
        raise ValueError("Each business input must have exactly one source.")
    return bindings


def binding_signature(bindings: list[InputBinding]) -> list[tuple]:
    return sorted((
        binding.name, binding.source,
        (binding.credentials_key or binding.name) if binding.source == "credentials" else "",
        binding.payload_field, binding.required,
    ) for binding in bindings)


def variable_bindings(session: Session) -> list[InputBinding]:
    return [InputBinding.model_validate(variable.model_dump(include={
        "name", "source", "credentials_key", "payload_field", "required",
    })) for variable in session.prepare_brief.variables if variable.kind == "runtime" and variable.name]


def input_contract_errors(session: Session, skill_md: str | None = None) -> list[str]:
    errors: list[str] = []
    if SkillKind(session.skill_kind) is SkillKind.CAPABILITY:
        bindings = variable_bindings(session)
        if skill_md is not None:
            try:
                declared = parse_input_bindings(skill_md)
            except ValueError as exc:
                return [str(exc)]
            explicit = any(binding.source == "request" or binding.credentials_key or binding.payload_field for binding in bindings)
            if (explicit and declared is None) or (bindings and declared is not None and binding_signature(declared) != binding_signature(bindings)):
                errors.append("Required Inputs bindings do not match the confirmed runtime inputs.")
            bindings = declared if declared is not None else bindings
        if len({binding.name for binding in bindings}) != len(bindings):
            errors.append("Each runtime input must have exactly one source.")
        if any(binding.source == "request" for binding in bindings) and not request_inputs_enabled():
            errors.append("Request-derived business inputs are experimental; enable SGV2_ENABLE_REQUEST_INPUTS for validation first.")
    else:
        for delegation in session.prepare_brief.delegation:
            child_md = session.child_full_md.get(delegation.child_skill, "")
            try:
                declared = parse_input_bindings(child_md)
            except ValueError as exc:
                errors.append(f"{delegation.child_skill}: {exc}")
                continue
            if declared is None:
                if delegation.input_bindings:
                    errors.append(f"{delegation.child_skill}: child has no explicit input-bindings contract; do not invent one in the scenario.")
                continue
            if binding_signature(declared) != binding_signature(delegation.input_bindings):
                errors.append(f"{delegation.child_skill}: delegation input sources must match the child's Required Inputs.")
            if delegation.credentials_key:
                errors.append(f"{delegation.child_skill}: use input_bindings, not an additional legacy credentials_key.")
            if any(binding.source == "request" for binding in declared) and not request_inputs_enabled():
                errors.append(f"{delegation.child_skill}: request inputs require SGV2_ENABLE_REQUEST_INPUTS.")
    return errors