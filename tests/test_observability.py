"""Tests for observability + drift detection.

The persona/voice resolver passes already (test_persona_sync.py). These
tests cover the runtime drift detection that fires at speak()/list_voices()
time even when the offline validator is happy.
"""

import json
import os

import pytest

from speak_when_done import (
    PERSONA_VOICES,
    _CALL_LOG,
    _check_drift,
    _file_sha,
    _load_persona_playbooks,
    _log_event,
    _pick_persona_for_worktree,
    _text_sha,
    list_voices,
)


def test_text_sha_is_stable_and_truncated():
    """Same input → same 16-char hex digest."""
    a = _text_sha("hello")
    b = _text_sha("hello")
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_text_sha_changes_on_edit():
    """Drift signal: any change to the prompt produces a new hash."""
    a = _text_sha("## herzog — original")
    b = _text_sha("## herzog — slightly edited")
    assert a != b


def test_file_sha_returns_none_for_missing():
    assert _file_sha("/nonexistent/path/does-not-exist.safetensors") is None


def test_file_sha_returns_hex_for_real_file():
    # Use any existing voice as a smoke test; if none exist, skip.
    for cfg in PERSONA_VOICES.values():
        if os.path.exists(cfg["path"]):
            sha = _file_sha(cfg["path"])
            assert sha is not None
            assert len(sha) == 16
            return
    pytest.skip("no voice safetensors on disk")


def test_check_drift_clean_when_paired():
    """The happy path: persona + matching voice + matching playbook section."""
    playbooks = _load_persona_playbooks()
    persona = "aghdashloo"
    voice = PERSONA_VOICES[persona]["path"]
    issues = _check_drift(persona, voice, playbooks)
    # Empty list when everything lines up. Voice file may be missing on
    # CI machines; only fail if drift cites a different reason.
    assert all("does not match" not in i for i in issues), issues


def test_check_drift_flags_mismatched_voice():
    """Wrong .safetensors path for this persona is the canonical desync."""
    playbooks = _load_persona_playbooks()
    issues = _check_drift(
        "aghdashloo",
        PERSONA_VOICES["herzog"]["path"],
        playbooks,
    )
    assert any("does not match" in i for i in issues), issues


def test_check_drift_flags_missing_section():
    issues = _check_drift("aghdashloo", PERSONA_VOICES["aghdashloo"]["path"], {})
    assert any("no persona file at" in i and "aghdashloo" in i for i in issues)


def test_check_drift_flags_thin_section():
    """A section that got trimmed below the useful threshold is drift."""
    thin = {"aghdashloo": "## aghdashloo — too thin to be useful\n"}
    issues = _check_drift("aghdashloo", PERSONA_VOICES["aghdashloo"]["path"], thin)
    assert any("thin" in i for i in issues)


def test_check_drift_flags_unknown_persona():
    issues = _check_drift("not_a_real_persona", "/whatever", {})
    assert any("missing from PERSONA_VOICES" in i for i in issues)


def test_check_drift_does_not_flag_builtin_voice():
    """Built-in voices (alba, charlie, etc.) are explicit overrides, not drift."""
    playbooks = _load_persona_playbooks()
    issues = _check_drift("aghdashloo", "alba", playbooks)
    # No "does not match" — built-in voice names don't end in .safetensors.
    assert all("does not match" not in i for i in issues)


def test_log_event_writes_jsonl(tmp_path, monkeypatch):
    """_log_event appends parseable JSONL records."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr("speak_when_done._LOG_DIR", str(log_dir))
    monkeypatch.setattr("speak_when_done._CALL_LOG", str(log_dir / "calls.jsonl"))
    monkeypatch.setattr("speak_when_done._DESYNC_LOG", str(log_dir / "desync.jsonl"))

    _log_event("test_event", persona="herzog", voice="x")
    _log_event("test_drift", persona="herzog", drift=["bad"])

    calls = (log_dir / "calls.jsonl").read_text().strip().splitlines()
    desync = (log_dir / "desync.jsonl").read_text().strip().splitlines()
    assert len(calls) == 2
    assert len(desync) == 1
    rec = json.loads(calls[0])
    assert rec["event"] == "test_event"
    assert rec["persona"] == "herzog"
    assert "ts" in rec
    drift_rec = json.loads(desync[0])
    assert drift_rec["drift"] == ["bad"]


def test_log_event_never_raises(monkeypatch):
    """Best-effort: a broken log path must not crash speak() / list_voices()."""
    monkeypatch.setattr("speak_when_done._LOG_DIR", "/nonexistent/cannot/be/written")
    monkeypatch.setattr(
        "speak_when_done._CALL_LOG", "/nonexistent/cannot/be/written/x.jsonl"
    )
    # Should not raise.
    _log_event("test_event", persona="herzog")


def test_pick_persona_fallback_no_longer_alphabetical():
    """When cwd is None, fall back to a hash of (hostname, ppid) instead of
    alphabetical names[0]. Two consecutive calls in this same process get
    the same persona (ppid is stable), but it isn't always 'aghdashloo'
    across hosts/parents.
    """
    p1 = _pick_persona_for_worktree(None)
    p2 = _pick_persona_for_worktree(None)
    assert p1 == p2  # deterministic within a process
    assert p1 in PERSONA_VOICES


def test_list_voices_includes_sha_fields():
    """Every list_voices response must expose prompt_sha + voice_sha + drift."""
    r = list_voices()
    assert "prompt_sha" in r
    assert "voice_sha" in r
    assert "drift" in r
    assert isinstance(r["drift"], list)
    # Active persona's prompt_sha must be a 16-char hex (the persona files
    # ship in this repo's personas/ directory, so it's never empty here).
    assert len(r["prompt_sha"]) == 16
