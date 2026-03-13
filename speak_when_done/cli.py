"""
CLI entry point for speak_when_done.

Usage:
    uvx speak_when_done --text "Hello world"
    uvx speak_when_done --text "Build complete" --voice alba
    uvx speak_when_done --list-voices
"""

import argparse
import sys

from . import speak, list_voices


def main():
    parser = argparse.ArgumentParser(
        prog="speak_when_done",
        description="Speak text aloud using Pocket TTS with automatic cleanup",
    )
    parser.add_argument(
        "--text", "-t",
        help="The text to speak aloud",
    )
    parser.add_argument(
        "--voice", "-v",
        default="alba",
        help="Voice to use (default: alba). Can be a voice name or path to audio file for cloning.",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress pocket-tts output",
    )
    parser.add_argument(
        "--list-voices", "-l",
        action="store_true",
        help="List available voices and exit",
    )

    args = parser.parse_args()

    # Handle --list-voices
    if args.list_voices:
        result = list_voices()
        print("Available voices:")
        print(f"  Default: {result['default_voice']}")
        print("\n  Built-in voices:")
        for voice in result["builtin_voices"]:
            print(f"    - {voice['name']}: {voice['description']}")
        print(f"\n  {result['custom_voice_hint']}")
        sys.exit(0)

    # Require --text if not listing voices
    if not args.text:
        parser.error("--text is required unless using --list-voices")

    result = speak(args.text, voice=args.voice, quiet=args.quiet)

    if not result["success"]:
        if result.get("suppressed"):
            print(f"Suppressed: {result['reason']}", file=sys.stderr)
        else:
            print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
