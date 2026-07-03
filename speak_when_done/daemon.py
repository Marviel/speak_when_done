"""
Shared warm TTS daemon for speak_when_done.

ONE process (run as a LaunchAgent) keeps the Pocket TTS model and all persona
voice states resident in memory, and exposes the `speak` / `list_voices` tools
over a streamable-HTTP MCP endpoint at http://HOST:PORT/mcp.

Every Claude Code session connects to this single endpoint instead of spawning
its own stdio MCP server, so there are ZERO per-session speak_when_done
processes (the previous design spawned a `uv run` wrapper + a python server per
session, and re-loaded the TTS model cold on every notification).

`speak` is asynchronous: requests are validated, enqueued, and answered
immediately (queued=true, position=N); ONE background worker drains the FIFO —
synthesize, play to completion, next — so audio never overlaps and no MCP
request ever blocks on the (potentially slow) TTS model. Stale messages are
dropped at dequeue time, an idle keep-warm generation stops the model weights
from being paged out, and a watchdog logs a WARNING when a single synthesis
runs long (the memory-pressure signature). Tuning knobs: QUEUE_MAX,
STALE_AFTER_S, KEEP_WARM_IDLE_S, SYNTH_WATCHDOG_S below.

Persona selection still happens per worktree: the calling agent passes its
`cwd`, and the deterministic worktree-hash assignment in the shared library
(speak_when_done.__init__) maps it to a persona + voice. Generation, the
per-persona speed stretch, the cross-session playback lock, mic-suppression and
drift logging are all reused unchanged from the library via the `_GENERATOR`
hook — the daemon only swaps the synthesis backend from a cold `uvx` subprocess
to the resident model.
"""

import contextlib
import dataclasses
import logging
import os
import queue
import sys
import tempfile
import threading
import time

import speak_when_done as swd
from speak_when_done import DEFAULT_LANGUAGE, PERSONA_VOICES

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("speak_when_done.daemon")

HOST = os.environ.get("SPEAK_WHEN_DONE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPEAK_WHEN_DONE_PORT", "9876"))

# ---- Queue tuning (env-overridable) -----------------------------------------
# `speak` enqueues and returns immediately; ONE background worker synthesizes
# and plays strictly in arrival order, so audio never overlaps and worktrees
# wait their turn. This replaced synchronous in-request synthesis after a
# paged-out model made one generation take 12.5 minutes, stacking every other
# MCP call behind the _gen_lock until the clients timed out (~60s) and the
# daemon hit anyio.ClosedResourceError answering disconnected sockets.
QUEUE_MAX = int(os.environ.get("SPEAK_WHEN_DONE_QUEUE_MAX", "20"))
# Messages older than this at DEQUEUE time are dropped (and logged) instead of
# playing ancient notifications after a long stall.
STALE_AFTER_S = float(os.environ.get("SPEAK_WHEN_DONE_STALE_AFTER_S", "600"))
# After this much queue idleness the worker does a tiny throwaway generation
# to page-touch the resident model weights so they don't get fully evicted.
KEEP_WARM_IDLE_S = float(os.environ.get("SPEAK_WHEN_DONE_KEEP_WARM_IDLE_S", "600"))
# A single synthesis exceeding this logs a WARNING — the signature of the
# memory-pressure mode above.
SYNTH_WATCHDOG_S = float(os.environ.get("SPEAK_WHEN_DONE_SYNTH_WATCHDOG_S", "45"))
KEEP_WARM_TEXT = "Okay."

# Resident state. Models are keyed by language (one TTSModel per language); voice
# states are keyed by (language, voice) so each persona's safetensors prompt is
# computed once and reused for every subsequent notification.
_models: dict[str, object] = {}
_states: dict[tuple[str, str], object] = {}
_model_lock = threading.Lock()  # guards model construction
_gen_lock = threading.Lock()    # serializes generation (the model isn't reentrant)


def _get_model(language: str):
    """Load (once) and return the resident TTSModel for a language."""
    with _model_lock:
        model = _models.get(language)
        if model is None:
            logger.info("Loading Pocket TTS model (language=%s)...", language)
            from pocket_tts.models.tts_model import TTSModel

            # eos_threshold=-2 matches the legacy `--eos-threshold -2` flag; it's
            # a load-time parameter for the model.
            model = TTSModel.load_model(language=language, eos_threshold=-2.0)
            model.to("cpu")
            _models[language] = model
            logger.info("Model ready (language=%s)", language)
        return model


def _get_state(language: str, voice: str):
    """Compute (once) and return the conditioning state for a voice."""
    key = (language, voice)
    state = _states.get(key)
    if state is None:
        model = _get_model(language)
        with _gen_lock:
            state = model.get_state_for_audio_prompt(voice)
        _states[key] = state
        logger.info("Voice state cached: %s (language=%s)", voice, language)
    return state


