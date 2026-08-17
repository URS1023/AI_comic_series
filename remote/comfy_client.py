"""Small synchronous ComfyUI API client used only inside the AMD node."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import requests


class ComfyClient:
    """Submit workflows and retrieve their concrete media outputs."""

    def __init__(self, port: int, timeout_seconds: float = 60) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.client_id = uuid.uuid4().hex
        self._object_info: dict[str, Any] | None = None

    def close(self) -> None:
        self.session.close()

    def health(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/system_stats", timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("ComfyUI system_stats response is not an object")
        return data

    def object_info(self) -> dict[str, Any]:
        if self._object_info is not None:
            return self._object_info
        response = self.session.get(f"{self.base_url}/object_info", timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("ComfyUI object_info response is not an object")
        self._object_info = data
        return data

    def validate_prompt(self, prompt: dict[str, Any]) -> None:
        """Fail before queueing when the pinned API graph drifts from real nodes."""

        object_info = self.object_info()
        errors: list[str] = []
        for node_id, node in prompt.items():
            class_type = str(node.get("class_type", ""))
            schema = object_info.get(class_type)
            if not isinstance(schema, dict):
                errors.append(f"{node_id}: unknown class_type {class_type}")
                continue
            input_schema = schema.get("input", {})
            if not isinstance(input_schema, dict):
                errors.append(f"{node_id}: {class_type} has malformed input schema")
                continue
            required = input_schema.get("required", {})
            optional = input_schema.get("optional", {})
            hidden = input_schema.get("hidden", {})
            allowed = set(required) | set(optional) | set(hidden)
            actual = set(node.get("inputs", {}))
            unknown = sorted(actual - allowed)
            missing = sorted(set(required) - actual)
            if unknown:
                errors.append(f"{node_id}: {class_type} unknown inputs {unknown}")
            if missing:
                errors.append(f"{node_id}: {class_type} missing required inputs {missing}")
        if errors:
            raise RuntimeError("ComfyUI API schema mismatch:\n" + "\n".join(errors))

    def upload_image(self, path: Path, remote_name: str) -> str:
        with path.open("rb") as handle:
            response = self.session.post(
                f"{self.base_url}/upload/image",
                files={"image": (remote_name, handle, "application/octet-stream")},
                data={"type": "input", "overwrite": "true"},
                timeout=max(self.timeout_seconds, 300),
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("name"):
            raise RuntimeError(f"ComfyUI upload did not return an image name: {data}")
        subfolder = str(data.get("subfolder", "")).strip("/")
        return f"{subfolder}/{data['name']}" if subfolder else str(data["name"])

    def submit(self, prompt: dict[str, Any]) -> str:
        self.validate_prompt(prompt)
        response = self.session.post(
            f"{self.base_url}/prompt",
            json={"prompt": prompt, "client_id": self.client_id},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("prompt_id"):
            raise RuntimeError(f"ComfyUI rejected the prompt: {data}")
        if data.get("node_errors"):
            raise RuntimeError(f"ComfyUI node validation errors: {data['node_errors']}")
        return str(data["prompt_id"])

    def wait(self, prompt_id: str, timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self.session.get(
                f"{self.base_url}/history/{prompt_id}",
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            record = data.get(prompt_id) if isinstance(data, dict) else None
            if isinstance(record, dict):
                status = record.get("status", {})
                if isinstance(status, dict) and status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    raise RuntimeError(f"ComfyUI execution failed: {messages[-5:]}")
                outputs = record.get("outputs")
                if isinstance(outputs, dict) and outputs:
                    return record
            time.sleep(2)
        raise TimeoutError(f"ComfyUI prompt {prompt_id} exceeded {timeout_seconds} seconds")

    @staticmethod
    def output_files(record: dict[str, Any]) -> list[dict[str, str]]:
        """Recursively collect concrete output descriptors from history."""

        collected: list[dict[str, str]] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("filename"), str):
                    collected.append(
                        {
                            "filename": value["filename"],
                            "subfolder": str(value.get("subfolder", "")),
                            "type": str(value.get("type", "output")),
                        }
                    )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(record.get("outputs", {}))
        unique: dict[tuple[str, str, str], dict[str, str]] = {}
        for item in collected:
            key = (item["filename"], item["subfolder"], item["type"])
            unique[key] = item
        return list(unique.values())

    def download(self, descriptor: dict[str, str], target: Path) -> None:
        response = self.session.get(
            f"{self.base_url}/view",
            params=descriptor,
            stream=True,
            timeout=max(self.timeout_seconds, 300),
        )
        response.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                if chunk:
                    handle.write(chunk)
