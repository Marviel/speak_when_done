"""
CLI entry point for speak_when_done.

Usage:
    speak_when_done --text "Hello world"
    speak_when_done --text "Build complete" --voice alba
    speak_when_done --text "Done" --profile galadriel
    speak_when_done --list-voices
    speak_when_done --list-profiles
"""

import argparse
import json
import sys

from . import speak, list_voices
from .voices import load_profiles, get_profile, get_default_profile_name


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
        default=None,
        help="Voice to use. Can be a voice name or path to audio file for cloning.",
    )
    parser.add_argument(
        "--profile", "-p",
        default=None,
        help="Voice profile name from voices.yaml config.",
    )
    parser.add_argument(
        "--speed", "-s",
        type=float,
        default=None,
        help="Playback speed multiplier (default: 1.0). Requires ffmpeg.",
    )
    parser.add_argument(
        "--warmup", "-w",
        default=None,
        help="Text prepended for voice cloning warmup (e.g. '... ...').",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress pocket-tts output",
    )
    parser.add_argument(
        "--ignore-meeting",
        action="store_true",
        help="Speak even if microphone is active (override meeting suppression)",
    )
    parser.add_argument(
        "--list-voices", "-l",
        action="store_true",
        help="List available built-in voices and exit",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List configured voice profiles and exit",
    )
    parser.add_argument(
        "--profile-json",
        action="store_true",
        help="Output the resolved profile as JSON (for use by hooks/scripts)",
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

    # Handle --list-profiles
    if args.list_profiles:
        profiles = load_profiles()
        default_name = get_default_profile_name()
        print("Voice profiles:")
        for name, cfg in profiles.items():
            marker = " (default)" if name == default_name else ""
            print(f"  {name}{marker}:")
            print(f"    voice:   {cfg['voice']}")
            print(f"    speed:   {cfg['speed']}")
            print(f"    warmup:  {cfg['warmup']!r}")
            if cfg['persona']:
                print(f"    persona: {cfg['persona'][:80]}...")
        sys.exit(0)

    # Resolve profile
    profile_name = args.profile or get_default_profile_name()
    profile = get_profile(profile_name)

    # Build speak kwargs from profile + CLI overrides
    voice = args.voice or (profile["voice"] if profile else "alba")
    speed = args.speed if args.speed is not None else (profile["speed"] if profile else 1.0)
    warmup = args.warmup if args.warmup is not None else (profile["warmup"] if profile else "")

    # Handle --profile-json (output resolved settings for scripts)
    if args.profile_json:
        info = {
            "profile": profile_name,
            "voice": voice,
            "speed": speed,
            "warmup": warmup,
            "persona": profile["persona"] if profile else "",
        }
        print(json.dumps(info))
        sys.exit(0)

    # Require --text if not listing
    if not args.text:
        parser.error("--text is required unless using --list-voices or --list-profiles")

    result = speak(args.text, voice=voice, quiet=args.quiet, speed=speed, warmup=warmup,
                   suppress_in_meeting=not args.ignore_meeting)

    if not result["success"]:
        if result.get("suppressed"):
            print(f"Suppressed: {result['reason']}", file=sys.stderr)
        else:
            print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
