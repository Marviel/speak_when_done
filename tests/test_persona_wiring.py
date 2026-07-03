"""Tests for the 2026-07-02 persona-roster expansion wiring.

Three new personas (flightdeck/gumshoe/expediter) use Pocket TTS BUILTIN
voice NAMES as their "path" instead of cloned .safetensors files, and
pre-existing worktrees were pinned in personas/assignments.json (seeded from
the frozen six-persona roster) so growing the roster never reshuffled an
existing worktree's voice. Covered here:

- builtin-name voice resolution (no coercion to the "alba" fallback, and the
  persona's own path comes back so speak()'s speed modifier still applies)
- drift checking: builtin names never flag "voice file missing on disk";
  a genuinely missing .safetensors path still does
- assignments.json pins win over the full-roster hash (via a tmp pins file —
  never the real one)
- the three new personas are reachable for unpinned (new) worktrees
- assignment is deterministic per path

All resolution tests isolate _ASSIGNMENTS_PATH to tmp_path and clear
SPEAK_WHEN_DONE_VOICE, so nothing here reads or writes the real personas dir.
"""

import json

import pytest

import speak_when_done as swd
from speak_when_done import (
    _LEGACY_PERSONAS,
    PERSONA_VOICES,
    _check_drift,
    _is_builtin_voice,
    _legacy_persona_for_worktree,
    _load_persona_playbooks,
    _pick_persona_for_worktree,
    _resolve_active_persona_and_voice,
)

NEW_PERSONAS = ("flightdeck", "gumshoe", "expediter")


def _fake_playbook(persona: str) -> str:
    """A minimal well-formed persona section (correct header, >= 400 chars)."""
    return f"## {persona} — test register\n" + "filler register line\n" * 30


@pytest.fixture()
def pins_file(monkeypatch, tmp_path):
    """Isolate persona resolution from the real assignments.json and env.

    Points _ASSIGNMENTS_PATH at a tmp file (initially absent, i.e. no pins)
    and clears SPEAK_WHEN_DONE_VOICE so resolution takes the pin/hash path.
    """
    monkeypatch.delenv("SPEAK_WHEN_DONE_VOICE", raising=False)
    path = tmp_path / "assignments.json"
    monkeypatch.setattr(swd, "_ASSIGNMENTS_PATH", str(path))
    return path


def _pin(pins_file, worktree: str, persona: str) -> None:
    pins_file.write_text(json.dumps({"pins": {worktree: persona}}))


# ---- builtin-voice resolution ------------------------------------------------


@pytest.mark.parametrize("persona", NEW_PERSONAS)
def test_builtin_voice_persona_resolves_to_builtin_name(pins_file, persona):
    """A persona whose path is a builtin NAME resolves to that name, not 'alba'.

    Builtin names never exist on disk, so the missing-file fallback must skip
    them — otherwise every builtin-voice persona silently loses its timbre.
    """
    wt = "/Users/test/builtin-voice-worktree"
    _pin(pins_file, wt, persona)
    got_persona, got_voice = _resolve_active_persona_and_voice(wt)
    assert got_persona == persona
    expected = PERSONA_VOICES[persona]["path"]
    assert _is_builtin_voice(expected), (
        f"{persona}.path {expected!r} is expected to be a builtin voice name"
    )
    assert got_voice == expected, (
        f"builtin-name voice was coerced: got {got_voice!r}, want {expected!r}"
    )
    assert got_voice != "alba"


@pytest.mark.parametrize("persona", NEW_PERSONAS)
def test_builtin_voice_persona_speed_survives(pins_file, persona):
    """Resolution returns the persona's OWN path, which is exactly the
    condition speak() checks (is_persona_voice) before applying the persona's
    speed modifier. If resolution fell back to 'alba', speed would be lost.
    """
    wt = "/Users/test/builtin-voice-worktree"
    _pin(pins_file, wt, persona)
    _, voice = _resolve_active_persona_and_voice(wt)
    cfg = PERSONA_VOICES[persona]
    assert voice == cfg["path"], (
        "voice != persona path would disable the persona speed in speak()"
    )
    # The new personas all carry a non-default speed; it must still be wired.
    assert cfg.get("speed", 1.0) != 1.0


# ---- drift checking ----------------------------------------------------------


