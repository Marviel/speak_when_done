"""Tests for aborting playback when the microphone goes live.

The mic check at the top of speak() only covers the instant speech is
requested. TTS generation and playback take seconds, and a user who starts
dictating during them was being talked over. These cover the two windows that
were previously unguarded: after generation, and during playback itself.
"""

import sys
import time
from unittest.mock import patch

import pytest


def test_play_audio_without_abort_check_succeeds():
    """The original blocking path is unchanged when no abort_check is given."""
    from speak_when_done import _play_audio

    result = _play_audio(["sleep"], "0.1")

    assert result["success"] is True


def test_play_audio_stops_when_abort_check_becomes_true():
    """Playback is cut short as soon as the abort predicate fires."""
    from speak_when_done import _play_audio

    started = time.monotonic()
    result = _play_audio(["sleep"], "10", abort_check=lambda: True, poll_interval=0.05)
    elapsed = time.monotonic() - started

    assert result["success"] is False
    assert result["suppressed"] is True
    assert "playback" in result["reason"]
    # The player was killed rather than allowed to run its full 10 seconds.
    assert elapsed < 5


def test_play_audio_runs_to_completion_when_abort_check_stays_false():
    """A quiet microphone lets playback finish normally."""
    from speak_when_done import _play_audio

    result = _play_audio(["sleep"], "0.2", abort_check=lambda: False, poll_interval=0.05)

    assert result["success"] is True
    assert "suppressed" not in result


def test_play_audio_aborts_partway_through():
    """A mic that goes live mid-utterance stops the speech in progress."""
    from speak_when_done import _play_audio

    deadline = time.monotonic() + 0.3

    def mic_goes_live_shortly():
        return time.monotonic() > deadline

    started = time.monotonic()
    result = _play_audio(
        ["sleep"], "10", abort_check=mic_goes_live_shortly, poll_interval=0.05
    )
    elapsed = time.monotonic() - started

    assert result["suppressed"] is True
    assert 0.3 <= elapsed < 5


def test_play_audio_reports_player_failure():
    """A failing player still surfaces an error, not a false success."""
    from speak_when_done import _play_audio

    result = _play_audio(["false"], "ignored", abort_check=lambda: False, poll_interval=0.05)

    assert result["success"] is False
    assert "error" in result


@pytest.mark.skipif(sys.platform != "darwin", reason="mic guard is macOS-only")
def test_speak_rechecks_microphone_after_generation():
    """A mic that goes live during TTS generation suppresses playback.

    First call (entry check) reports quiet; the second (post-generation check)
    reports live. Without the re-check, speech would play over the user.
    """
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active", side_effect=[False, True]), \
         patch("speak_when_done._play_audio") as play_audio:
        result = speak("Hello")

    play_audio.assert_not_called()
    assert result["success"] is False
    assert result["suppressed"] is True
