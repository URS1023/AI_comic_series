"""Tests for the dependency-free code uploaded to the AMD node."""

from __future__ import annotations

from remote.runtime_common import parse_mountinfo


def test_mountinfo_parser_decodes_escaped_spaces() -> None:
    text = "36 25 8:1 / /mnt/large\\040disk rw,relatime - ext4 /dev/sdb1 rw\n"

    assert parse_mountinfo(text) == [("/mnt/large disk", "ext4", "/dev/sdb1")]


def test_mountinfo_parser_ignores_malformed_lines() -> None:
    assert parse_mountinfo("not a mount line\n") == []