@contextlib.contextmanager
def _synthesis_watchdog(label: str):
    """Warn when one synthesis exceeds SYNTH_WATCHDOG_S.

    A healthy warm generation takes single-digit seconds; anything past the
    threshold means the model weights were paged out under memory pressure.
    The timer fires mid-flight (so the log self-diagnoses while the synthesis
    is still grinding), and the exit path logs the final duration too.
    """
    threshold = SYNTH_WATCHDOG_S

    def _bark() -> None:
        logger.warning(
            "synthesis (%s) exceeding %.0fs — model weights likely paged out "
            "under memory pressure; audio will play when it finishes",
            label, threshold,
        )

    timer = threading.Timer(threshold, _bark)
    timer.daemon = True
    start = time.monotonic()
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        elapsed = time.monotonic() - start
        if elapsed >= threshold:
            logger.warning(
                "synthesis (%s) took %.1fs (threshold %.0fs)",
                label, elapsed, threshold,
            )


def _warm_generate(message: str, voice: str, language: str, output_path: str) -> None:
    """Write a WAV for `message` using the resident model. Registered as swd._GENERATOR."""
    from pocket_tts.data.audio import stream_audio_chunks
    from pocket_tts.default_parameters import MAX_TOKEN_PER_CHUNK

    model = _get_model(language)
    state = _get_state(language, voice)
    with _gen_lock, _synthesis_watchdog(
        f"voice={os.path.basename(voice)} chars={len(message)}"
    ):
        chunks = model.generate_audio_stream(
            model_state=state,
            text_to_generate=message,
            frames_after_eos=0,
            max_tokens=MAX_TOKEN_PER_CHUNK,
        )
        stream_audio_chunks(output_path, chunks, model.config.mimi.sample_rate)


# Route swd.speak() synthesis through the resident model instead of `uvx`.
swd._GENERATOR = _warm_generate


# ---- Async FIFO speak queue -------------------------------------------------


@dataclasses.dataclass
class _QueuedSpeak:
    message: str
    cwd: str | None
    voice: str | None
    enqueued_at: float  # time.monotonic()


_speak_queue: "queue.Queue[_QueuedSpeak]" = queue.Queue(maxsize=QUEUE_MAX)
_enqueue_lock = threading.Lock()  # makes the reported queue position exact
_worker_stop = threading.Event()


def _enqueue_speak(message: str, cwd: str | None, voice: str | None) -> dict:
    """Validate + enqueue a speak request; never blocks on synthesis.

    Returns the same identity fields the synchronous path used to
    (persona/voice/prompt_sha/voice_sha/drift, all cheap to compute) plus
    ``queued=True`` and the 1-based ``position`` in the queue.
    """
    if not message or not message.strip():
        return {"success": False, "error": "message is empty"}

    # Resolve persona + voice now (cheap: hash + a few file reads) so the
    # caller learns which register applies without waiting for synthesis.
    persona, resolved_voice = swd._resolve_active_persona_and_voice(cwd)
    if voice:
        for name, cfg in PERSONA_VOICES.items():
            if cfg["path"] == voice:
                persona = name
                break
        resolved_voice = voice
    playbooks = swd._load_persona_playbooks()
    prompt_sha = swd._text_sha(playbooks.get(persona, ""))
    voice_sha = (
        swd._file_sha(resolved_voice)
        if resolved_voice and os.path.isabs(resolved_voice)
        else None
    )
    drift = swd._check_drift(persona, resolved_voice, playbooks)

    item = _QueuedSpeak(
        message=message, cwd=cwd, voice=voice, enqueued_at=time.monotonic()
    )
    with _enqueue_lock:
        try:
            _speak_queue.put_nowait(item)
        except queue.Full:
            logger.error(
                "speak queue full (%d pending); rejecting: %s... (cwd=%s)",
                _speak_queue.qsize(), message[:50], cwd,
            )
            return {
                "success": False,
                "error": (
                    f"speak queue is full ({QUEUE_MAX} messages pending); "
                    "the daemon is backed up — try again later"
                ),
            }
        position = _speak_queue.qsize()

    return {
        "success": True,
        "queued": True,
        "position": position,
        "message": (
            f"Notification queued at position {position}; "
            "messages play in arrival order"
        ),
        "spoken_text": message,
        "persona": persona,
        "voice": resolved_voice,
        "prompt_sha": prompt_sha,
        "voice_sha": voice_sha,
        "drift": drift,
    }


def _process_item(item: _QueuedSpeak) -> None:
    """Synthesize + play one queued message (drop it when stale)."""
    age = time.monotonic() - item.enqueued_at
    if age > STALE_AFTER_S:
        logger.warning(
            "dropping stale queued message (age %.0fs > %.0fs cutoff): %s... (cwd=%s)",
            age, STALE_AFTER_S, item.message[:50], item.cwd,
        )
        swd._log_event(
            "speak_dropped_stale",
            age_s=round(age),
            message_chars=len(item.message),
            worktree=item.cwd,
        )
        return
    result = swd.speak(item.message, voice=item.voice, quiet=True, cwd=item.cwd)
    if result.get("success"):
        logger.info(
            "spoken (persona=%s, waited %.1fs in queue)",
            result.get("persona"), age,
        )
    elif result.get("suppressed"):
        logger.info("suppressed: %s", result.get("reason"))
    else:
        logger.error("speak failed: %s", result.get("error"))


