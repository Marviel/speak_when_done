"""
speak_when_done - Text-to-speech with automatic temp file handling.

Generates speech using Kyutai's Pocket TTS, plays it, and cleans up.

Also ships mic-aware suppression, an on-demand pause switch, JSONL
observability, and a cross-process playback lock (see daemon.py for the
shared warm-daemon mode that builds on these).
"""

import ctypes
import ctypes.util
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

__version__ = "0.1.0"

# Pocket TTS version used for the cold (subprocess) synthesis path. Must stay
# in step with the `pocket-tts` requirement in pyproject.toml's `daemon` extra:
# a voice state / safetensors export is only valid for the model it came from.
_POCKET_TTS_SPEC = "pocket-tts@2.1.0"

# Built-in (predefined) voices in Pocket TTS 2.x. This list must stay a subset
# of pocket_tts._ORIGINS_OF_PREDEFINED_VOICES — any bare name outside that dict
# is treated by pocket-tts as a file path and fails at synthesis (the previous
# list here — sandra, jessica, luca, ... — matched no voice in the installed
# build and none of those names worked). All 26 verified + f0-measured
# 2026-07-02 against pocket-tts 2.1.0.
BUILTIN_VOICES = [
    {"name": "alba", "description": "Default female voice"},
    {"name": "anna", "description": "Female voice, bright (~216 Hz, VCTK p228)"},
    {"name": "vera", "description": "Female voice, lower (~176 Hz, VCTK p229)"},
    {"name": "fantine", "description": "Female voice (~207 Hz, VCTK p244)"},
    {"name": "eponine", "description": "Female voice (VCTK p262)"},
    {"name": "azelma", "description": "Female voice (VCTK p303)"},
    {"name": "mary", "description": "Female voice (VCTK p333)"},
    {"name": "jane", "description": "Female voice (VCTK p339)"},
    {"name": "eve", "description": "Female voice, bright (~211 Hz, VCTK p361)"},
    {"name": "cosette", "description": "Female voice (Expresso, animated)"},
    {"name": "charles", "description": "Male voice, very deep (~75 Hz, VCTK p254)"},
    {"name": "paul", "description": "Male voice (~122 Hz, VCTK p259)"},
    {"name": "george", "description": "Male voice (~121 Hz, VCTK p315)"},
    {"name": "michael", "description": "Male voice, deep (~93 Hz, VCTK p360)"},
    {"name": "jean", "description": "Voice (EARS p010)"},
    {"name": "marius", "description": "Voice donation"},
    {"name": "javert", "description": "Voice donation"},
    {"name": "bill_boerst", "description": "Male narrator (voice-zero)"},
    {"name": "peter_yearsley", "description": "Male narrator (voice-zero)"},
    {"name": "stuart_bell", "description": "Male narrator (voice-zero)"},
    {"name": "caro_davy", "description": "Narrator (voice-zero)"},
    {"name": "giovanni", "description": "Male voice (Italian default)"},
    {"name": "lola", "description": "Female voice (Spanish default)"},
    {"name": "juergen", "description": "Male voice (German default)"},
    {"name": "rafael", "description": "Male voice (Portuguese default)"},
    {"name": "estelle", "description": "Female voice (French default)"},
]

_BUILTIN_VOICE_NAMES = frozenset(v["name"] for v in BUILTIN_VOICES)


def _is_builtin_voice(voice: str) -> bool:
    """True when `voice` is a Pocket TTS predefined voice NAME, not a file path.

    Builtin-name voices must
    skip every on-disk existence check: pocket-tts resolves them internally to
    hosted per-language voice embeddings.
    """
    return voice in _BUILTIN_VOICE_NAMES


# When True, is_microphone_active() runs the CoreAudio query in a fresh
# subprocess instead of in-process.
#
# WHY: a long-lived process that queries CoreAudio device properties but never
# runs a CoreAudio run loop accumulates a STALE HAL cache — it misses some mic
# on/off transitions and reports the wrong state. The shared daemon (running for
# days) spoke over a live meeting on 2026-07-05 for exactly this reason: the
# per-notification fresh process the pre-2026-07-02 design used always read the
# mic correctly; the resident daemon that replaced it does not. A brand-new
# process builds a fresh HAL client, so its reading is always current.
#
# The daemon sets this True at import; short-lived CLI/library callers leave it
# False — they are already fresh, so the extra subprocess would only add latency.
MIC_CHECK_FRESH = False


