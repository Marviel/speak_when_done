"""
speak_when_done - Text-to-speech with automatic temp file handling.

Generates speech using Kyutai's Pocket TTS, plays it, and cleans up.
"""

import os
import shutil
import subprocess
import sys
import tempfile

__version__ = "0.1.0"

# Built-in voices available in Pocket TTS
BUILTIN_VOICES = [
    {"name": "alba", "description": "Default female voice"},
    {"name": "alicia", "description": "Female voice variant"},
    {"name": "carla", "description": "Female voice variant"},
    {"name": "charlie", "description": "Male voice"},
    {"name": "danna", "description": "Female voice variant"},
    {"name": "elena", "description": "Female voice variant"},
    {"name": "emily", "description": "Female voice variant"},
    {"name": "erica", "description": "Female voice variant"},
    {"name": "guy", "description": "Male voice"},
    {"name": "jessica", "description": "Female voice variant"},
    {"name": "ken", "description": "Male voice variant"},
    {"name": "laura", "description": "Female voice variant"},
    {"name": "lina", "description": "Female voice variant"},
    {"name": "luca", "description": "Male voice variant"},
    {"name": "lucia", "description": "Female voice variant"},
    {"name": "mark", "description": "Male voice"},
    {"name": "maya", "description": "Female voice variant"},
    {"name": "michael", "description": "Male voice"},
    {"name": "mira", "description": "Female voice variant"},
    {"name": "nisha", "description": "Female voice variant"},
    {"name": "paola", "description": "Female voice variant"},
    {"name": "rosie", "description": "Female voice variant"},
    {"name": "sandra", "description": "Female voice variant"},
    {"name": "sara", "description": "Female voice variant"},
    {"name": "sarah", "description": "Female voice variant"},
    {"name": "sophia", "description": "Female voice variant"},
    {"name": "tom", "description": "Male voice"},
]


def list_voices() -> dict:
    """
    List available voices for text-to-speech.

    Returns:
        Dictionary with list of built-in voices and instructions for custom voices.
    """
    return {
        "success": True,
        "builtin_voices": BUILTIN_VOICES,
        "default_voice": "alba",
        "custom_voice_hint": "You can also use a path to an audio file for voice cloning.",
    }


def _get_audio_player() -> list[str] | None:
    """
    Get the appropriate audio player command for the current platform.

    Returns:
        List of command arguments for the audio player, or None if no player found.
    """
    platform = sys.platform

    if platform == "darwin":
        # macOS: use afplay (built-in)
        if shutil.which("afplay"):
            return ["afplay"]
    elif platform == "win32":
        # Windows: use PowerShell to play audio
        # This uses the built-in .NET audio player
        return [
            "powershell", "-c",
            "(New-Object Media.SoundPlayer '{path}').PlaySync()"
        ]
    else:
        # Linux/BSD: try common audio players in order of preference
        linux_players = [
            ["paplay"],      # PulseAudio (most common on modern Linux)
            ["aplay"],       # ALSA (fallback)
            ["ffplay", "-nodisp", "-autoexit"],  # FFmpeg (if installed)
        ]
        for player in linux_players:
            if shutil.which(player[0]):
                return player

    return None


def _play_audio(player_cmd: list[str], audio_path: str, timeout: int = 30) -> dict:
    """
    Play an audio file using the specified player command.

    Args:
        player_cmd: The audio player command (may contain {path} placeholder).
        audio_path: Path to the audio file to play.
        timeout: Maximum time to wait for playback in seconds.

    Returns:
        Dictionary with success status and any error details.
    """
    # Handle Windows PowerShell which needs the path embedded in the command
    if "powershell" in player_cmd[0].lower():
        # Replace {path} placeholder with actual path
        cmd = [arg.replace("{path}", audio_path) for arg in player_cmd]
    else:
        # For other players, append the path as an argument
        cmd = player_cmd + [audio_path]

    play_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if play_result.returncode != 0:
        return {
            "success": False,
            "error": f"Audio playback failed: {play_result.stderr}",
        }

    return {"success": True}


DEFAULT_VOICE = os.environ.get("SPEAK_WHEN_DONE_VOICE", "alba")


def speak(message: str, voice: str = DEFAULT_VOICE, quiet: bool = False) -> dict:
    """
    Speak a message aloud using Pocket TTS.

    Handles temp file creation and cleanup automatically.
    Supports macOS, Linux, and Windows.

    Args:
        message: The message to speak aloud.
        voice: Voice to use (default: "alba"). Can be a built-in voice name
               or path to an audio file for voice cloning.
        quiet: If True, suppress pocket-tts output.

    Returns:
        Dictionary with success status and details.
    """
    # Check for audio player before doing any work
    player_cmd = _get_audio_player()
    if player_cmd is None:
        return {
            "success": False,
            "error": f"No audio player found for platform '{sys.platform}'. "
                     "Install one of: afplay (macOS), paplay/aplay (Linux), "
                     "or ensure PowerShell is available (Windows).",
        }

    output_path = None
    try:
        # Create a temp file for the output audio
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name

        # Call pocket-tts via uvx
        cmd = [
            "uvx", "pocket-tts", "generate",
            "--text", message,
            "--voice", voice,
            "--output-path", output_path,
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

        # Play the audio file
        play_result = _play_audio(player_cmd, output_path)
        if not play_result["success"]:
            return play_result

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
