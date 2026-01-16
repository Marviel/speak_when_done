"""Tests for the speak_when_done CLI."""

from unittest.mock import MagicMock, patch

import pytest

from speak_when_done.cli import main


class TestCLI:
    """Tests for the CLI entry point."""

    @patch("speak_when_done.cli.speak")
    def test_cli_success(self, mock_speak: MagicMock):
        """Test successful CLI invocation."""
        mock_speak.return_value = {"success": True}

        with patch("sys.argv", ["speak_when_done", "--text", "Hello"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        mock_speak.assert_called_once_with("Hello", voice="alba", quiet=False)

    @patch("speak_when_done.cli.speak")
    def test_cli_failure(self, mock_speak: MagicMock):
        """Test CLI exit code on failure."""
        mock_speak.return_value = {"success": False, "error": "Test error"}

        with patch("sys.argv", ["speak_when_done", "--text", "Hello"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("speak_when_done.cli.speak")
    def test_cli_custom_voice(self, mock_speak: MagicMock):
        """Test CLI with custom voice."""
        mock_speak.return_value = {"success": True}

        with patch("sys.argv", ["speak_when_done", "--text", "Hello", "--voice", "tom"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        mock_speak.assert_called_once_with("Hello", voice="tom", quiet=False)

    @patch("speak_when_done.cli.speak")
    def test_cli_quiet_mode(self, mock_speak: MagicMock):
        """Test CLI with quiet mode."""
        mock_speak.return_value = {"success": True}

        with patch("sys.argv", ["speak_when_done", "--text", "Hello", "--quiet"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        mock_speak.assert_called_once_with("Hello", voice="alba", quiet=True)

    @patch("speak_when_done.cli.speak")
    def test_cli_short_flags(self, mock_speak: MagicMock):
        """Test CLI with short flags."""
        mock_speak.return_value = {"success": True}

        with patch("sys.argv", ["speak_when_done", "-t", "Hello", "-v", "tom", "-q"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        mock_speak.assert_called_once_with("Hello", voice="tom", quiet=True)

    def test_cli_missing_text(self):
        """Test CLI requires --text argument."""
        with patch("sys.argv", ["speak_when_done"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            # argparse exits with code 2 for missing required arguments
            assert exc_info.value.code == 2
