"""Protocol-level tests that do not contact the real AMD server."""

from __future__ import annotations

from ai_comic_series.jupyter import JupyterClient


def test_execute_message_uses_non_persistent_history() -> None:
    message = JupyterClient.build_execute_message("print('ok')", "session-id", "message-id")

    assert message["header"]["msg_id"] == "message-id"
    assert message["header"]["session"] == "session-id"
    assert message["content"]["store_history"] is False
    assert message["content"]["allow_stdin"] is False
    assert message["content"]["code"] == "print('ok')"


def test_contents_path_quotes_spaces_but_preserves_segments() -> None:
    assert JupyterClient._contents_path("project/a file.json") == "/api/contents/project/a%20file.json"
