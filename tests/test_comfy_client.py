"""HTTP-level mock integration for the remote ComfyUI client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from remote.comfy_client import ComfyClient


class MockComfyHandler(BaseHTTPRequestHandler):
    media = b"mock-media-bytes"

    def log_message(self, *_: object) -> None:
        return

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/system_stats":
            self._json({"devices": [{"name": "mock-gfx1100"}]})
        elif path == "/object_info":
            self._json(
                {
                    "SaveImage": {
                        "input": {
                            "required": {"images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]},
                            "optional": {},
                            "hidden": {},
                        }
                    }
                }
            )
        elif path == "/history/prompt-1":
            self._json(
                {
                    "prompt-1": {
                        "status": {"status_str": "success"},
                        "outputs": {
                            "1": {"images": [{"filename": "result.png", "subfolder": "mock", "type": "output"}]}
                        },
                    }
                }
            )
        elif path == "/view":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.media)))
            self.end_headers()
            self.wfile.write(self.media)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        if path == "/upload/image":
            self._json({"name": "reference.png", "subfolder": "ai-comic", "type": "input"})
        elif path == "/prompt":
            self._json({"prompt_id": "prompt-1", "number": 1, "node_errors": {}})
        else:
            self._json({"error": "not found"}, 404)


@pytest.fixture
def mock_comfy() -> tuple[int, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockComfyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_comfy_client_round_trip(mock_comfy: tuple[int, ThreadingHTTPServer], tmp_path: Path) -> None:
    port, _ = mock_comfy
    source = tmp_path / "source.png"
    source.write_bytes(b"fake-png")
    target = tmp_path / "result.png"
    prompt = {
        "1": {
            "class_type": "SaveImage",
            "inputs": {"images": ["IMAGE", 0], "filename_prefix": "mock"},
        }
    }
    client = ComfyClient(port)
    try:
        assert client.health()["devices"][0]["name"] == "mock-gfx1100"
        assert client.upload_image(source, "reference.png") == "ai-comic/reference.png"
        prompt_id = client.submit(prompt)
        record = client.wait(prompt_id, timeout_seconds=2)
        descriptors = client.output_files(record)
        assert descriptors == [{"filename": "result.png", "subfolder": "mock", "type": "output"}]
        client.download(descriptors[0], target)
    finally:
        client.close()
    assert target.read_bytes() == MockComfyHandler.media
