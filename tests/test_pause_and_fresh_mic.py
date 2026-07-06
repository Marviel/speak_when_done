"""Tests for the on-demand pause switch and the fresh-subprocess mic check.

Both cover the 2026-07-05 regression: the long-lived daemon's in-process
CoreAudio read went stale and it spoke over a live meeting. The fix routes the
daemon's mic check through a fresh subprocess (MIC_CHECK_FRESH) and adds a
file-backed pause switch so speech can be silenced on demand.
"""

import json
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_pause_state():
    """Every test starts and ends un-paused (state is a real file on disk)."""
    import speak_when_done as swd

    swd.set_pause(False)
    yield
    swd.set_pause(False)


# ---- Pause switch -----------------------------------------------------------


def test_pause_roundtrip():
    import speak_when_done as swd

    assert swd.is_paused() is False
    swd.set_pause(True, reason="test")
    assert swd.is_paused() is True
    state = swd.get_pause_state()
    assert state["paused"] is True
    assert state["reason"] == "test"
    assert state["until"] is None  # indefinite
    swd.set_pause(False)
    assert swd.is_paused() is False


def test_pause_expiry_auto_resumes():
    """A pause with an elapsed 'until' reads as not paused (no timer needed)."""
    import speak_when_done as swd

    swd.set_pause(True, duration_s=1000, reason="future")
    assert swd.is_paused() is True

    # Simulate time passing well beyond the horizon.
    real = swd.get_pause_state
    with patch("speak_when_done.time.time", return_value=swd.time.time() + 10_000):
        assert swd.is_paused() is False
    # Sanity: without the patch it's still paused.
    assert real()["paused"] is True


def test_pause_corrupt_file_reads_unpaused(tmp_path):
    import speak_when_done as swd

    with patch.object(swd, "_PAUSE_FILE", str(tmp_path / "pause.json")):
        (tmp_path / "pause.json").write_text("{ not json")
        assert swd.is_paused() is False


def test_speak_suppressed_when_paused():
    """Pause silences speech independently of the meeting/mic check."""
    import speak_when_done as swd

    swd.set_pause(True)
    # suppress_in_meeting=False proves it's the PAUSE gate, not the mic gate.
    result = swd.speak("should not play", suppress_in_meeting=False)
    assert result["success"] is False
    assert result["suppressed"] is True
    assert result["reason"] == "paused"


# ---- Fresh-subprocess mic check ---------------------------------------------


def test_fresh_mic_check_delegates_to_subprocess():
    """With MIC_CHECK_FRESH set, is_microphone_active() uses the subprocess,
    NOT the (potentially stale) in-process query."""
    import speak_when_done as swd

    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(swd, "MIC_CHECK_FRESH", True),
        patch.object(swd, "_microphone_active_subprocess", return_value=True) as sub,
        patch.object(swd, "_microphone_active_native", return_value=False) as native,
    ):
        assert swd.is_microphone_active() is True
        sub.assert_called_once()
        native.assert_not_called()


def test_fresh_mic_check_falls_back_when_subprocess_fails():
    """If the subprocess can't answer (None), fall back to the in-process query
    rather than treating the failure as 'mic is off'."""
    import speak_when_done as swd

    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(swd, "MIC_CHECK_FRESH", True),
        patch.object(swd, "_microphone_active_subprocess", return_value=None),
        patch.object(swd, "_microphone_active_native", return_value=True) as native,
    ):
        assert swd.is_microphone_active() is True
        native.assert_called_once()


def test_subprocess_and_native_agree():
    """The fresh subprocess must return the same reading as the in-process
    query at a single instant (they share the CoreAudio logic)."""
    import speak_when_done as swd

    if sys.platform != "darwin":
        pytest.skip("CoreAudio is macOS-only")
    native = swd._microphone_active_native()
    sub = swd._microphone_active_subprocess()
    assert sub is not None, "subprocess mic check failed to run"
    assert sub == native


# ---- History (recent speaks) ------------------------------------------------


def test_recent_speaks_parses_tail_newest_first(tmp_path):
    """_recent_speaks reads speak/suppressed/dropped events from the log tail,
    newest first, skips unrelated events, and tolerates a MISSING persona."""
    import speak_when_done as swd

    rows = [
        {
            "ts": "2026-07-06T03:00:00+00:00",
            "event": "speak",
            "persona": "gumshoe",
            "text": "build done",
            "message_chars": 10,
            "worktree": "/a/b/wt1",
        },
        {
            "ts": "2026-07-06T03:01:00+00:00",
            "event": "speak_suppressed",
            "reason": "paused",
            "text": "quiet one",
        },
        {"ts": "2026-07-06T03:02:00+00:00", "event": "list_voices", "persona": "x"},
        {
            "ts": "2026-07-06T03:03:00+00:00",
            "event": "speak",
            "text": "no persona here",
        },
    ]
    log = tmp_path / "calls.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    prev = swd.MIC_CHECK_FRESH  # importing daemon flips this global; restore it
    try:
        import speak_when_done.daemon as d

        with patch.object(swd, "_CALL_LOG", str(log)):
            recent = d._recent_speaks(limit=10)
    finally:
        swd.MIC_CHECK_FRESH = prev

    assert [r["kind"] for r in recent] == ["spoken", "suppressed", "spoken"]
    assert recent[0]["text"] == "no persona here"
    assert recent[0]["persona"] is None  # persona is optional — not everyone has one
    assert recent[1]["reason"] == "paused"
    assert recent[2]["persona"] == "gumshoe"
    assert recent[2]["worktree"] == "wt1"  # basename only


def test_recent_speaks_missing_log_is_empty(tmp_path):
    import speak_when_done as swd

    prev = swd.MIC_CHECK_FRESH
    try:
        import speak_when_done.daemon as d

        with patch.object(swd, "_CALL_LOG", str(tmp_path / "nope.jsonl")):
            assert d._recent_speaks() == []
    finally:
        swd.MIC_CHECK_FRESH = prev
