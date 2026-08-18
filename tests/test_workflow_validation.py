"""Static graph tests for every project-owned ComfyUI API workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_comic_series.exceptions import ConfigurationError
from ai_comic_series.workflow_validation import validate_api_workflow


@pytest.mark.parametrize("path", sorted(Path("workflows/comfyui/api").glob("*.json")))
def test_project_api_workflow_is_well_formed(path: Path) -> None:
    validate_api_workflow(json.loads(path.read_text(encoding="utf-8")))


def test_workflow_validation_rejects_dangling_connection() -> None:
    document = {
        "kind": "test",
        "bindings": {},
        "prompt": {
            "1": {"class_type": "SaveImage", "inputs": {"images": ["missing", 0]}},
        },
    }

    with pytest.raises(ConfigurationError, match="unknown source"):
        validate_api_workflow(document)


def test_workflow_validation_treats_two_reference_nodes_as_node_ids() -> None:
    document = {
        "kind": "test-flf2v",
        "bindings": {"referenceNodes": ["1", "2"]},
        "prompt": {
            "1": {"class_type": "LoadImage", "inputs": {"image": "START"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "END"}},
            "3": {"class_type": "SaveVideo", "inputs": {"video": ["1", 0]}},
        },
    }

    validate_api_workflow(document)
