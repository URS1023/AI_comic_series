"""Static validation for project-owned ComfyUI API workflow documents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_comic_series.exceptions import ConfigurationError

__all__ = ["validate_api_workflow"]

OUTPUT_CLASSES = {"SaveImage", "SaveVideo"}


def _connection(value: object) -> tuple[str, int] | None:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
        return value[0], value[1]
    return None


def validate_api_workflow(document: Mapping[str, Any]) -> None:
    """Validate graph references, bindings, and a concrete media output node."""

    prompt = document.get("prompt")
    bindings = document.get("bindings")
    kind = document.get("kind")
    errors: list[str] = []
    if not isinstance(kind, str) or not kind:
        errors.append("kind must be a non-empty string")
    if not isinstance(prompt, dict) or not prompt:
        errors.append("prompt must be a non-empty object")
        prompt = {}
    if not isinstance(bindings, dict):
        errors.append("bindings must be an object")
        bindings = {}
    for node_id, node in prompt.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            errors.append(f"{node_id}: missing class_type")
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            errors.append(f"{node_id}: inputs must be an object")
            continue
        for input_name, value in inputs.items():
            connection = _connection(value)
            if connection is None:
                continue
            source_id, output_index = connection
            if source_id not in prompt:
                errors.append(f"{node_id}.{input_name}: unknown source node {source_id}")
            if output_index < 0:
                errors.append(f"{node_id}.{input_name}: negative output index")
    if not any(node.get("class_type") in OUTPUT_CLASSES for node in prompt.values() if isinstance(node, dict)):
        errors.append("workflow has no SaveImage or SaveVideo output")
    for name, binding in bindings.items():
        if isinstance(binding, list) and len(binding) == 2 and all(isinstance(item, str) for item in binding):
            node_id, input_name = binding
            if node_id not in prompt:
                errors.append(f"binding {name}: unknown node {node_id}")
            elif input_name not in prompt[node_id].get("inputs", {}):
                errors.append(f"binding {name}: unknown input {node_id}.{input_name}")
        elif name == "referenceNodes" and isinstance(binding, list):
            for node_id in binding:
                if node_id not in prompt or prompt[node_id].get("class_type") != "LoadImage":
                    errors.append(f"binding referenceNodes: {node_id} is not a LoadImage node")
        elif name in {"referenceNode", "positiveNode", "negativeNode"} and isinstance(binding, str):
            if binding not in prompt:
                errors.append(f"binding {name}: unknown node {binding}")
    if errors:
        raise ConfigurationError("Invalid ComfyUI API workflow:\n" + "\n".join(errors))