@pytest.mark.parametrize("persona", NEW_PERSONAS)
def test_builtin_voice_produces_no_missing_file_drift(persona):
    """Builtin voice names must not be flagged as missing files on disk."""
    voice = PERSONA_VOICES[persona]["path"]
    playbooks = {persona: _fake_playbook(persona)}
    issues = _check_drift(persona, voice, playbooks)
    assert not any("voice file missing" in i for i in issues), issues
    assert issues == []


def test_builtin_voice_personas_are_drift_free_against_real_playbooks():
    """The live wiring: real persona files + builtin voices → drift == []."""
    playbooks = _load_persona_playbooks()
    for persona in NEW_PERSONAS:
        issues = _check_drift(persona, PERSONA_VOICES[persona]["path"], playbooks)
        assert issues == [], f"{persona}: {issues}"


def test_missing_safetensors_still_drifts(monkeypatch, tmp_path):
    """A genuinely absent .safetensors path keeps its missing-file drift entry."""
    ghost_path = str(tmp_path / "ghost.safetensors")  # deliberately never created
    monkeypatch.setitem(
        PERSONA_VOICES,
        "ghost",
        {"path": ghost_path, "speed": 1.0, "tagline": "test-only ghost persona"},
    )
    issues = _check_drift("ghost", ghost_path, {"ghost": _fake_playbook("ghost")})
    assert any("voice file missing on disk" in i for i in issues), issues


# ---- assignment stability (pins) ----------------------------------------------


def test_pinned_worktree_overrides_full_roster_hash(pins_file):
    """A pin in assignments.json wins over whatever the nine-name hash says."""
    wt = "/Users/test/pinned-worktree"
    hashed = _pick_persona_for_worktree(wt)  # no pins file yet → pure hash
    pinned = next(n for n in sorted(PERSONA_VOICES) if n != hashed)
    _pin(pins_file, wt, pinned)
    assert _pick_persona_for_worktree(wt) == pinned
    persona, _ = _resolve_active_persona_and_voice(wt)
    assert persona == pinned


def test_seeded_pin_preserves_legacy_assignment(pins_file):
    """The roster-expansion scenario the pins exist for: a pre-expansion
    worktree whose six-roster (legacy) persona differs from the nine-roster
    hash must keep its legacy persona once pinned."""
    wt = next(
        p
        for p in (f"/Users/test/legacy-wt-{i}" for i in range(500))
        if _legacy_persona_for_worktree(p) != _pick_persona_for_worktree(p)
    )
    legacy = _legacy_persona_for_worktree(wt)
    _pin(pins_file, wt, legacy)
    persona, _ = _resolve_active_persona_and_voice(wt)
    assert persona == legacy


def test_pin_to_unknown_persona_falls_back_to_hash(pins_file):
    """A pin naming a retired/unknown persona is ignored, not a crash."""
    wt = "/Users/test/unknown-pin-worktree"
    hashed = _pick_persona_for_worktree(wt)
    _pin(pins_file, wt, "nonexistent-persona")
    assert _pick_persona_for_worktree(wt) == hashed


def test_legacy_assignment_only_uses_legacy_roster():
    """Pin seeding relies on the legacy hash never producing a new persona."""
    for i in range(64):
        persona = _legacy_persona_for_worktree(f"/Users/test/wt-{i}")
        assert persona in _LEGACY_PERSONAS


# ---- new-worktree reachability + determinism ----------------------------------


@pytest.mark.parametrize("persona", NEW_PERSONAS)
def test_new_personas_reachable_for_unpinned_worktrees(pins_file, persona):
    """Unpinned (new) worktrees hash over the FULL roster, so each new persona
    must be reachable. Deterministic: sha256 over synthetic paths, no RNG."""
    for i in range(2000):
        wt = f"/Users/test/reachability-wt-{i}"
        if _pick_persona_for_worktree(wt) == persona:
            got_persona, got_voice = _resolve_active_persona_and_voice(wt)
            assert got_persona == persona
            assert got_voice == PERSONA_VOICES[persona]["path"]
            return
    pytest.fail(f"no synthetic path out of 2000 hashed to {persona!r}")


def test_same_path_resolves_same_persona_across_calls(pins_file):
    """Same worktree path → same (persona, voice) on every call."""
    wt = "/Users/test/determinism-worktree"
    results = {_resolve_active_persona_and_voice(wt) for _ in range(5)}
    assert len(results) == 1
