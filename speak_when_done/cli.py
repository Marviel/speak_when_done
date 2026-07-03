"""
CLI entry point for speak_when_done.

Usage:
    speak_when_done --text "Hello world"
    speak_when_done --text "Build complete" --voice alba
    speak_when_done --text "Done" --profile galadriel
    speak_when_done --list-voices
    speak_when_done --list-profiles
    speak_when_done --validate-personas
"""

import argparse
import json
import os
import sys

from . import _CALL_LOG, _DESYNC_LOG, list_voices, speak, validate_persona_sync
from .voices import _load_raw_config, get_default_profile_name, get_profile, load_profiles


def _print_tail(path: str, n: int, *, label: str = "calls") -> None:
    """Print the last n JSONL records, one per line, in a compact format."""
    if not os.path.exists(path):
        print(f"(no {label} log yet at {path})")
        return
    with open(path) as f:
        lines = f.readlines()
    for line in lines[-n:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            print(line.rstrip())
            continue
        ts = r.get("ts", "?")
        event = r.get("event", "?")
        persona = r.get("persona", "-")
        voice = (r.get("voice") or "-").rsplit("/", 1)[-1]
        prompt_sha = r.get("prompt_sha", "-")
        voice_sha = r.get("voice_sha") or "-"
        drift = r.get("drift")
        drift_str = f"  DRIFT={drift}" if drift else ""
        print(f"{ts}  {event:18s}  {persona:14s}  {voice:36s}  p={prompt_sha}  v={voice_sha}{drift_str}")


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
        help="Voice to use (default: from the configured profile, or the "
             "per-worktree persona when no profile is configured). Can be a "
             "voice name or path to audio file for cloning.",
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
        help="Playback speed multiplier (default: profile/persona speed). Requires ffmpeg.",
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
        help="List available voices, personas, and the active worktree persona",
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
    parser.add_argument(
        "--validate-personas",
        action="store_true",
        help="Check that PERSONA_VOICES and the persona files agree; "
             "exit non-zero on drift. Use after editing either source.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=20,
        metavar="N",
        help="Show the last N call-log entries (default 20) from the "
             "logs/calls.jsonl observability log and exit.",
    )
    parser.add_argument(
        "--tail-desync",
        type=int,
        nargs="?",
        const=20,
        metavar="N",
        help="Show the last N desync events from desync.jsonl. Empty output "
             "is the goal — anything here is a real drift bug.",
    )

    args = parser.parse_args()

    if args.tail is not None:
        _print_tail(_CALL_LOG, args.tail)
        sys.exit(0)
    if args.tail_desync is not None:
        _print_tail(_DESYNC_LOG, args.tail_desync, label="desync")
        sys.exit(0)

    if args.validate_personas:
        result = validate_persona_sync()
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            print(
                f"\nDRIFT detected. Edit the files in {result['persona_dir']} or "
                f"PERSONA_VOICES in speak_when_done/__init__.py until both sources agree.",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(0)

    if args.list_voices:
        result = list_voices()
        print(f"Active worktree: {result['active_worktree']}")
        print(f"Active persona:  {result['active_persona']}")
        print(f"Default voice:   {result['default_voice']}")
        print(f"Playbook source: {result['playbook_source']}")
        print("\nBuilt-in voices:")
        for voice in result["builtin_voices"]:
            print(f"  - {voice['name']}: {voice['description']}")
        print("\nPersona voices:")
        for v in result["persona_voices"]:
            marker = " (active)" if v["name"] == result["active_persona"] else ""
            tagline = v["style"].split("\n", 1)[0]
            print(f"  - {v['name']}{marker}: {tagline[:120]}")
        sync = result["playbook_sync"]
        if not sync["ok"]:
            print(f"\nWARNING - playbook drift: {sync}", file=sys.stderr)
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

    # Resolve profile. A profile is "configured" when the user asked for one
    # explicitly (flag/env) or set a default in voices.yaml; only then does it
    # override the per-worktree persona resolution inside speak().
    profile_name = args.profile or get_default_profile_name()
    profile = get_profile(profile_name)
    profile_configured = bool(
        args.profile
        or os.environ.get("SPEAK_WHEN_DONE_PROFILE")
        or "default" in _load_raw_config()
    )

    if profile_configured and profile:
        voice = args.voice or profile["voice"]
        speed = args.speed if args.speed is not None else profile["speed"]
        warmup = args.warmup if args.warmup is not None else profile["warmup"]
    else:
        # No profile configured: leave voice/speed as None so speak() resolves
        # the per-worktree persona (and its configured speed).
        voice = args.voice
        speed = args.speed
        warmup = args.warmup or ""

    # Handle --profile-json (output resolved settings for scripts).
    # voice=null means "per-worktree persona resolution applies".
    if args.profile_json:
        info = {
            "profile": profile_name if profile_configured else None,
            "voice": voice,
            "speed": speed,
            "warmup": warmup,
            "persona": profile["persona"] if (profile_configured and profile) else "",
        }
        print(json.dumps(info))
        sys.exit(0)

    # Require --text if not listing
    if not args.text:
        parser.error(
            "--text is required unless using --list-voices, --list-profiles, "
            "--validate-personas, --tail, or --tail-desync"
        )

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
