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
import http.server
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

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
_gen_lock = threading.Lock()  # serializes generation (the model isn't reentrant)


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
            label,
            threshold,
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
                label,
                elapsed,
                threshold,
            )


def _warm_generate(message: str, voice: str, language: str, output_path: str) -> None:
    """Write a WAV for `message` using the resident model. Registered as swd._GENERATOR."""
    from pocket_tts.data.audio import stream_audio_chunks
    from pocket_tts.default_parameters import MAX_TOKEN_PER_CHUNK

    model = _get_model(language)
    state = _get_state(language, voice)
    with (
        _gen_lock,
        _synthesis_watchdog(f"voice={os.path.basename(voice)} chars={len(message)}"),
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

# This daemon runs for days. An in-process CoreAudio query goes stale in a
# long-lived process (it misses mic on/off transitions and spoke over a live
# meeting on 2026-07-05) — so force the mic check into a fresh subprocess, which
# always reads the current state. See swd.MIC_CHECK_FRESH.
swd.MIC_CHECK_FRESH = True


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
                _speak_queue.qsize(),
                message[:50],
                cwd,
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
            age,
            STALE_AFTER_S,
            item.message[:50],
            item.cwd,
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
            result.get("persona"),
            age,
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
                language,
                time.monotonic() - start,
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


# ---- Control server (browser pause UI) --------------------------------------
# A tiny stdlib HTTP server on its own port serving a one-page control panel and
# a JSON control API, so the user can pause/mute notifications on demand — even
# when NOT in a meeting. Pause state is file-backed in the shared library
# (swd.get_pause_state / swd.set_pause), so the worker's playback-time check and
# this UI always agree. Kept on a SEPARATE port from the MCP endpoint so it
# never interferes with the JSON-RPC transport.

CONTROL_HOST = os.environ.get("SPEAK_WHEN_DONE_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("SPEAK_WHEN_DONE_CONTROL_PORT", "9877"))

# The fresh mic check spawns a subprocess; cache it briefly so an open browser
# tab polling /status doesn't fork one every few seconds.
_mic_cache: dict[str, float | bool] = {"value": False, "at": 0.0}
_MIC_CACHE_TTL_S = 2.0


def _mic_active_cached() -> bool:
    now = time.monotonic()
    if now - float(_mic_cache["at"]) > _MIC_CACHE_TTL_S:
        _mic_cache["value"] = swd.is_microphone_active()
        _mic_cache["at"] = now
    return bool(_mic_cache["value"])


_HISTORY_KINDS = {
    "speak": "spoken",
    "speak_suppressed": "suppressed",
    "speak_dropped_stale": "dropped",
}

# Optional worktree-title resolution: the history logs the worktree *path*, but
# the UI can show the human TBD title ("🔇 Fix Speak Mic…") instead. Resolved via
# `tbd worktree list --json`, cached (titles rarely change), best-effort — if tbd
# is missing or fails, titles are simply absent and the feature no-ops. launchd's
# minimal PATH omits ~/.local/bin, so probe an explicit path too.
_TBD_BIN = shutil.which("tbd") or os.path.expanduser("~/.local/bin/tbd")
_TBD_CACHE_TTL_S = 15.0
_tbd_cache_map: dict[str, str] = {}
_tbd_cache_at: float = 0.0


def _tbd_titles() -> dict[str, str]:
    """{worktree_path: display title} from TBD, cached ~15s; {} if unavailable.

    The daemon already knows each speak's worktree PATH (callers pass it as
    `cwd`), so it resolves the human title itself — no worker or MCP-registration
    change is needed. Best-effort: last-good map is kept on any failure.
    """
    global _tbd_cache_map, _tbd_cache_at
    now = time.monotonic()
    if now - _tbd_cache_at < _TBD_CACHE_TTL_S:
        return _tbd_cache_map
    result = _tbd_cache_map  # fall back to last-good on any failure below
    try:
        proc = subprocess.run(
            [_TBD_BIN, "worktree", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            result = {}
            for wt in json.loads(proc.stdout):
                p = (wt.get("path") or "").rstrip("/")
                title = wt.get("displayName") or wt.get("name")
                if p and title:
                    result[p] = title
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _tbd_cache_map = result
    _tbd_cache_at = now
    return result


def _recent_speaks(limit: int = 40) -> list[dict]:
    """Recent speak-related events, newest first, from the tail of the call log.

    persona / text are OPTIONAL — not every caller sets a persona, and rows
    logged before text capture have no text. The UI renders around whatever is
    present.
    """
    path = swd._CALL_LOG
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 500_000)  # only the tail; the log grows unbounded
            f.seek(size - back)
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if back < size and lines:
        lines = lines[1:]  # drop the partial first line from a mid-file seek
    titles = _tbd_titles()  # {path: title}, cached + best-effort
    out: list[dict] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            r = json.loads(line)
        except ValueError:
            continue
        kind = _HISTORY_KINDS.get(r.get("event"))
        if kind is None:
            continue
        wt = (r.get("worktree") or "").rstrip("/")
        out.append(
            {
                "ts": r.get("ts"),
                "kind": kind,
                "reason": r.get("reason"),
                "persona": r.get("persona"),
                "text": r.get("text"),
                "chars": r.get("message_chars"),
                "worktree": os.path.basename(wt) if wt else None,
                # Human TBD title for the worktree, when resolvable — the UI
                # shows it only if the user enables "Show worktree names".
                "worktree_title": titles.get(wt) if wt else None,
            }
        )
    return out


def _status_dict(*, with_history: bool = True) -> dict:
    state = swd.get_pause_state()
    until = state.get("until")
    until_iso = None
    seconds_left = None
    if until is not None:
        seconds_left = max(0, int(float(until) - time.time()))
        until_iso = time.strftime("%H:%M", time.localtime(float(until)))
    out = {
        "paused": state["paused"],
        "until": until,
        "until_iso": until_iso,
        "seconds_left": seconds_left,
        "reason": state.get("reason"),
        "mic_active": _mic_active_cached(),
        "queue_depth": _speak_queue.qsize(),
    }
    if with_history:
        out["recent"] = _recent_speaks()
    return out


_CONTROL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>speak when done</title>
<style>
  :root{
    --bg:#0e1013; --card:#16191f; --fg:#e7e9ee; --muted:#8b929c; --faint:#5b626c;
    --line:#252a32; --hover:#1d212a; --accent:#6b8cff;
    --ok:#35d29a; --warn:#f2b03d; --stop:#ff6b6b;
  }
  :root[data-theme="dark"]{ color-scheme:dark; }
  @media (prefers-color-scheme:light){
    :root{ --bg:#eef1f5; --card:#fff; --fg:#141822; --muted:#697180; --faint:#9aa2ae;
           --line:#e6e9ef; --hover:#f5f7fa; --accent:#3b6bff; }
  }
  :root[data-theme="light"]{ --bg:#eef1f5; --card:#fff; --fg:#141822; --muted:#697180;
    --faint:#9aa2ae; --line:#e6e9ef; --hover:#f5f7fa; --accent:#3b6bff; color-scheme:light; }
  *{ box-sizing:border-box; }
  body{ margin:0; min-height:100vh; background:var(--bg); color:var(--fg);
        font:14.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
        display:flex; align-items:center; justify-content:center; padding:4vh 4vw;
        -webkit-font-smoothing:antialiased; }
  /* Desktop: a wide two-pane card — controls on the left, a tall history feed
     on the right. Collapses to a single narrow column on real phones. */
  .card{ width:100%; max-width:1000px; height:min(82vh,760px); background:var(--card);
         border:1px solid var(--line); border-radius:22px; overflow:hidden;
         box-shadow:0 18px 60px rgba(0,0,0,.20);
         display:grid; grid-template-columns:340px 1fr; }

  .controls{ padding:32px 30px; display:flex; flex-direction:column;
             border-right:1px solid var(--line); }
  header{ display:flex; align-items:center; justify-content:space-between; margin-bottom:30px; }
  .brand{ font-size:14px; font-weight:600; letter-spacing:.3px; color:var(--muted); }
  .qd{ font-size:11.5px; color:var(--faint); }

  .hero{ display:flex; align-items:center; gap:15px; margin-bottom:26px; }
  .dot{ width:14px; height:14px; border-radius:50%; flex:0 0 auto; background:var(--faint);
        transition:background .25s, box-shadow .25s; }
  .is-on   .dot{ background:var(--ok);   box-shadow:0 0 0 5px color-mix(in srgb,var(--ok) 18%,transparent); }
  .is-mic  .dot{ background:var(--warn); box-shadow:0 0 0 5px color-mix(in srgb,var(--warn) 18%,transparent); }
  .is-mute .dot{ background:var(--stop); box-shadow:0 0 0 5px color-mix(in srgb,var(--stop) 18%,transparent); }
  .state{ font-size:28px; font-weight:650; letter-spacing:-.5px; line-height:1.1; }
  .statesub{ font-size:13px; color:var(--muted); margin-top:3px; }

  .toggle{ width:100%; font:inherit; font-weight:600; font-size:15.5px; padding:15px;
           border-radius:13px; cursor:pointer; border:1px solid var(--line);
           background:var(--hover); color:var(--fg); transition:transform .05s, filter .15s; }
  .toggle:hover{ filter:brightness(1.06); }
  .toggle:active{ transform:scale(.985); }
  .toggle.resume{ background:var(--ok); border-color:var(--ok); color:#042a1c; }
  .chips{ display:flex; gap:9px; margin-top:12px; }
  .chips button{ flex:1; font:inherit; font-size:13px; font-weight:550; color:var(--muted);
                 padding:10px; border-radius:10px; cursor:pointer; background:transparent;
                 border:1px solid var(--line); transition:background .15s, color .15s; }
  .chips button:hover{ background:var(--hover); color:var(--fg); }

  /* "Show worktree names" toggle, pinned to the bottom of the controls pane. */
  .opt{ margin-top:auto; padding-top:22px; display:flex; align-items:center; gap:9px;
        font-size:12.5px; color:var(--muted); cursor:pointer; user-select:none; }
  .opt input{ accent-color:var(--accent); width:15px; height:15px; cursor:pointer; margin:0; }

  .feed{ display:flex; flex-direction:column; min-height:0; padding:30px 14px 10px 30px; }
  .rule{ display:flex; align-items:center; gap:12px; margin:0 16px 4px 0; }
  .rule span{ font-size:11.5px; font-weight:600; letter-spacing:.6px; text-transform:uppercase; color:var(--faint); }
  .rule:after{ content:""; height:1px; background:var(--line); flex:1; }

  .hist{ list-style:none; margin:0; padding:0 16px 0 0; flex:1; overflow-y:auto; }
  .hist::-webkit-scrollbar{ width:9px; }
  .hist::-webkit-scrollbar-thumb{ background:var(--line); border-radius:8px; }
  .hitem{ display:grid; grid-template-columns:auto 1fr auto; align-items:start; gap:13px;
          padding:12px 8px; margin-right:-6px; border-bottom:1px solid var(--line);
          border-radius:8px; }
  .hitem:last-child{ border-bottom:0; }
  .hitem.long{ cursor:pointer; }
  .hitem.long:hover{ background:var(--hover); }
  .hic{ font-size:14px; line-height:1.5; opacity:.9; }
  .hbody{ min-width:0; }
  /* Messages wrap and show in full; only a genuinely long one is clamped to 3
     lines (with a caret) and expands on click. Most are one line — nothing cut. */
  .htext{ font-size:14.5px; word-break:break-word;
          display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3; overflow:hidden; }
  .hitem.expanded .htext{ -webkit-line-clamp:unset; }
  .hitem.long .htext{ position:relative; }
  .hitem.long .htext::after{ content:" ▾"; color:var(--faint); font-size:12px; }
  .hitem.long.expanded .htext::after{ content:" ▴"; }
  .k-mute .htext,.k-drop .htext{ color:var(--muted); }
  .hmeta{ font-size:12px; color:var(--faint); margin-top:3px; }
  .hwt{ font-size:12px; color:var(--muted); font-weight:500; margin-top:2px; }
  .htime{ font-size:12px; color:var(--faint); white-space:nowrap; line-height:1.5; }
  .empty{ text-align:center; color:var(--faint); font-size:13px; padding:34px 0; }

  @media (max-width:720px){
    body{ align-items:flex-start; padding:20px 14px; }
    .card{ grid-template-columns:1fr; height:auto; max-width:440px; }
    .controls{ border-right:0; border-bottom:1px solid var(--line); padding:24px; }
    header{ margin-bottom:20px; }
    .hero{ margin-bottom:18px; }
    .opt{ margin-top:20px; padding-top:0; }
    .state{ font-size:23px; }
    .feed{ padding:22px 8px 8px 22px; }
    .hist{ max-height:48vh; }
  }
</style>
</head>
<body>
  <div class="card">
    <aside class="controls">
      <header>
        <span class="brand">speak when done</span>
        <span class="qd" id="qd"></span>
      </header>

      <div class="hero">
        <span class="dot" id="dot"></span>
        <div>
          <div class="state" id="state">…</div>
          <div class="statesub" id="statesub"></div>
        </div>
      </div>

      <button class="toggle" id="toggle" onclick="toggleMute()"></button>
      <div class="chips" id="chips">
        <button onclick="pause(15)">15 min</button>
        <button onclick="pause(30)">30 min</button>
        <button onclick="pause(60)">1 hour</button>
      </div>

      <label class="opt"><input type="checkbox" id="wtToggle"> Show worktree names</label>
    </aside>

    <section class="feed">
      <div class="rule"><span>Recent</span></div>
      <ul class="hist" id="hist"></ul>
    </section>
  </div>
<script>
  let cur = {};
  const KIND = { spoken:'\\u{1F50A}', suppressed:'\\u{1F507}', dropped:'\\u23F1' };

  function ago(iso){
    const t = Date.parse(iso||''); if(isNaN(t)) return '';
    let s = Math.max(0,(Date.now()-t)/1000);
    if(s<45) return 'just now';
    const m=s/60; if(m<60) return Math.round(Math.max(1,m))+'m';
    const h=m/60; if(h<24) return Math.round(h)+'h';
    return Math.round(h/24)+'d';
  }
  function label(it){
    if(it.kind==='suppressed') return it.reason==='paused' ? 'muted' : 'mic';
    if(it.kind==='dropped') return 'stale';
    return '';
  }
  const wtToggle = document.getElementById('wtToggle');
  const expanded = new Set();   // row keys the user has expanded — survive refreshes
  let lastHistKey = '';         // skip re-rendering the feed when nothing changed

  function renderHist(items){
    const ul = document.getElementById('hist'); ul.innerHTML='';
    if(!items || !items.length){
      const li=document.createElement('li'); li.className='empty';
      li.textContent='No notifications yet.'; ul.appendChild(li); return;
    }
    const showWt = wtToggle.checked;
    for(const it of items){
      const key = (it.ts||'') + '|' + (it.text||'');
      const li=document.createElement('li'); li.className='hitem k-'+it.kind.slice(0,4);
      const ic=document.createElement('span'); ic.className='hic'; ic.textContent=KIND[it.kind]||KIND.spoken;
      const body=document.createElement('div'); body.className='hbody';
      const txt=document.createElement('div'); txt.className='htext';
      txt.textContent = it.text || (it.chars ? '('+it.chars+' chars)' : '(no text logged)');
      body.appendChild(txt);
      const bits=[]; const l=label(it);
      if(l) bits.push(l);
      if(it.persona) bits.push(it.persona);      // persona is optional
      if(bits.length){ const m=document.createElement('div'); m.className='hmeta';
        m.textContent=bits.join(' \\u00B7 '); body.appendChild(m); }
      if(showWt && it.worktree_title){ const w=document.createElement('div'); w.className='hwt';
        w.textContent=it.worktree_title; body.appendChild(w); }
      const tm=document.createElement('span'); tm.className='htime'; tm.textContent=ago(it.ts);
      li.appendChild(ic); li.appendChild(body); li.appendChild(tm);
      ul.appendChild(li);
      // Only rows whose text actually overflows the 3-line clamp become
      // click-to-expand (measured now that the element is laid out).
      if(expanded.has(key)) li.classList.add('expanded');
      if(txt.scrollHeight > txt.clientHeight + 1 || expanded.has(key)){
        li.classList.add('long');
        li.addEventListener('click', () => {
          if(expanded.has(key)){ expanded.delete(key); li.classList.remove('expanded'); }
          else { expanded.add(key); li.classList.add('expanded'); }
        });
      }
    }
  }
  async function refresh(){
    try{
      const s = await (await fetch('/status',{cache:'no-store'})).json();
      cur = s;
      document.body.className = s.paused ? 'is-mute' : (s.mic_active ? 'is-mic' : 'is-on');
      const st=document.getElementById('state'), sub=document.getElementById('statesub');
      const tg=document.getElementById('toggle'), chips=document.getElementById('chips');
      if(s.paused){
        st.textContent='Muted';
        sub.textContent = s.seconds_left==null ? 'until you resume'
          : Math.ceil(s.seconds_left/60)+' min left \\u00B7 resumes '+(s.until_iso||'');
        tg.textContent='Resume speaking'; tg.className='toggle resume'; chips.style.display='none';
      } else if(s.mic_active){
        st.textContent='Quiet'; sub.textContent='mic in use \\u2014 auto-paused';
        tg.textContent='Mute anyway'; tg.className='toggle'; chips.style.display='flex';
      } else {
        st.textContent='On'; sub.textContent='notifications will play';
        tg.textContent='Mute'; tg.className='toggle'; chips.style.display='flex';
      }
      document.getElementById('qd').textContent = s.queue_depth ? (s.queue_depth+' queued') : '';
      // Rebuild the feed only when its data (or the toggle) changed — otherwise
      // the 4s poll would reset scroll position and collapse expanded rows.
      const hk = JSON.stringify((s.recent||[]).map(r=>[r.ts,r.kind,r.reason,r.worktree_title]))
                 + '|' + wtToggle.checked;
      if(hk !== lastHistKey){ lastHistKey = hk; renderHist(s.recent); }
    }catch(e){ document.getElementById('state').textContent='daemon unreachable'; }
  }
  wtToggle.checked = localStorage.getItem('swd_show_worktree')==='1';
  wtToggle.addEventListener('change', ()=>{
    localStorage.setItem('swd_show_worktree', wtToggle.checked?'1':'0');
    lastHistKey=''; if(cur.recent) renderHist(cur.recent);
  });
  function toggleMute(){ cur.paused ? resume() : pause(0); }
  async function pause(min){ await fetch('/pause?minutes='+min,{method:'POST'}); refresh(); }
  async function resume(){ await fetch('/resume',{method:'POST'}); refresh(); }
  refresh(); setInterval(refresh, 4000);
</script>
</body>
</html>
"""


class _ControlHandler(http.server.BaseHTTPRequestHandler):
    # Quiet the default per-request stderr spam; route to the daemon logger.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("control %s - %s", self.address_string(), format % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _send_json(self, obj: dict) -> None:
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._send(200, _CONTROL_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/status":
            self._send_json(_status_dict())
        elif path == "/history":
            self._send_json({"recent": _recent_speaks()})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        if path == "/pause":
            minutes = 0.0
            with contextlib.suppress(ValueError, IndexError):
                minutes = float(params.get("minutes", ["0"])[0])
            duration_s = minutes * 60 if minutes > 0 else None
            state = swd.set_pause(True, duration_s=duration_s, reason="control-ui")
            logger.info(
                "paused via control UI (%s)",
                f"{minutes:.0f} min" if duration_s else "until resumed",
            )
            self._send_json({**_status_dict(), **{"ok": True, "state": state}})
        elif path == "/resume":
            swd.set_pause(False)
            logger.info("resumed via control UI")
            self._send_json({**_status_dict(), "ok": True})
        else:
            self._send(404, b"not found", "text/plain")


def _start_control_server() -> None:
    """Serve the pause UI + control API in a background daemon thread."""
    try:
        httpd = http.server.ThreadingHTTPServer(
            (CONTROL_HOST, CONTROL_PORT), _ControlHandler
        )
    except OSError as e:
        logger.warning(
            "control server not started (port %d unavailable): %s", CONTROL_PORT, e
        )
        return
    thread = threading.Thread(
        target=httpd.serve_forever, name="speak-control", daemon=True
    )
    thread.start()
    logger.info(
        "control UI at http://%s:%d/ (pause/resume notifications)",
        CONTROL_HOST,
        CONTROL_PORT,
    )


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
            result.get("position"),
            result.get("persona"),
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
    _start_control_server()
    logger.info(
        "speak_when_done daemon serving MCP at http://%s:%d/mcp "
        "(queue_max=%d, stale_after=%.0fs, keep_warm_idle=%.0fs, watchdog=%.0fs)",
        HOST,
        PORT,
        QUEUE_MAX,
        STALE_AFTER_S,
        KEEP_WARM_IDLE_S,
        SYNTH_WATCHDOG_S,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
