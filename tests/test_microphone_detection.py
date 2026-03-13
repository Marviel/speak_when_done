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


def test_speak_suppressed_when_mic_active():
    """speak() returns suppressed result when mic is active."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active", return_value=True), \
         patch.object(sys, "platform", "darwin"):
        result = speak("Hello")

    assert result["success"] is False
    assert result["suppressed"] is True
    assert "reason" in result


def test_speak_not_suppressed_when_mic_inactive():
    """speak() proceeds normally when mic is not active and verifies check ran."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active", return_value=False) as mock_mic, \
         patch.object(sys, "platform", "darwin"), \
         patch("speak_when_done._get_audio_player", return_value=["afplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True)

    mock_mic.assert_called_once()
    assert result["success"] is True


def test_speak_suppress_in_meeting_false_skips_check():
    """speak(suppress_in_meeting=False) skips mic check entirely."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active") as mock_mic, \
         patch("speak_when_done._get_audio_player", return_value=["afplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True, suppress_in_meeting=False)

    mock_mic.assert_not_called()
    assert result["success"] is True


def test_speak_suppression_skipped_on_non_darwin():
    """speak() does not check mic on non-macOS even with suppress_in_meeting=True."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active") as mock_mic, \
         patch.object(sys, "platform", "linux"), \
         patch("speak_when_done._get_audio_player", return_value=["paplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True)

    mock_mic.assert_not_called()
    assert result["success"] is True
