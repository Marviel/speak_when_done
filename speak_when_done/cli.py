"""
CLI entry point for speak_when_done.

Usage:
    uvx speak_when_done --text "Hello world"
    uvx speak_when_done --text "Build complete" --voice alba
"""

import argparse
import sys

from . import speak


def main():
    parser = argparse.ArgumentParser(
        prog="speak_when_done",
        description="Speak text aloud using Pocket TTS with automatic cleanup",
    )
    parser.add_argument(
        "--text",
        "-t",
        required=True,
        help="The text to speak aloud",
    )
    parser.add_argument(
        "--voice",
        "-v",
        default="alba",
        help="Voice to use (default: alba). Can be a voice name or path to audio file for cloning.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress pocket-tts output",
    )

    args = parser.parse_args()

    result = speak(args.text, voice=args.voice, quiet=args.quiet)

    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