def _keep_warm() -> None:
    """Page-touch every resident model with a tiny throwaway generation.

    Runs after KEEP_WARM_IDLE_S of queue idleness so the model weights don't
    get fully evicted between notifications (the root cause of the 12.5-minute
    synthesis). The WAV is discarded — keep-alives are never played.
    """
    for language in list(_models):
        voice = next((v for (lang, v) in list(_states) if lang == language), None)
        if voice is None:
            continue
        out = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                out = tmp.name
            start = time.monotonic()
            _warm_generate(KEEP_WARM_TEXT, voice, language, out)
            logger.info(
                "keep-warm generation ok (language=%s, %.1fs)",
                language, time.monotonic() - start,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("keep-warm generation failed (language=%s): %s", language, e)
        finally:
            if out:
                with contextlib.suppress(OSError):
                    os.unlink(out)


def _worker_loop() -> None:
    """Single consumer: strict FIFO, synthesize then play to completion.

    One worker means audio never overlaps (the same guarantee _gen_lock +
    the cross-process playback flock gave the synchronous design) and
    worktrees wait their turn in arrival order.
    """
    while not _worker_stop.is_set():
        try:
            item = _speak_queue.get(timeout=KEEP_WARM_IDLE_S)
        except queue.Empty:
            _keep_warm()
            continue
        try:
            _process_item(item)
        except Exception as e:  # noqa: BLE001
            logger.error("worker error processing queued message: %s", e)
        finally:
            _speak_queue.task_done()


def _start_worker() -> threading.Thread:
    thread = threading.Thread(target=_worker_loop, name="speak-worker", daemon=True)
    thread.start()
    return thread


# ---- MCP surface (streamable HTTP) -----------------------------------------

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    "speak_when_done",
    host=HOST,
    port=PORT,
    # Each notification is a self-contained request; no session affinity needed.
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def speak(message: str, cwd: str | None = None, voice: str | None = None) -> dict:
    """
    Speak a message aloud to notify the user.

    Use this tool ONLY when you need to get the user's attention after
    a long-running task has completed. Do not use for routine responses.

    Good examples:
    - "Your build has completed successfully"
    - "The test suite finished with 3 failures"
    - "Deployment is complete"
    - "I found the bug you were looking for"

    Args:
        message: The message to speak aloud. Keep it brief and informative.
        cwd: Your current working directory (the session's worktree). Pass this
             so the correct per-worktree persona and voice are selected. If
             omitted, a stable fallback persona is used.
        voice: Optional override; a built-in voice name or a path to a
               safetensors voice clone. Normally leave unset.

    Returns:
        Dictionary with success status and details. Returns IMMEDIATELY once
        the message is accepted: the daemon queues it (queued=true, position=N)
        and a background worker synthesizes + plays messages strictly in
        arrival order, so audio from different sessions never overlaps.
        Microphone suppression is evaluated at playback time, not enqueue time.
    """
    logger.info("speak: %s... (cwd=%s)", message[:50], cwd)
    result = _enqueue_speak(message, cwd=cwd, voice=voice)
    if result.get("success"):
        logger.info(
            "queued at position %s (persona=%s)",
            result.get("position"), result.get("persona"),
        )
    else:
        logger.error("enqueue failed: %s", result.get("error"))
    return result


@mcp.tool()
def list_voices(cwd: str | None = None) -> dict:
    """
    List available voices and reveal the persona assigned to this worktree.

    Pass `cwd` (your current working directory) so the active persona and its
    full register are resolved for the right worktree.

    Returns built-in voices, persona voices, and the active persona's style.
    """
    return swd.list_voices(cwd=cwd)


def _preload() -> None:
    """Warm the default-language model and its persona voice states at boot.

    Only the default language is eagerly loaded; rarer languages (e.g.
    attenborough's english_2026-01) load lazily on first use to avoid holding a
    second full model in memory unless it's actually needed.
    """
    try:
        default_personas = [
            (name, cfg)
            for name, cfg in PERSONA_VOICES.items()
            if cfg.get("language", DEFAULT_LANGUAGE) == DEFAULT_LANGUAGE
            # Builtin voice NAMES (no file on disk) resolve to hosted
            # embeddings; file-path voices must exist locally.
            and (swd._is_builtin_voice(cfg["path"]) or os.path.exists(cfg["path"]))
        ]
        if not default_personas:
            return
        _get_model(DEFAULT_LANGUAGE)
        for name, cfg in default_personas:
            try:
                _get_state(DEFAULT_LANGUAGE, cfg["path"])
            except Exception as e:  # noqa: BLE001
                logger.warning("preload voice %s failed: %s", name, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("preload skipped: %s", e)


def main() -> None:
    _preload()
    _start_worker()
    logger.info(
        "speak_when_done daemon serving MCP at http://%s:%d/mcp "
        "(queue_max=%d, stale_after=%.0fs, keep_warm_idle=%.0fs, watchdog=%.0fs)",
        HOST, PORT, QUEUE_MAX, STALE_AFTER_S, KEEP_WARM_IDLE_S, SYNTH_WATCHDOG_S,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
