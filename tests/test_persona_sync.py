"""Tests for persona ↔ persona-file sync.

The audio plumbing (PERSONA_VOICES) and the persona register (individual
files in personas/) must agree on the set of personas. This test catches
drift — a persona key added to one source but not the other, or a file
that got trimmed below a useful length.
"""

from speak_when_done import (
    PERSONA_VOICES,
    _PERSONA_DIR,
    _load_persona_playbooks,
    _resolve_active_persona_and_voice,
    validate_persona_sync,
)


def test_every_persona_has_a_playbook_file():
    """Every PERSONA_VOICES key has a matching file in personas/."""
    playbooks = _load_persona_playbooks()
    missing = sorted(set(PERSONA_VOICES.keys()) - set(playbooks.keys()))
    assert not missing, (
        f"Personas declared in code but missing from {_PERSONA_DIR}: "
        f"{missing}. Add a `<name>.md` file there."
    )


def test_no_orphan_playbook_files():
    """Every parsed playbook file corresponds to a real persona voice."""
    playbooks = _load_persona_playbooks()
    orphans = sorted(set(playbooks.keys()) - set(PERSONA_VOICES.keys()))
    assert not orphans, (
        f"Playbook files in {_PERSONA_DIR} without a matching PERSONA_VOICES entry: "
        f"{orphans}. Either remove the file or add the voice."
    )


def test_no_thin_sections():
    """Each persona file has enough content to actually shape composition.

    400 chars is roughly: a tagline + register bullets + opens-with line. Below
    that the file was probably trimmed by mistake or never finished.
    """
    playbooks = _load_persona_playbooks()
    thin = sorted(n for n, body in playbooks.items() if len(body) < 400)
    assert not thin, (
        f"Persona files shorter than 400 chars in {_PERSONA_DIR}: "
        f"{thin}. They probably need a real Register / Phrase bank / Vignettes block."
    )


def test_validate_persona_sync_returns_ok():
    """The bundled validator agrees with the individual checks above."""
    result = validate_persona_sync()
    assert result["ok"] is True, result


def test_each_section_starts_with_correct_header():
    """Defends against the parser silently grabbing the wrong block.

    Each parsed file must start with `## <name> —`.
    """
    playbooks = _load_persona_playbooks()
    for name, body in playbooks.items():
        first_line = body.split("\n", 1)[0]
        assert first_line.startswith(f"## {name} "), (
            f"File for {name!r} does not start with `## {name} —`: {first_line!r}"
        )


def test_persona_and_voice_resolve_together():
    """Active persona ↔ voice path stay paired across resolutions.

    The whole point of `_resolve_active_persona_and_voice`: it returns a
    matched (persona, voice) tuple so the spoken register and the voice
    that plays it can never desync.
    """
    persona, voice = _resolve_active_persona_and_voice("/Users/test/some/worktree")
    assert persona in PERSONA_VOICES
    # Voice is either the persona's own safetensors path or the 'alba' fallback
    # when the safetensors is missing on disk. Never an unrelated persona's path.
    expected_path = PERSONA_VOICES[persona]["path"]
    assert voice in (expected_path, "alba"), (
        f"resolver returned mismatched persona={persona!r} voice={voice!r}; "
        f"expected voice {expected_path!r} or fallback 'alba'"
    )


def test_persona_selection_is_deterministic():
    """Same worktree path always resolves to the same persona."""
    p1, _ = _resolve_active_persona_and_voice("/Users/test/some/worktree")
    p2, _ = _resolve_active_persona_and_voice("/Users/test/some/worktree")
    assert p1 == p2
