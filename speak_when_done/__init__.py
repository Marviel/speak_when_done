"""
speak_when_done - Text-to-speech with automatic temp file handling.

Generates speech using Kyutai's Pocket TTS, plays it, and cleans up.

Persona registers live in the repo-level ``personas/`` directory (one file per
persona, plus ``_common.md`` for shared discipline/TTS rules). This module
reads those files at runtime so the spoken register and the voice that plays
it can never drift apart. Run ``speak_when_done --validate-personas`` (or
import ``validate_persona_sync``) to detect drift.
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
import socket
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

    Builtin-name voices (used by personas without a cloned .safetensors) must
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

# Where cloned persona voices (.safetensors) live. Machine-local by design;
# personas whose file is missing fall back to the "alba" builtin voice.
_VOICES_DIR = os.environ.get(
    "SPEAK_WHEN_DONE_VOICES_DIR", os.path.expanduser("~/.claude/voices")
)

# Persona registers live in individual files under the repo-level personas/
# directory (one per persona, plus _common.md for shared discipline/TTS rules).
# Editing those files is reflected in `list_voices()` immediately. When the
# package is installed as a wheel (no personas/ next to it), playbook loading
# degrades gracefully to the inline taglines below. PERSONA_VOICES only stores
# audio plumbing (path/speed/language) plus a one-line fallback tagline used
# when a persona file is missing or unreadable.
_PERSONA_DIR = os.environ.get("SPEAK_WHEN_DONE_PERSONA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "personas"
)

PERSONA_VOICES = {
    "attenborough": {
        # Clone exported against the older model; language pins it.
        "path": os.path.join(_VOICES_DIR, "attenborough.safetensors"),
        "language": "english_2026-01",
        "speed": 1.0,
        "tagline": "Sir David Attenborough, nature documentary. Hushed, unhurried, awed.",
    },
    "herzog": {
        "path": os.path.join(_VOICES_DIR, "herzog.safetensors"),
        "speed": 0.95,
        "tagline": "Werner Herzog, fatalist monologue. Indifferent universe; long cascading sentences.",
    },
    "mcconaughey": {
        "path": os.path.join(_VOICES_DIR, "mcconaughey.safetensors"),
        "speed": 0.92,
        "tagline": "Matthew McConaughey, Lincoln-ad drawl. Unhurried, warm, fragments and pauses.",
    },
    "aghdashloo": {
        "path": os.path.join(_VOICES_DIR, "aghdashloo.safetensors"),
        "speed": 0.94,
        "tagline": "Shohreh Aghdashloo as Avasarala. Gravelly verdicts; staccato, no hedging.",
    },
    "elliott": {
        "path": os.path.join(_VOICES_DIR, "elliott.safetensors"),
        "speed": 0.95,
        "tagline": "Sam Elliott, slow Western drawl. Weatherier than McConaughey; rural, low.",
    },
    "gottfried": {
        "path": os.path.join(_VOICES_DIR, "gottfried.safetensors"),
        "speed": 1.0,
        "tagline": "Gilbert Gottfried / Iago. Nasal kvetch, escalating outrage, rhetorical questions.",
    },
    # These three personas use Pocket TTS PREDEFINED voices (bare names, no
    # .safetensors on disk) — see _is_builtin_voice. Voices chosen by register
    # fit + measured f0 against pocket-tts 2.1.0's actual predefined set.
    "flightdeck": {
        "path": "anna",
        "speed": 1.1,
        "tagline": "Over-caffeinated mission-control flight director. Crisp go/no-go callouts; anomalies are the good part.",
    },
    "gumshoe": {
        "path": "vera",
        "speed": 0.98,
        "tagline": "Noir private eye. Every bug is a case; dry past-tense case-file closes, one custom simile.",
    },
    "expediter": {
        "path": "paul",
        "speed": 1.12,
        "tagline": "Michelin-kitchen expediter running the pass. Fire, plate, eighty-six; taste-before-you-toss.",
    },
}


# ---- Observability ---------------------------------------------------------
# Every speak()/list_voices() call appends a JSONL record to the logs/
# directory next to personas/. Drift events get a separate desync.jsonl so
# `tail -f` on it surfaces real problems without log noise.

_LOG_DIR = os.environ.get("SPEAK_WHEN_DONE_LOG_DIR") or os.path.join(
    os.path.dirname(_PERSONA_DIR), "logs"
)
_CALL_LOG = os.path.join(_LOG_DIR, "calls.jsonl")
_DESYNC_LOG = os.path.join(_LOG_DIR, "desync.jsonl")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _text_sha(text: str) -> str:
    """SHA-256 of a string, truncated to 16 hex chars for log brevity."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _file_sha(path: str) -> str | None:
    """SHA-256 of a file (16 hex chars) or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def _log_event(event: str, *, drift: list[str] | None = None, **fields) -> None:
    """Append a JSONL record. Best-effort; never raises."""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        record = {"ts": _now_iso(), "event": event, **fields}
        if drift:
            record["drift"] = drift
        line = json.dumps(record, default=str) + "\n"
        with open(_CALL_LOG, "a") as f:
            f.write(line)
        if drift:
            with open(_DESYNC_LOG, "a") as f:
                f.write(line)
    except Exception:
        pass


# ---- Pause / mute state -----------------------------------------------------
# A file-backed pause switch so speech can be silenced ON DEMAND — independent
# of the meeting/microphone check. The control UI (daemon) writes it; speak()
# reads it at playback time. File-backed (not in-memory) so any process — the
# daemon worker, the CLI, a curl to the control server — sees the same state.
_STATE_DIR = os.environ.get("SPEAK_WHEN_DONE_STATE_DIR") or os.path.join(
    os.path.dirname(_PERSONA_DIR), "state"
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


def _check_drift(persona: str, voice: str, playbooks: dict[str, str]) -> list[str]:
    """Return human-readable drift issues; empty list when clean.

    Catches: persona key not in PERSONA_VOICES, voice path that contradicts
    the persona's expected safetensors, missing voice file, missing or
    malformed persona file, section that's been trimmed below useful
    length. The validator runs at PARSE time (offline); this runs at CALL
    time so a stale long-running MCP server still surfaces drift.
    """
    issues: list[str] = []
    cfg = PERSONA_VOICES.get(persona)
    if cfg is None:
        return [f"persona {persona!r} missing from PERSONA_VOICES"]

    expected_path = cfg["path"]
    if voice.endswith(".safetensors") and voice != expected_path:
        # A safetensors voice that isn't the persona's own file means the
        # composed register and the timbre will not line up.
        issues.append(
            f"voice {voice!r} does not match {persona}.path {expected_path!r}"
        )
    if (
        voice == expected_path
        and not _is_builtin_voice(expected_path)
        and not os.path.exists(expected_path)
    ):
        # Builtin voice NAMES never exist on disk — pocket-tts resolves them
        # to hosted embeddings, so only file-path voices get this check.
        issues.append(f"voice file missing on disk: {expected_path}")

    section = playbooks.get(persona, "")
    if not section:
        issues.append(
            f"no persona file at {os.path.join(_PERSONA_DIR, persona + '.md')}"
        )
    else:
        first_line = section.split("\n", 1)[0]
        if not first_line.lower().startswith(f"## {persona} "):
            issues.append(f"section header malformed: {first_line[:80]!r}")
        if len(section) < 400:
            issues.append(f"section thin ({len(section)} chars; need >= 400)")
    return issues


def _load_persona_playbooks(persona_dir: str = _PERSONA_DIR) -> dict[str, str]:
    """Read individual persona files and return ``{persona_name: full_text}``.

    Each persona has its own file at ``<persona_dir>/<name>.md``. Only files
    whose stem matches a key in ``PERSONA_VOICES`` are returned.

    Returns ``{}`` on any read/parse error so the caller can fall back to
    inline taglines without crashing.
    """
    out: dict[str, str] = {}
    for name in PERSONA_VOICES:
        path = os.path.join(persona_dir, f"{name}.md")
        try:
            with open(path, encoding="utf-8") as f:
                section = f.read().rstrip()
            section = re.sub(r"\n+-{3,}\s*$", "", section)
            out[name] = section
        except OSError:
            pass
    return out


def validate_persona_sync(persona_dir: str = _PERSONA_DIR) -> dict:
    """Detect drift between PERSONA_VOICES and the persona files.

    Returns a dict with ``ok``, ``persona_dir``, ``missing_in_playbook``,
    ``missing_in_code``, and ``thin_sections`` (sections shorter than a
    sensible minimum, suggesting they were not actually fleshed out).
    """
    playbooks = _load_persona_playbooks(persona_dir)
    file_keys = set(playbooks.keys())
    code_keys = set(PERSONA_VOICES.keys())
    thin = sorted(n for n, body in playbooks.items() if len(body) < 400)
    return {
        "ok": file_keys == code_keys and not thin,
        "persona_dir": persona_dir,
        "missing_in_playbook": sorted(code_keys - file_keys),
        "missing_in_code": sorted(file_keys - code_keys),
        "thin_sections": thin,
    }


# Personas that existed before the 2026-07-02 roster expansion. Worktrees
# already active by then were pinned (personas/assignments.json) to the
# assignment this frozen list produces, because the hash below is modular over
# the roster size — adding personas would otherwise reshuffle EVERY existing
# worktree's voice. Never edit this tuple; if the roster grows again, re-seed
# pins for all then-known worktrees first (over the then-current roster).
_LEGACY_PERSONAS = (
    "aghdashloo",
    "attenborough",
    "elliott",
    "gottfried",
    "herzog",
    "mcconaughey",
)

# Per-worktree persona pins: {"pins": {worktree_path: persona_name}}. Read on
# every call (like the persona files), so edits apply without a restart. Also
# the manual override point — pin any worktree to any persona by editing it.
# The file is machine-local state and gitignored; see
# personas/assignments.example.json for the format. Missing file == no pins.
_ASSIGNMENTS_PATH = os.path.join(_PERSONA_DIR, "assignments.json")


def _legacy_persona_for_worktree(cwd: str) -> str:
    """Assignment under the pre-2026-07-02 six-persona roster (pin seeding)."""
    names = sorted(_LEGACY_PERSONAS)
    h = hashlib.sha256(cwd.encode()).digest()
    return names[h[0] % len(names)]


def _load_persona_pins() -> dict[str, str]:
    """Read persona pins from assignments.json. Best-effort; {} on any error.

    Pins naming a persona that no longer exists are ignored (falls through to
    the hash) rather than crashing resolution.
    """
    try:
        with open(_ASSIGNMENTS_PATH, encoding="utf-8") as f:
            pins = json.load(f).get("pins", {})
        return {wt: p for wt, p in pins.items() if p in PERSONA_VOICES}
    except (OSError, ValueError):
        return {}


def _pick_persona_for_worktree(cwd: str | None) -> str:
    """Deterministic persona selection.

    Order of precedence:
    1. An explicit pin in personas/assignments.json (protects existing
       worktrees' assignments across roster expansions, and doubles as the
       manual-override mechanism).
    2. SHA-256 of the worktree path over the FULL current roster (so each new
       worktree gets a stable persona across sessions, and new personas are
       reachable).

    When cwd discovery fails, fall back to a hash of (hostname, ppid) instead
    of alphabetical-first — otherwise every cwd-discovery failure collapses to
    the same persona and the distribution skews hard. Failures are logged so
    they're visible.
    """
    names = sorted(PERSONA_VOICES.keys())
    if not cwd:
        seed = f"{socket.gethostname()}:{os.getppid()}".encode()
        h = hashlib.sha256(seed).digest()
        _log_event(
            "cwd_discovery_failed",
            persona_fallback=names[h[0] % len(names)],
            ppid=os.getppid(),
        )
        return names[h[0] % len(names)]
    pinned = _load_persona_pins().get(cwd)
    if pinned is not None:
        return pinned
    h = hashlib.sha256(cwd.encode()).digest()
    return names[h[0] % len(names)]


def _discover_caller_cwd() -> str | None:
    """Walk up from this process to find the calling agent session's worktree.

    An MCP server typically runs with its CWD inside the install directory,
    so we look at ancestor processes and skip any CWD inside ``~/.claude``.
    """
    home_claude = os.path.expanduser("~/.claude")
    pid = os.getppid()
    for _ in range(10):
        if not pid or pid == 1:
            break
        try:
            r = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            cwd = None
            for line in r.stdout.splitlines():
                if line.startswith("n/"):
                    cwd = line[1:]
                    break
            if cwd and not cwd.startswith(home_claude) and cwd != "/":
                return cwd
            pr = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pid = int(pr.stdout.strip() or 0) or None
        except Exception:
            break
    return None


# Env values that should be treated as "auto" (fall through to the persona
# hash) rather than as a literal voice override.
_STALE_VOICE_ENVS = {
    "auto",
    "",
}


def _resolve_active_persona_and_voice(cwd: str | None = None) -> tuple[str, str]:
    """Pick the persona NAME + voice PATH together so they cannot drift.

    - SPEAK_WHEN_DONE_VOICE pointing at a known persona path locks both.
    - SPEAK_WHEN_DONE_VOICE pointing at a built-in voice or unknown safetensors
      keeps the worktree-hash persona but uses the requested voice.
    - No env override: persona = SHA-256 hash of worktree, voice = its path.
    """
    if cwd is None:
        cwd = _discover_caller_cwd()

    env = os.environ.get("SPEAK_WHEN_DONE_VOICE", "")
    if env and env not in _STALE_VOICE_ENVS:
        # If env points at one of our persona safetensors, lock persona to match.
        for name, cfg in PERSONA_VOICES.items():
            if cfg["path"] == env:
                return name, env
        # Env points at something else (built-in voice or unknown path):
        # honour the voice, but pick the persona from the worktree hash so the
        # message register at least matches the deterministic worktree style.
        if not env.endswith(".safetensors") or os.path.exists(env):
            return _pick_persona_for_worktree(cwd), env

    persona = _pick_persona_for_worktree(cwd)
    voice_path = PERSONA_VOICES[persona]["path"]
    # Builtin voice NAMES (flightdeck/gumshoe/expediter) never exist on disk;
    # only file-path voices fall back to "alba" when their file is missing.
    if not _is_builtin_voice(voice_path) and not os.path.exists(voice_path):
        return persona, "alba"
    return persona, voice_path


# Module-level snapshot taken at import time. ``list_voices()`` and ``speak()``
# both re-resolve at call time, so changing persona files / env vars at runtime is
# picked up without a restart.
# The fresh-subprocess mic check (see MIC_CHECK_FRESH) re-imports this module
# only to call _microphone_active_native(); it sets this env var so import skips
# the process-tree walk in _discover_caller_cwd() (a few hundred ms + a
# cwd_discovery_failed log event we don't want on every playback).
ACTIVE_WORKTREE = (
    None
    if os.environ.get("SPEAK_WHEN_DONE_NO_CWD_DISCOVERY")
    else _discover_caller_cwd()
)
ACTIVE_PERSONA, DEFAULT_VOICE = _resolve_active_persona_and_voice(ACTIVE_WORKTREE)


def list_voices(cwd: str | None = None) -> dict:
    """List voices and reveal the persona assigned to this worktree.

    The persona's full register (who they are, how the work bends them, what
    to avoid) is sourced from individual files in the personas/ directory and
    returned in ``active_style`` (prefixed with shared discipline/TTS rules
    from ``_common.md``). Inactive personas only get a one-line tagline.

    Also returns ``prompt_sha`` (SHA of the active persona's file
    content) and ``voice_sha`` (SHA of its safetensors). Compare these
    across sessions to detect silent drift — when either changes without
    the other, the prompt and timbre have decoupled.
    """
    worktree = cwd if cwd is not None else ACTIVE_WORKTREE
    persona, voice = _resolve_active_persona_and_voice(worktree)
    playbooks = _load_persona_playbooks()
    sync = validate_persona_sync()

    persona_voices = []
    for name, cfg in PERSONA_VOICES.items():
        # Active persona gets the full playbook; others get tagline only.
        if name == persona and name in playbooks:
            style = playbooks[name]
        else:
            style = cfg.get("tagline", f"(see {_PERSONA_DIR}/{name}.md)")
        persona_voices.append(
            {
                "name": name,
                "style": style,
                "path": cfg["path"],
                "speed": cfg.get("speed", 1.0),
                "language": cfg.get("language", DEFAULT_LANGUAGE),
            }
        )

    active_style_parts = []
    common_path = os.path.join(_PERSONA_DIR, "_common.md")
    try:
        with open(common_path, encoding="utf-8") as f:
            active_style_parts.append(f.read().rstrip())
    except OSError:
        pass
    persona_style = playbooks.get(persona) or PERSONA_VOICES.get(persona, {}).get(
        "tagline"
    )
    if persona_style:
        active_style_parts.append(persona_style)
    active_style = "\n\n".join(active_style_parts) if active_style_parts else None
    prompt_sha = _text_sha(playbooks.get(persona, ""))
    voice_sha = _file_sha(voice) if voice and os.path.isabs(voice) else None
    drift = _check_drift(persona, voice, playbooks)

    _log_event(
        "list_voices",
        persona=persona,
        voice=voice,
        worktree=worktree,
        prompt_sha=prompt_sha,
        voice_sha=voice_sha,
        sync_ok=sync["ok"],
        drift=drift or None,
    )

    return {
        "success": True,
        "builtin_voices": BUILTIN_VOICES,
        "persona_voices": persona_voices,
        "active_worktree": worktree,
        "active_persona": persona,
        "active_style": active_style,
        "default_voice": voice,
        "playbook_source": _PERSONA_DIR,
        "playbook_sync": sync,
        "prompt_sha": prompt_sha,
        "voice_sha": voice_sha,
        "drift": drift,
        "custom_voice_hint": (
            f"Compose your spoken message in 'active_style' above. "
            f"The full register lives in {_PERSONA_DIR}/{persona}.md — "
            f"edit that file to update the persona; this tool re-reads it on every call."
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
    cwd: str | None = None,
) -> dict:
    """
    Speak a message aloud using Pocket TTS.

    Handles temp file creation and cleanup automatically.
    Supports macOS, Linux, and Windows.

    Args:
        message: The message to speak aloud.
        voice: Voice to use (default: resolved from worktree persona). Can be a
               built-in voice name, a path to a safetensors voice clone, or a
               path to an audio file (exported + cached automatically).
        quiet: If True, suppress pocket-tts output.
        speed: Playback speed multiplier. When None (default), the active
               persona's configured speed applies if its own voice is playing;
               pass an explicit value (e.g. from a voice profile) to override.
        warmup: Text prepended to the message for voice cloning warmup
                (e.g. "... ..."). Never included in the returned spoken_text.
        cwd: The calling session's worktree, used to resolve the per-worktree
             persona. When None, falls back to the import-time discovered cwd.

    Returns:
        Dictionary with success status and details.
    """
    # Resolve persona+voice together so the speed/language modifiers always
    # match the persona that composed the message. If the caller passed an
    # explicit voice, use it but still look up the matching persona by path.
    worktree = cwd if cwd is not None else ACTIVE_WORKTREE
    persona, default_voice = _resolve_active_persona_and_voice(worktree)
    if voice:
        # If the explicit voice happens to be one of our persona paths, switch
        # the active persona snapshot too so speed/language line up.
        for name, cfg in PERSONA_VOICES.items():
            if cfg["path"] == voice:
                persona = name
                break
    else:
        voice = default_voice

    # On-demand pause takes precedence over (and is independent of) the meeting
    # check — this is how the user silences notifications when NOT in a meeting.
    if is_paused():
        _log_event(
            "speak_suppressed",
            reason="paused",
            persona=persona,
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
            persona=persona,
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

        persona_cfg = PERSONA_VOICES.get(persona, {})
        # Only apply the persona's language/speed overrides when actually
        # playing the persona's own voice (not a caller-picked builtin voice).
        is_persona_voice = persona_cfg.get("path") == voice
        language = (
            persona_cfg.get("language", DEFAULT_LANGUAGE)
            if is_persona_voice
            else DEFAULT_LANGUAGE
        )

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

        # Effective playback speed: an explicit `speed` argument wins;
        # otherwise the persona's configured speed applies when its own
        # voice is playing. atempo preserves pitch; a missing ffmpeg or a
        # failed stretch just plays the original.
        if speed is None:
            speed = persona_cfg.get("speed", 1.0) if is_persona_voice else 1.0
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

            playbooks = _load_persona_playbooks()
            prompt_sha = _text_sha(playbooks.get(persona, ""))
            voice_sha = _file_sha(voice) if voice and os.path.isabs(voice) else None
            drift = _check_drift(persona, voice, playbooks)
            _log_event(
                "speak",
                persona=persona,
                voice=voice,
                worktree=worktree,
                prompt_sha=prompt_sha,
                voice_sha=voice_sha,
                message_chars=len(message),
                text=message,
                drift=drift or None,
            )
            return {
                "success": True,
                "message": "Notification spoken to user",
                "spoken_text": message,
                "persona": persona,
                "voice": voice,
                "prompt_sha": prompt_sha,
                "voice_sha": voice_sha,
                "drift": drift,
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
