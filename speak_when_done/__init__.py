"""
speak_when_done - Text-to-speech with automatic temp file handling.

Generates speech using Kyutai's Pocket TTS, plays it, and cleans up.
"""

import os
import subprocess
import tempfile

__version__ = "0.1.0"


def speak(message: str, voice: str = "alba", quiet: bool = False) -> dict:
    """
    Speak a message aloud using Pocket TTS.

    Handles temp file creation and cleanup automatically.

    Args:
        message: The message to speak aloud.
        voice: Voice to use (default: "alba"). Can be a built-in voice name
               or path to an audio file for voice cloning.
        quiet: If True, suppress pocket-tts output.

    Returns:
        Dictionary with success status and details.
    """
    output_path = None
    try:
        # Create a temp file for the output audio
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name

        # Call pocket-tts via uvx
        cmd = [
            "uvx",
            "pocket-tts",
            "generate",
            "--text",
            message,
            "--voice",
            voice,
            "--output-path",
            output_path,
        ]
        if quiet:
            cmd.append("--quiet")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"TTS generation failed: {result.stderr}",
            }

        # Play the audio file using macOS afplay
        play_result = subprocess.run(
            ["afplay", output_path],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if play_result.returncode != 0:
            return {
                "success": False,
                "error": f"Audio playback failed: {play_result.stderr}",
            }

        return {
            "success": True,
            "message": "Notification spoken to user",
            "spoken_text": message,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Operation timed out",
        }
    except FileNotFoundError as e:
        return {
            "success": False,
            "error": f"Required command not found: {e}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }
    finally:
        # Always clean up the temp file
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass
