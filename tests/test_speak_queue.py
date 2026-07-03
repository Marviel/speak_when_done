"""Tests for the daemon's async FIFO speak queue.

`speak` must enqueue and return immediately; a single background worker
drains the queue strictly in arrival order (so audio never overlaps),
drops stale messages at dequeue time, and page-touches the model with a
silent keep-warm generation when the queue sits idle.

These tests drive the queue machinery directly (no HTTP server, no TTS
model): swd.speak / _keep_warm are monkeypatched with recording stubs.
"""

import logging
import queue
import threading
import time

import pytest

import speak_when_done as swd
from speak_when_done import daemon


@pytest.fixture()
def fresh_queue(monkeypatch):
    """Isolated queue + stop event so tests never share worker state."""
    q = queue.Queue(maxsize=daemon.QUEUE_MAX)
    monkeypatch.setattr(daemon, "_speak_queue", q)
    monkeypatch.setattr(daemon, "_worker_stop", threading.Event())
    return q


def _stub_speak(record):
    """A swd.speak replacement that records (message, voice, cwd)."""
    def stub(message, voice=None, quiet=False, cwd=None):
        record.append((message, voice, cwd))
        return {"success": True, "persona": "stub"}
    return stub


# ---- enqueue ----------------------------------------------------------------


def test_enqueue_returns_immediately_with_position(fresh_queue):
    r1 = daemon._enqueue_speak("first", cwd="/Users/test/some/worktree", voice=None)
    r2 = daemon._enqueue_speak("second", cwd="/Users/test/some/worktree", voice=None)
    assert r1["success"] is True
    assert r1["queued"] is True
    assert r1["position"] == 1
    assert r2["position"] == 2
    # Identity fields from the old synchronous response are preserved.
    assert r1["persona"] in swd.PERSONA_VOICES
    assert r1["spoken_text"] == "first"
    assert "prompt_sha" in r1
    assert "voice_sha" in r1
    assert isinstance(r1["drift"], list)
    # Nothing was synthesized or played — the items just sit in the queue.
    assert fresh_queue.qsize() == 2


def test_enqueue_rejects_empty_message(fresh_queue):
    r = daemon._enqueue_speak("   ", cwd=None, voice=None)
    assert r["success"] is False
    assert "empty" in r["error"]
    assert fresh_queue.qsize() == 0


def test_enqueue_errors_when_queue_full(monkeypatch):
    monkeypatch.setattr(daemon, "_speak_queue", queue.Queue(maxsize=2))
    cwd = "/Users/test/some/worktree"
    assert daemon._enqueue_speak("a", cwd=cwd, voice=None)["success"] is True
    assert daemon._enqueue_speak("b", cwd=cwd, voice=None)["success"] is True
    r = daemon._enqueue_speak("c", cwd=cwd, voice=None)
    assert r["success"] is False
    assert "full" in r["error"]


def test_enqueue_persona_matches_resolver(fresh_queue):
    """Enqueue-time persona must agree with the library resolver."""
    cwd = "/Users/test/some/worktree"
    persona, voice = swd._resolve_active_persona_and_voice(cwd)
    r = daemon._enqueue_speak("hello", cwd=cwd, voice=None)
    assert r["persona"] == persona
    assert r["voice"] == voice


# ---- worker: FIFO order, staleness, keep-warm --------------------------------


def test_process_item_passes_queued_args_to_speak(fresh_queue, monkeypatch):
    calls = []
    monkeypatch.setattr(swd, "speak", _stub_speak(calls))
    item = daemon._QueuedSpeak("hi there", "/wt", None, time.monotonic())
    daemon._process_item(item)
    assert calls == [("hi there", None, "/wt")]


def test_process_item_drops_stale_message(fresh_queue, monkeypatch, caplog, tmp_path):
    calls = []
    monkeypatch.setattr(swd, "speak", _stub_speak(calls))
    # Keep the drop's JSONL record out of the real logs dir.
    monkeypatch.setattr(swd, "_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(swd, "_CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setattr(swd, "_DESYNC_LOG", str(tmp_path / "desync.jsonl"))

    item = daemon._QueuedSpeak(
        "ancient news", "/wt", None,
        time.monotonic() - daemon.STALE_AFTER_S - 1,
    )
    with caplog.at_level(logging.WARNING, logger="speak_when_done.daemon"):
        daemon._process_item(item)
    assert calls == [], "stale message must not be synthesized or played"
    assert any("stale" in r.getMessage() for r in caplog.records)


def test_worker_plays_in_fifo_order(fresh_queue, monkeypatch):
    order = []
    monkeypatch.setattr(swd, "speak", _stub_speak(order))
    monkeypatch.setattr(daemon, "KEEP_WARM_IDLE_S", 0.05)
    monkeypatch.setattr(daemon, "_keep_warm", lambda: None)

    for msg in ("one", "two", "three"):
        assert daemon._enqueue_speak(msg, cwd="/wt", voice=None)["success"]
    thread = daemon._start_worker()
    deadline = time.monotonic() + 5
    while len(order) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    daemon._worker_stop.set()
    thread.join(timeout=2)
    assert [m for m, _, _ in order] == ["one", "two", "three"]


def test_worker_runs_keep_warm_when_idle(fresh_queue, monkeypatch):
    monkeypatch.setattr(daemon, "KEEP_WARM_IDLE_S", 0.05)
    hits = []
    monkeypatch.setattr(daemon, "_keep_warm", lambda: hits.append(time.monotonic()))

    thread = daemon._start_worker()
    deadline = time.monotonic() + 5
    while not hits and time.monotonic() < deadline:
        time.sleep(0.01)
    daemon._worker_stop.set()
    thread.join(timeout=2)
    assert hits, "idle queue must trigger a keep-warm generation"


def test_worker_survives_processing_exception(fresh_queue, monkeypatch):
    """One bad message must not kill the worker; later messages still play."""
    order = []

    def flaky(message, voice=None, quiet=False, cwd=None):
        if message == "boom":
            raise RuntimeError("synthetic failure")
        order.append(message)
        return {"success": True, "persona": "stub"}

    monkeypatch.setattr(swd, "speak", flaky)
    monkeypatch.setattr(daemon, "KEEP_WARM_IDLE_S", 0.05)
    monkeypatch.setattr(daemon, "_keep_warm", lambda: None)

    daemon._enqueue_speak("boom", cwd="/wt", voice=None)
    daemon._enqueue_speak("after", cwd="/wt", voice=None)
    thread = daemon._start_worker()
    deadline = time.monotonic() + 5
    while "after" not in order and time.monotonic() < deadline:
        time.sleep(0.01)
    daemon._worker_stop.set()
    thread.join(timeout=2)
    assert order == ["after"]


# ---- watchdog ----------------------------------------------------------------


def test_watchdog_warns_when_synthesis_is_slow(monkeypatch, caplog):
    monkeypatch.setattr(daemon, "SYNTH_WATCHDOG_S", 0.05)
    with caplog.at_level(logging.WARNING, logger="speak_when_done.daemon"):
        with daemon._synthesis_watchdog("test-slow"):
            time.sleep(0.15)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("test-slow" in m for m in warnings), warnings


def test_watchdog_silent_when_synthesis_is_fast(monkeypatch, caplog):
    monkeypatch.setattr(daemon, "SYNTH_WATCHDOG_S", 5.0)
    with caplog.at_level(logging.WARNING, logger="speak_when_done.daemon"):
        with daemon._synthesis_watchdog("test-fast"):
            pass
    assert not [r for r in caplog.records if "test-fast" in r.getMessage()]