def is_microphone_active() -> bool:
    """
    Check if any microphone is currently in use on macOS.

    Uses CoreAudio API to query all audio input devices. Returns False on
    non-macOS platforms. When MIC_CHECK_FRESH is set, delegates to a fresh
    subprocess to dodge the stale-HAL-cache bug documented on that flag.
    """
    if sys.platform != "darwin":
        return False
    if MIC_CHECK_FRESH:
        fresh = _microphone_active_subprocess()
        if fresh is not None:
            return fresh
        # Subprocess failed for some reason — fall back to the in-process query
        # rather than silently un-suppressing (returning a hard False).
    return _microphone_active_native()


def _microphone_active_subprocess() -> bool | None:
    """Run the CoreAudio mic query in a fresh interpreter; None on failure.

    A brand-new process has no stale HAL cache, so its reading is always
    current. Reuses _microphone_active_native() (single source of truth for the
    CoreAudio logic) and sets SPEAK_WHEN_DONE_NO_CWD_DISCOVERY so the re-import
    skips the process-tree walk. Returns None (not False) on any failure so the
    caller can fall back rather than treating a crash as "mic is off".
    """
    env = {**os.environ, "SPEAK_WHEN_DONE_NO_CWD_DISCOVERY": "1"}
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import speak_when_done as s; "
                "print(1 if s._microphone_active_native() else 0)",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
    except (subprocess.SubprocessError, OSError):
        return None
    out = proc.stdout.strip()
    if proc.returncode != 0 or out not in ("0", "1"):
        return None
    return out == "1"


def _microphone_active_native() -> bool:
    """CoreAudio query in the CURRENT process (may be stale in a long-lived
    daemon — see MIC_CHECK_FRESH)."""
    if sys.platform != "darwin":
        return False

    try:
        ca = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
    except OSError:
        return False

    class AudioObjectPropertyAddress(ctypes.Structure):
        _fields_ = [
            ("mSelector", ctypes.c_uint32),
            ("mScope", ctypes.c_uint32),
            ("mElement", ctypes.c_uint32),
        ]

    AUDIO_OBJECT_SYSTEM_OBJECT = 1
    SCOPE_GLOBAL = 0x676C6F62  # 'glob'
    SCOPE_INPUT = 0x696E7074  # 'inpt'
    PROP_DEVICES = 0x64657623  # 'dev#'
    PROP_STREAMS = 0x73746D23  # 'stm#'
    PROP_RUNNING_SOMEWHERE = 0x676F6E65  # 'gone'

    # Get all audio device IDs
    addr = AudioObjectPropertyAddress(PROP_DEVICES, SCOPE_GLOBAL, 0)
    size = ctypes.c_uint32(0)
    err = ca.AudioObjectGetPropertyDataSize(
        AUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)
    )
    if err != 0 or size.value == 0:
        return False

    num_devices = size.value // 4
    devices = (ctypes.c_uint32 * num_devices)()
    err = ca.AudioObjectGetPropertyData(
        AUDIO_OBJECT_SYSTEM_OBJECT,
        ctypes.byref(addr),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(devices),
    )
    if err != 0:
        return False

    # Check each device for active input
    for i in range(num_devices):
        dev = devices[i]

        # Does this device have input streams?
        stream_addr = AudioObjectPropertyAddress(PROP_STREAMS, SCOPE_INPUT, 0)
        stream_size = ctypes.c_uint32(0)
        err = ca.AudioObjectGetPropertyDataSize(
            dev, ctypes.byref(stream_addr), 0, None, ctypes.byref(stream_size)
        )
        if err != 0 or stream_size.value == 0:
            continue

        # Is any process using this input device?
        run_addr = AudioObjectPropertyAddress(PROP_RUNNING_SOMEWHERE, SCOPE_GLOBAL, 0)
        is_running = ctypes.c_uint32(0)
        run_size = ctypes.c_uint32(4)
        err = ca.AudioObjectGetPropertyData(
            dev,
            ctypes.byref(run_addr),
            0,
            None,
            ctypes.byref(run_size),
            ctypes.byref(is_running),
        )
        if err == 0 and is_running.value == 1:
            return True

    return False


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
        # Windows: use PowerShell with -File or script block
        # Path is passed as a separate argument to avoid injection
        return [
            "powershell",
            "-NoProfile",
            "-Command",
            "[System.Media.SoundPlayer]::new($args[0]).PlaySync()",
            "-args",
        ]
    else:
        # Linux/BSD: try common audio players in order of preference
        linux_players = [
            ["paplay"],  # PulseAudio (most common on modern Linux)
            ["aplay"],  # ALSA (fallback)
            ["ffplay", "-nodisp", "-autoexit"],  # FFmpeg (if installed)
        ]
        for player in linux_players:
            if shutil.which(player[0]):
                return player

    return None


