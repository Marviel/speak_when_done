# Suppress Speech During Meetings

## Problem

When the user is in a Google Meet (or any meeting/call), `speak_when_done` plays audio that interrupts the call. Speech should be silently suppressed when any microphone is active.

## Design

### Detection: `is_microphone_active()` (macOS only)

A new function in `speak_when_done/__init__.py` that uses the macOS CoreAudio C API via Python `ctypes` to detect whether any audio input device is currently in use.

**How it works:**

1. Load `/System/Library/Frameworks/CoreAudio.framework/CoreAudio` via `ctypes.cdll.LoadLibrary`
2. Query `kAudioHardwarePropertyDevices` (`'dev#'` / `0x64657623`) on `kAudioObjectSystemObject` to get all audio device IDs
3. For each device, check if it has input streams via `kAudioDevicePropertyStreams` (`'stm#'` / `0x73746d23`) with `kAudioObjectPropertyScopeInput` (`'inpt'` / `0x696e7074`)
4. For each input device, query `kAudioDevicePropertyDeviceIsRunningSomewhere` (`'gone'` / `0x676f6e65`) with `kAudioObjectPropertyScopeGlobal` (`'glob'` / `0x676c6f62`)
5. Return `True` if any input device has `IsRunningSomewhere == 1`

**Why check all input devices, not just the default:**
During testing, a Google Meet call used the built-in MacBook Pro microphone (device 78) while the default input device was AirPods (device 89). Checking only the default would miss this.

**No special permissions required.** This reads public I/O Registry state via CoreAudio — no TCC prompt, no entitlements. Verified on macOS 15 (Apple Silicon).

**Platform scoping:** This function only works on macOS (`sys.platform == "darwin"`). On other platforms it returns `False` (no suppression).

### Integration into `speak()`

Add a `suppress_in_meeting` parameter (default `True`) to the existing `speak()` function.

Before generating TTS audio, if `suppress_in_meeting is True` and `sys.platform == "darwin"`:
- Call `is_microphone_active()`
- If `True`, skip TTS generation and playback entirely
- Return `{"success": False, "suppressed": True, "reason": "microphone in use"}`

If the mic is not active, proceed as normal.

The `suppress_in_meeting=False` escape hatch lets library users override the behavior when they explicitly want speech during a call.

### Return value when suppressed

```python
{
    "success": False,
    "suppressed": True,
    "reason": "microphone in use"
}
```

- `success: False` — the caller knows speech did not happen
- `suppressed: True` — distinguishes this from an actual error (TTS failure, missing player, etc.)
- `reason` — human-readable explanation

### What changes

- `speak_when_done/__init__.py`:
  - Add `AudioObjectPropertyAddress` ctypes struct
  - Add `is_microphone_active() -> bool` function
  - Add `suppress_in_meeting: bool = True` parameter to `speak()`
  - Add early-return check at the top of `speak()`
- `speak_when_done/cli.py`:
  - Handle the `suppressed` return case — currently `cli.py` accesses `result["error"]` on failure, which would `KeyError` on a suppressed return. Check for `result.get("suppressed")` and print an appropriate message instead.
- `speak_when_done/server.py`:
  - Handle the `suppressed` return case in logging — currently logs `Speech failed: {result.get('error')}` which would show `None` for suppressed returns. Log a distinct message for suppressed calls.

### What does NOT change

- No new dependencies
- No new files

### Future extension: queue and delay

This design leaves the door open for a future "queue and delay" mode where suppressed messages are held and played after the meeting ends. The `suppressed` field in the return value provides the signal a queue system would need. This is explicitly out of scope for now.
