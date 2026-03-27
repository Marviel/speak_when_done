"""
Voice profile management for speak_when_done.

Loads voice profiles from a YAML config file. Each profile specifies:
- voice: path to audio file or built-in voice name
- speed: playback speed multiplier (default 1.0)
- persona: LLM prompt persona for message generation
- warmup: text prepended before the message for voice cloning warmup (default "... ...")
"""

import os
import sys
from pathlib import Path
from typing import Any

# Default config locations (checked in order)
CONFIG_LOCATIONS = [
    Path.home() / ".config" / "speak_when_done" / "voices.yaml",
    Path.home() / ".speak_when_done" / "voices.yaml",
]

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "alba": {
        "voice": "alba",
        "speed": 1.0,
        "persona": "",
        "warmup": "",
    },
}


def _find_config() -> Path | None:
    """Find the first existing config file."""
    # Check env override first
    env_path = os.environ.get("SPEAK_WHEN_DONE_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    for loc in CONFIG_LOCATIONS:
        if loc.exists():
            return loc
    return None


def load_profiles() -> dict[str, dict[str, Any]]:
    """Load voice profiles from config, merged with defaults."""
    config_path = _find_config()
    if config_path is None:
        return DEFAULT_PROFILES.copy()

    import yaml

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        return DEFAULT_PROFILES.copy()

    profiles = DEFAULT_PROFILES.copy()
    voice_profiles = raw.get("voices", {})
    if not isinstance(voice_profiles, dict):
        return profiles

    for name, cfg in voice_profiles.items():
        if not isinstance(cfg, dict):
            continue
        try:
            speed = float(cfg.get("speed", 1.0))
        except (ValueError, TypeError):
            print(f"Warning: invalid speed for profile '{name}', using 1.0", file=sys.stderr)
            speed = 1.0
        profiles[name] = {
            "voice": cfg.get("voice", "alba"),
            "speed": speed,
            "persona": cfg.get("persona", ""),
            "warmup": cfg.get("warmup", ""),
        }

    return profiles


def get_profile(name: str) -> dict[str, Any] | None:
    """Get a single voice profile by name."""
    profiles = load_profiles()
    return profiles.get(name)


def _load_raw_config() -> dict:
    """Load the raw config dict."""
    config_path = _find_config()
    if config_path is None:
        return {}
    try:
        import yaml
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        return raw if isinstance(raw, dict) else {}
    except ImportError:
        return {}
    except Exception as e:
        print(f"Warning: failed to load config {config_path}: {e}", file=sys.stderr)
        return {}


def get_default_profile_name() -> str:
    """Get the default profile name from config or env."""
    env_default = os.environ.get("SPEAK_WHEN_DONE_PROFILE")
    if env_default:
        return env_default

    raw = _load_raw_config()
    return raw.get("default", "alba")


def agent_can_choose_voice() -> bool:
    """Check if the agent is allowed to pick its own voice profile."""
    env_val = os.environ.get("SPEAK_WHEN_DONE_AGENT_CAN_CHOOSE")
    if env_val is not None:
        return env_val.lower() in ("1", "true", "yes")

    raw = _load_raw_config()
    return bool(raw.get("agent_can_choose", False))
