# Suppress Speech During Meetings — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically suppress TTS playback when any microphone is active (e.g., user is in a Google Meet call), returning a distinct `suppressed` response.

**Architecture:** Add `is_microphone_active()` to `__init__.py` using macOS CoreAudio via `ctypes` to check all input devices. Gate `speak()` with a `suppress_in_meeting` parameter that calls this check before doing any TTS work. Update `cli.py` and `server.py` to handle the new suppressed return shape.

**Tech Stack:** Python `ctypes`, macOS CoreAudio framework (no new dependencies)

**Spec:** `docs/superpowers/specs/2026-03-12-suppress-during-meeting-design.md`

---

## File Structure

- **Modify:** `speak_when_done/__init__.py` — add `is_microphone_active()` and `suppress_in_meeting` parameter to `speak()`
- **Modify:** `speak_when_done/cli.py` — handle suppressed return (avoid `KeyError` on missing `"error"` key)
- **Modify:** `speak_when_done/server.py` — handle suppressed return in logging
- **Create:** `tests/test_microphone_detection.py` — unit tests for `is_microphone_active()` and suppression integration
- **Modify:** `pyproject.toml` — add pytest as dev dependency

---

## Chunk 1: Core detection and integration

### Task 1: Set up test infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_microphone_detection.py`

- [ ] **Step 1: Add pytest dev dependency**

In `pyproject.toml`, add after the `dependencies` list:

```toml
[project.optional-dependencies]
dev = ["pytest"]
```

- [ ] **Step 2: Create test file with first failing test**

Create `tests/test_microphone_detection.py`:

```python
"""Tests for microphone detection and meeting suppression."""

import sys
from unittest.mock import patch, MagicMock

import pytest


def test_is_microphone_active_returns_bool():
    """is_microphone_active() returns a boolean."""
    from speak_when_done import is_microphone_active

    result = is_microphone_active()
    assert isinstance(result, bool)


def test_is_microphone_active_non_darwin_returns_false():
    """On non-macOS platforms, always returns False."""
    from speak_when_done import is_microphone_active

    with patch.object(sys, "platform", "linux"):
        assert is_microphone_active() is False

    with patch.object(sys, "platform", "win32"):
        assert is_microphone_active() is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: `ImportError` — `is_microphone_active` does not exist yet.

- [ ] **Step 4: Commit test infrastructure**

```bash
git add pyproject.toml tests/test_microphone_detection.py
git commit -m "Add pytest and initial tests for microphone detection"
```

---

### Task 2: Implement `is_microphone_active()`

**Files:**
- Modify: `speak_when_done/__init__.py` (add after `list_voices()`, before `_get_audio_player()`)

- [ ] **Step 1: Add the `is_microphone_active()` function**

Add these imports at the top of `__init__.py` (after existing imports):

```python
import ctypes
import ctypes.util
```

Add this function after `list_voices()` and before `_get_audio_player()`:

```python
def is_microphone_active() -> bool:
    """
    Check if any microphone is currently in use on macOS.

    Uses CoreAudio API to query all audio input devices.
    Returns False on non-macOS platforms.
    """
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
    SCOPE_GLOBAL = 0x676C6F62   # 'glob'
    SCOPE_INPUT = 0x696E7074    # 'inpt'
    PROP_DEVICES = 0x64657623   # 'dev#'
    PROP_STREAMS = 0x73746D23   # 'stm#'
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
        run_addr = AudioObjectPropertyAddress(
            PROP_RUNNING_SOMEWHERE, SCOPE_GLOBAL, 0
        )
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: Both tests PASS.

- [ ] **Step 3: Commit**

```bash
git add speak_when_done/__init__.py
git commit -m "Add is_microphone_active() using CoreAudio API"
```

---

### Task 3: Add `suppress_in_meeting` to `speak()` with tests

**Files:**
- Modify: `speak_when_done/__init__.py` (modify `speak()` signature and add early return)
- Modify: `tests/test_microphone_detection.py` (add integration tests)

- [ ] **Step 1: Add tests for speak() suppression behavior**

Append to `tests/test_microphone_detection.py`:

