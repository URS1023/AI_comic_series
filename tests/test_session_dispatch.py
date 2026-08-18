"""Dispatch tests for the long-lived credential session."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from ai_comic_series.session import dispatch


def test_session_dispatch_rejects_unknown_generation_stage() -> None:
    with pytest.raises(ValueError, match="Unknown generation stage"):
        dispatch(Mock(), {"action": "generate", "stage": "placeholder-stills"})


def test_session_dispatch_reads_status_without_mutation() -> None:
    manager = Mock()
    manager.read_status.return_value = {"state": "running", "phase": "download"}

    result = dispatch(manager, {"action": "status", "job": "models"})

    assert result == {"state": "running", "phase": "download"}
    manager.read_status.assert_called_once_with("models")
