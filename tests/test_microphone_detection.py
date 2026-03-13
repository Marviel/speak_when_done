"""Tests for microphone detection and meeting suppression."""

import sys
from unittest.mock import patch, MagicMock

import pytest


def test_is_microphone_active_returns_bool():
    """is_microphone_active() returns a boolean."""
    from speak_when_done import is_microphone_active

    result = is_microphone_active()
    assert isinstance(result, bool)


def test_is_microphone_active_non_darwin_returns_false():
    """On non-macOS platforms, always returns False."""
    from speak_when_done import is_microphone_active

    with patch.object(sys, "platform", "linux"):
        assert is_microphone_active() is False

    with patch.object(sys, "platform", "win32"):
        assert is_microphone_active() is False