```python
def test_speak_suppressed_when_mic_active():
    """speak() returns suppressed result when mic is active."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active", return_value=True), \
         patch.object(sys, "platform", "darwin"):
        result = speak("Hello")

    assert result["success"] is False
    assert result["suppressed"] is True
    assert "reason" in result


def test_speak_not_suppressed_when_mic_inactive():
    """speak() proceeds normally when mic is not active and verifies check ran."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active", return_value=False) as mock_mic, \
         patch.object(sys, "platform", "darwin"), \
         patch("speak_when_done._get_audio_player", return_value=["afplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True)

    mock_mic.assert_called_once()
    assert result["success"] is True


def test_speak_suppress_in_meeting_false_skips_check():
    """speak(suppress_in_meeting=False) skips mic check entirely."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active") as mock_mic, \
         patch("speak_when_done._get_audio_player", return_value=["afplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True, suppress_in_meeting=False)

    mock_mic.assert_not_called()
    assert result["success"] is True


def test_speak_suppression_skipped_on_non_darwin():
    """speak() does not check mic on non-macOS even with suppress_in_meeting=True."""
    from speak_when_done import speak

    with patch("speak_when_done.is_microphone_active") as mock_mic, \
         patch.object(sys, "platform", "linux"), \
         patch("speak_when_done._get_audio_player", return_value=["paplay"]), \
         patch("subprocess.run") as mock_run, \
         patch("os.unlink"):
        mock_run.return_value = MagicMock(returncode=0)
        result = speak("Hello", quiet=True)

    mock_mic.assert_not_called()
    assert result["success"] is True
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: New tests FAIL — `speak()` doesn't have `suppress_in_meeting` parameter yet.

- [ ] **Step 3: Modify `speak()` to add suppression**

In `speak_when_done/__init__.py`, change the `speak()` signature and add the early-return check.

Change the signature from:
```python
def speak(message: str, voice: str = "alba", quiet: bool = False) -> dict:
```
to:
```python
def speak(message: str, voice: str = "alba", quiet: bool = False, suppress_in_meeting: bool = True) -> dict:
```

Add this block right after the docstring (before the `player_cmd = _get_audio_player()` line):

```python
    # Check if mic is active (meeting in progress) — macOS only
    if suppress_in_meeting and sys.platform == "darwin" and is_microphone_active():
        return {
            "success": False,
            "suppressed": True,
            "reason": "microphone in use",
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add speak_when_done/__init__.py tests/test_microphone_detection.py
git commit -m "Add suppress_in_meeting parameter to speak()"
```

---

## Chunk 2: CLI and server updates

### Task 4: Fix `cli.py` to handle suppressed returns

**Files:**
- Modify: `speak_when_done/cli.py:58-64`
- Modify: `tests/test_microphone_detection.py` (add CLI test)

- [ ] **Step 1: Add test for CLI suppression handling**

Append to `tests/test_microphone_detection.py`:

```python
def test_cli_handles_suppressed_result(capsys):
    """CLI prints suppression message instead of crashing on KeyError."""
    from speak_when_done.cli import main

    suppressed_result = {"success": False, "suppressed": True, "reason": "microphone in use"}

    with patch("speak_when_done.cli.speak", return_value=suppressed_result), \
         patch("sys.argv", ["speak_when_done", "--text", "Hello"]), \
         pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "microphone in use" in captured.err


def test_cli_handles_normal_error(capsys):
    """CLI still prints normal errors correctly."""
    from speak_when_done.cli import main

    error_result = {"success": False, "error": "TTS generation failed"}

    with patch("speak_when_done.cli.speak", return_value=error_result), \
         patch("sys.argv", ["speak_when_done", "--text", "Hello"]), \
         pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "TTS generation failed" in captured.err
```

- [ ] **Step 2: Run tests to verify the suppressed test fails**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py::test_cli_handles_suppressed_result -v`

Expected: FAIL with `KeyError: 'error'`.

- [ ] **Step 3: Fix `cli.py` error handling**

In `speak_when_done/cli.py`, replace lines 60-62:

```python
    if not result["success"]:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)
```

with:

```python
    if not result["success"]:
        if result.get("suppressed"):
            print(f"Suppressed: {result['reason']}", file=sys.stderr)
        else:
            print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Run all tests**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add speak_when_done/cli.py tests/test_microphone_detection.py
git commit -m "Fix cli.py to handle suppressed returns without KeyError"
```

---

### Task 5: Fix `server.py` logging for suppressed returns

**Files:**
- Modify: `speak_when_done/server.py:53-56`

- [ ] **Step 1: Update server.py logging**

In `speak_when_done/server.py`, replace lines 53-56:

```python
    if result["success"]:
        logger.info("Message spoken successfully")
    else:
        logger.error(f"Speech failed: {result.get('error')}")
```

with:

```python
    if result["success"]:
        logger.info("Message spoken successfully")
    elif result.get("suppressed"):
        logger.info(f"Speech suppressed: {result.get('reason')}")
    else:
        logger.error(f"Speech failed: {result.get('error')}")
```

- [ ] **Step 2: Run all tests to verify nothing broke**

Run: `cd /Users/chang/projects/speak_when_done && uv run pytest tests/test_microphone_detection.py -v`

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add speak_when_done/server.py
git commit -m "Fix server.py logging for suppressed speech returns"
```

---

### Task 6: Manual smoke test

- [ ] **Step 1: Test with no meeting active**

Run: `cd /Users/chang/projects/speak_when_done && uv run python -c "from speak_when_done import is_microphone_active; print(f'Mic active: {is_microphone_active()}')"`

Expected: `Mic active: False` (assuming no call in progress).

- [ ] **Step 2: Test speak() works normally when no meeting**

Run: `cd /Users/chang/projects/speak_when_done && uv run speak_when_done --text "Test speech" --quiet`

Expected: Audio plays, exits with code 0.

- [ ] **Step 3: Test during a meeting (if possible)**

Start a Google Meet or any app that uses the microphone, then run:

`cd /Users/chang/projects/speak_when_done && uv run python -c "from speak_when_done import is_microphone_active; print(f'Mic active: {is_microphone_active()}')"`

Expected: `Mic active: True`.

Then: `cd /Users/chang/projects/speak_when_done && uv run speak_when_done --text "Should be suppressed"`

Expected: `Suppressed: microphone in use` on stderr, exit code 1, no audio.