def _play_audio(player_cmd: list[str], audio_path: str, timeout: int = 120) -> dict:
    """
    Play an audio file using the specified player command.

    Args:
        player_cmd: The audio player command.
        audio_path: Path to the audio file to play.
        timeout: Maximum time to wait for playback in seconds.

    Returns:
        Dictionary with success status and any error details.
    """
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


VOICE_CACHE_DIR = Path.home() / ".cache" / "speak_when_done" / "voices"


def _get_cached_voice(voice_path: str) -> str:
    """Return path to a cached safetensors file for a voice audio file.

    Computes SHA256 of the source file, checks for a cached export,
    and generates one via pocket-tts export-voice if missing or stale.
    Returns the original path if it's already a .safetensors file or if
    caching fails.
    """
    # Already a safetensors file — no caching needed
    if voice_path.endswith(".safetensors"):
        return voice_path

    # Not a local file (built-in voice name or URL) — skip caching
    if not os.path.isfile(voice_path):
        return voice_path

    try:
        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Hash the source file
        h = hashlib.sha256()
        with open(voice_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        source_hash = h.hexdigest()[:16]

        stem = Path(voice_path).stem
        cached = VOICE_CACHE_DIR / f"{stem}_{source_hash}.safetensors"

        if cached.exists():
            return str(cached)

        # Export voice to safetensors atomically (temp file then rename)
        tmp_cached = (
            VOICE_CACHE_DIR / f".tmp_{stem}_{source_hash}_{os.getpid()}.safetensors"
        )
        result = subprocess.run(
            [
                "uvx",
                _POCKET_TTS_SPEC,
                "export-voice",
                voice_path,
                str(tmp_cached),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and tmp_cached.exists():
            tmp_cached.rename(cached)
            return str(cached)

        # Clean up failed temp file
        try:
            tmp_cached.unlink(missing_ok=True)
        except OSError:
            pass
        return voice_path
    except Exception:
        return voice_path


def _apply_speed(audio_path: str, speed: float) -> str | None:
    """Speed up/slow down a WAV file using ffmpeg. Returns path to new file or None on failure.

    ffmpeg's atempo filter supports 0.5-2.0 per stage. For values outside
    that range, we chain multiple atempo filters.
    """
    if speed == 1.0:
        return None

    if speed <= 0:
        return None

    # Clamp to reasonable range
    speed = max(0.5, min(speed, 4.0))

    # Build atempo filter chain — each stage handles 0.5-2.0x
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining}")
    filter_chain = ",".join(filters)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        fast_path = tmp.name

    result = subprocess.run(
        ["ffmpeg", "-i", audio_path, "-filter:a", filter_chain, "-y", fast_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        try:
            os.unlink(fast_path)
        except OSError:
            pass
        return None
    return fast_path


# A voice .safetensors is tied to the pocket-tts model version it was exported
# from; override here if a local voice clone was generated against an older model.
DEFAULT_LANGUAGE = os.environ.get("SPEAK_WHEN_DONE_LANGUAGE", "english_2026-04")

# Default voice for callers that do not pass one: env override, else the
# stock "alba" builtin. Explicit `voice=` arguments and voice profiles
# (see voices.py) take precedence over this.
DEFAULT_VOICE = os.environ.get("SPEAK_WHEN_DONE_VOICE") or "alba"


# ---- Observability ---------------------------------------------------------
# Every speak()/list_voices() call appends a JSONL record so suppressed or
# failed notifications are diagnosable after the fact.

_LOG_DIR = os.environ.get("SPEAK_WHEN_DONE_LOG_DIR") or os.path.expanduser(
    "~/.cache/speak_when_done/logs"
)
_CALL_LOG = os.path.join(_LOG_DIR, "calls.jsonl")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _log_event(event: str, **fields) -> None:
    """Append a JSONL record. Best-effort; never raises."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        record = {"ts": _now_iso(), "event": event, **fields}
        with open(_CALL_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + chr(10))
    except Exception:
        pass


# ---- Pause / mute state -----------------------------------------------------
# A file-backed pause switch so speech can be silenced ON DEMAND — independent
# of the meeting/microphone check. The control UI (daemon) writes it; speak()
# reads it at playback time. File-backed (not in-memory) so any process — the
# daemon worker, the CLI, a curl to the control server — sees the same state.
_STATE_DIR = os.environ.get("SPEAK_WHEN_DONE_STATE_DIR") or os.path.expanduser(
    "~/.cache/speak_when_done/state"
)
_PAUSE_FILE = os.path.join(_STATE_DIR, "pause.json")


def get_pause_state() -> dict:
    """Return the current pause state, honoring expiry.

    Shape: {"paused": bool, "until": float | None, "reason": str | None}.
    A pause whose "until" epoch has passed reads as not paused (auto-resume),
    so "pause for 30 minutes" needs no timer. Never raises.
    """
    not_paused = {"paused": False, "until": None, "reason": None}
    try:
        with open(_PAUSE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return not_paused
    if not data.get("paused"):
        return not_paused
    until = data.get("until")
    if until is not None:
        try:
            if time.time() >= float(until):
                return not_paused
        except (TypeError, ValueError):
            until = None
    return {"paused": True, "until": until, "reason": data.get("reason")}


def is_paused() -> bool:
    """True if speech is currently paused on demand (expiry-aware)."""
    return get_pause_state()["paused"]


def set_pause(
    paused: bool,
    *,
    duration_s: float | None = None,
    reason: str | None = None,
) -> dict:
    """Persist the pause switch. ``duration_s`` sets an auto-resume horizon
    (None = pause until explicitly resumed). Returns the resulting state."""
    if paused:
        until = time.time() + duration_s if duration_s else None
        data = {"paused": True, "until": until, "reason": reason}
    else:
        data = {"paused": False, "until": None, "reason": None}
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _PAUSE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _PAUSE_FILE)
    except OSError:
        pass
    _log_event("pause_set", paused=paused, until=data.get("until"), reason=reason)
    return get_pause_state()




def list_voices() -> dict:
    """List available voices and the current default."""
    _log_event("list_voices", voice=DEFAULT_VOICE)
    return {
        "success": True,
        "builtin_voices": BUILTIN_VOICES,
        "default_voice": DEFAULT_VOICE,
        "custom_voice_hint": (
            "Pass voice=<builtin name>, a path to a .safetensors voice clone, "
            "or a path to an audio file (exported and cached automatically)."
        ),
    }


# Lock file for serializing playback across multiple instances
_PLAYBACK_LOCK_PATH = os.path.expanduser("~/.claude/speak_when_done.lock")

# Optional warm-synthesis hook. When the shared daemon (speak_when_done.daemon)
# is running this process, it registers a callable here that writes a WAV using
# the resident Pocket TTS model. When None (e.g. the legacy stdio server), speak()
# falls back to spawning `uvx pocket-tts generate` per call.
#   _GENERATOR(message: str, voice: str, language: str, output_path: str) -> None
_GENERATOR = None


def speak(
    message: str,
    voice: str | None = None,
    quiet: bool = False,
    suppress_in_meeting: bool = True,
    speed: float | None = None,
    warmup: str = "",
) -> dict:
    """
    Speak a message aloud using Pocket TTS.

    Handles temp file creation and cleanup automatically.
    Supports macOS, Linux, and Windows.

    Args:
        message: The message to speak aloud.
        voice: Voice to use (default: SPEAK_WHEN_DONE_VOICE or "alba"). Can be a
               built-in voice name, a path to a safetensors voice clone, or a
               path to an audio file (exported + cached automatically).
        quiet: If True, suppress pocket-tts output.
        speed: Playback speed multiplier (default 1.0). Pass an explicit value
               (e.g. from a voice profile) to override.
        warmup: Text prepended to the message for voice cloning warmup
                (e.g. "... ..."). Never included in the returned spoken_text.

    Returns:
        Dictionary with success status and details.
    """
    voice = voice or DEFAULT_VOICE

    # On-demand pause takes precedence over (and is independent of) the meeting
    # check — this is how the user silences notifications when NOT in a meeting.
    if is_paused():
        _log_event(
            "speak_suppressed",
            reason="paused",
            voice=voice,
            text=message,
        )
        return {
            "success": False,
            "suppressed": True,
            "reason": "paused",
        }

    if suppress_in_meeting and sys.platform == "darwin" and is_microphone_active():
        _log_event(
            "speak_suppressed",
            reason="microphone in use",
            voice=voice,
            text=message,
        )
        return {
            "success": False,
            "suppressed": True,
            "reason": "microphone in use",
        }

    player_cmd = _get_audio_player()
    if player_cmd is None:
        return {
            "success": False,
            "error": f"No audio player found for platform '{sys.platform}'. "
            "Install one of: afplay (macOS), paplay/aplay (Linux), "
            "or ensure PowerShell is available (Windows).",
        }

    output_path = None
    fast_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            output_path = tmp.name

        language = DEFAULT_LANGUAGE

        # Prepend warmup text if provided (voice-cloning warmup)
        tts_text = f"{warmup} {message}" if warmup else message

        # Audio-file voices get exported + cached as safetensors once;
        # builtin names and .safetensors paths pass through unchanged.
        resolved_voice = _get_cached_voice(voice)

        if _GENERATOR is not None:
            # Warm path: the shared daemon synthesizes with the resident model.
            # Same EOS tuning is baked into the model at load time (eos_threshold=-2)
            # and frames_after_eos=0 is passed per generate, matching the CLI below.
            try:
                _GENERATOR(tts_text, resolved_voice, language, output_path)
            except Exception as e:
                return {
                    "success": False,
                    "error": f"TTS generation failed: {e}",
                }
        else:
            cmd = [
                "uvx",
                _POCKET_TTS_SPEC,
                "generate",
                "--text",
                tts_text,
                "--voice",
                resolved_voice,
                "--language",
                language,
                # Tighter EOS detection + no trailing frames prevents the "trails off
                # weirdly" artifact at sentence end.
                "--eos-threshold",
                "-2",
                "--frames-after-eos",
                "0",
                "--output-path",
                output_path,
            ]
            if quiet:
                cmd.append("--quiet")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"TTS generation failed: {result.stderr}",
                }

        # atempo preserves pitch; a missing ffmpeg or a failed stretch just
        # plays the original.
        if speed is None:
            speed = 1.0
        play_path = output_path
        if speed != 1.0:
            try:
                fast_path = _apply_speed(output_path, speed)
            except Exception:
                fast_path = None
            if fast_path:
                play_path = fast_path

        # Serialize playback so concurrent speak() calls don't talk over each other.
        # TTS generation above is unlocked so it can run concurrently.
        lock_fd = open(_PLAYBACK_LOCK_PATH, "a")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            play_result = _play_audio(player_cmd, play_path)
            if not play_result["success"]:
                return play_result

            _log_event(
                "speak",
                voice=voice,
                message_chars=len(message),
                text=message,
            )
            return {
                "success": True,
                "message": "Notification spoken to user",
                "spoken_text": message,
                "voice": voice,
            }
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

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
        # Always clean up temp files
        for path in (output_path, fast_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
