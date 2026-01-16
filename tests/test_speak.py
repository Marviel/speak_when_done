"""Tests for the speak_when_done core functionality."""

import subprocess
from unittest.mock import MagicMock, patch


from speak_when_done import speak


class TestSpeak:
    """Tests for the speak() function."""

    @patch("speak_when_done.subprocess.run")
    def test_speak_success(self, mock_run: MagicMock):
        """Test successful speech generation and playback."""
        # Mock both TTS generation and audio playback to succeed
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = speak("Hello world")

        assert result["success"] is True
        assert result["message"] == "Notification spoken to user"
        assert result["spoken_text"] == "Hello world"

    @patch("speak_when_done.subprocess.run")
    def test_speak_tts_failure(self, mock_run: MagicMock):
        """Test handling of TTS generation failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr="TTS error")

        result = speak("Hello world")

        assert result["success"] is False
        assert "TTS generation failed" in result["error"]

    @patch("speak_when_done.subprocess.run")
    def test_speak_playback_failure(self, mock_run: MagicMock):
        """Test handling of audio playback failure."""
        # First call (TTS) succeeds, second call (playback) fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=1, stderr="Playback error"),
        ]

        result = speak("Hello world")

        assert result["success"] is False
        assert "playback failed" in result["error"]

    @patch("speak_when_done.subprocess.run")
    def test_speak_timeout(self, mock_run: MagicMock):
        """Test handling of timeout during TTS or playback."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=60)

        result = speak("Hello world")

        assert result["success"] is False
        assert "timed out" in result["error"]

    @patch("speak_when_done.subprocess.run")
    def test_speak_command_not_found(self, mock_run: MagicMock):
        """Test handling of missing command (uvx not installed)."""
        mock_run.side_effect = FileNotFoundError("uvx not found")

        result = speak("Hello world")

        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("speak_when_done.subprocess.run")
    def test_speak_custom_voice(self, mock_run: MagicMock):
        """Test using a custom voice."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = speak("Hello world", voice="custom_voice")

        assert result["success"] is True
        # Verify the voice was passed to the TTS command
        call_args = mock_run.call_args_list[0]
        assert "custom_voice" in call_args[0][0]

    @patch("speak_when_done.subprocess.run")
    def test_speak_quiet_mode(self, mock_run: MagicMock):
        """Test quiet mode suppresses TTS output."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = speak("Hello world", quiet=True)

        assert result["success"] is True
        # Verify --quiet was passed to the TTS command
        call_args = mock_run.call_args_list[0]
        assert "--quiet" in call_args[0][0]

    @patch("speak_when_done.subprocess.run")
    def test_speak_not_quiet_by_default(self, mock_run: MagicMock):
        """Test quiet mode is off by default."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = speak("Hello world")

        assert result["success"] is True
        # Verify --quiet was NOT passed to the TTS command
        call_args = mock_run.call_args_list[0]
        assert "--quiet" not in call_args[0][0]

    @patch("speak_when_done.subprocess.run")
    @patch("speak_when_done.os.unlink")
    def test_speak_cleans_up_temp_file(
        self, mock_unlink: MagicMock, mock_run: MagicMock
    ):
        """Test that temp file is cleaned up after playback."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        speak("Hello world")

        # Verify unlink was called to clean up the temp file
        mock_unlink.assert_called_once()

    @patch("speak_when_done.subprocess.run")
    @patch("speak_when_done.os.unlink")
    def test_speak_cleans_up_on_failure(
        self, mock_unlink: MagicMock, mock_run: MagicMock
    ):
        """Test that temp file is cleaned up even on failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr="TTS error")

        speak("Hello world")

        # Verify unlink was still called to clean up
        mock_unlink.assert_called_once()

    @patch("speak_when_done.subprocess.run")
    def test_speak_handles_generic_exception(self, mock_run: MagicMock):
        """Test handling of unexpected exceptions."""
        mock_run.side_effect = RuntimeError("Unexpected error")

        result = speak("Hello world")

        assert result["success"] is False
        assert "Unexpected error" in result["error"]
