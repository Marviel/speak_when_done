"""Tests for the playback serialization lock.

The lock prevents concurrent speak() calls from talking over each other.
These tests guard against regressions in the lock path and file-open mode.
"""

import ast
import inspect
import os
import textwrap

import speak_when_done


def test_lock_path_is_shared_not_session_specific():
    """Lock must use a fixed path so all sessions contend on the same file.

    tempfile.gettempdir() returns a per-session directory on macOS, which
    breaks cross-session serialization. The lock must live at a fixed,
    persistent path that every Claude Code session can see.
    """
    lock_path = speak_when_done._PLAYBACK_LOCK_PATH
    assert "tmp" not in lock_path.lower(), (
        f"Lock path {lock_path!r} appears to be in a temp directory. "
        "This breaks cross-session serialization on macOS because "
        "tempfile.gettempdir() returns a per-session path."
    )
    assert os.path.isabs(lock_path), "Lock path must be absolute"


def test_lock_path_resolves_to_home_claude():
    """Lock should live in ~/.claude/ where all sessions can find it."""
    lock_path = speak_when_done._PLAYBACK_LOCK_PATH
    expected_dir = os.path.expanduser("~/.claude")
    assert lock_path.startswith(expected_dir), (
        f"Lock path {lock_path!r} is not under {expected_dir}. "
        "It must be in a shared, persistent location."
    )


def test_lock_file_opened_in_append_mode():
    """Lock file must be opened with 'a' mode, not 'w'.

    Opening with 'w' truncates the file on each open, which can break
    flock() serialization when processes close and reopen between locks.
    """
    source = inspect.getsource(speak_when_done.speak)
    tree = ast.parse(textwrap.dedent(source))

    lock_opens = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and len(node.args) >= 1
        ):
            first_arg = ast.dump(node.args[0])
            if "LOCK" in first_arg or "lock" in first_arg.lower():
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    lock_opens.append(node.args[1].value)

    assert lock_opens, "Could not find open() call for the lock file in speak()"
    for mode in lock_opens:
        assert mode != "w", (
            "Lock file must not be opened with 'w' (truncates on open). "
            "Use 'a' to preserve the inode across processes."
        )
        assert mode == "a", f"Unexpected lock file open mode: {mode!r}"
