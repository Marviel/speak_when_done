# speak_when_done

Text-to-speech with automatic temp file handling. Speaks text aloud and cleans up after itself.

Works as a CLI tool, Python library, or MCP server for AI assistants.

## What it does

```bash
uvx --from git+https://github.com/Marviel/speak_when_done speak_when_done --text "Your build is complete"
```

That's it. It generates speech, plays it, and cleans up the temp file automatically.

### As an MCP server

You kick off a long task (build, test suite, deployment) and go do something else. When it's done, your AI speaks to you:

> "Your build completed successfully with no errors."

> "The test suite finished. 47 passed, 2 failed."

> "I found the bug you were looking for in the auth module."

## Prerequisites

- macOS (uses `afplay` for audio playback)
- [uv](https://docs.astral.sh/uv/) package manager

Test that pocket-tts works:
```bash
uvx pocket-tts generate --text "hello world" --quiet
```

## Installation

### CLI (via uvx)

No installation needed! Just run:
```bash
uvx --from git+https://github.com/Marviel/speak_when_done speak_when_done --text "Hello world"
```

Options:
```bash
uvx --from git+https://github.com/Marviel/speak_when_done speak_when_done -t "Hello" -v alba -q
```

| Flag | Long | Description |
|------|------|-------------|
| `-t` | `--text` | Text to speak (required) |
| `-v` | `--voice` | Voice to use (default: alba) |
| `-p` | `--profile` | Voice profile from config |
| `-s` | `--speed` | Playback speed multiplier (default: 1.0) |
| `-w` | `--warmup` | Text prepended for voice cloning warmup |
| `-q` | `--quiet` | Suppress TTS output |
| | `--ignore-meeting` | Speak even if microphone is active |
| `-l` | `--list-voices` | List built-in voices |
| | `--list-profiles` | List configured voice profiles |
| | `--profile-json` | Output resolved profile as JSON |

### Python library

```bash
pip install git+https://github.com/Marviel/speak_when_done
# or
uv add git+https://github.com/Marviel/speak_when_done
```

```python
from speak_when_done import speak

result = speak("Hello world")
result = speak("Hello", voice="alba", quiet=True)
result = speak("Done", voice="/path/to/clone.wav", speed=1.25, warmup="... ...")
```

### MCP Server for Claude Code

Add globally (available in all projects):
```bash
claude mcp add speak_when_done -s user -- uvx --from git+https://github.com/Marviel/speak_when_done python -m speak_when_done.server
```

Or project-specific:
```bash
claude mcp add speak_when_done -- uvx --from git+https://github.com/Marviel/speak_when_done python -m speak_when_done.server
```

### MCP Server for Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "speak_when_done": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Marviel/speak_when_done",
        "python",
        "-m",
        "speak_when_done.server"
      ]
    }
  }
}
```

Then restart Cursor or reload the window.

## Usage with AI assistants

Once installed as an MCP server, your AI has access to a `speak` tool. Ask it to notify you when something finishes:

> "Run the full test suite and tell me out loud when it's done"

> "Deploy to staging and speak to me when it completes"

> "Search for all usages of the deprecated API and let me know what you find"

## Recommended Instructions

Add to your custom instructions or CLAUDE.md:

```
When using the speak_when_done MCP:
- Only use the speak tool after completing long-running tasks (builds, tests, deployments, extensive searches)
- Keep spoken messages brief and informative
- Do not use speak for routine responses or simple questions
```

## Voices

### Built-in voices

Pocket TTS includes several built-in voices. List them with:
```bash
speak_when_done --list-voices
```

### Voice cloning

You can clone any voice by passing a path to an audio file (WAV or MP3):
```bash
speak_when_done --text "Hello" --voice /path/to/sample.wav
```

Only the first 30 seconds of the audio file are used. For faster repeated use, pre-export to safetensors with [pocket-tts](https://github.com/kyutai-labs/pocket-tts):
```bash
uvx pocket-tts export-voice clip.mp3 my_voice.safetensors
speak_when_done --text "Hello" --voice my_voice.safetensors
```

> **Note:** Voice cloning requires accepting the Hugging Face license at [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts) and logging in with `uvx hf auth login`.

### Voice warmup

When using voice cloning, the first few frames can be unstable. Use `--warmup` to prepend filler text that absorbs this:
```bash
speak_when_done --text "Build complete" --voice clone.wav --warmup "... ..."
```

### Speed control

Adjust playback speed with `--speed` (requires ffmpeg):
```bash
speak_when_done --text "Hello" --speed 1.25
```

## Voice profiles

Configure reusable voice profiles in `~/.config/speak_when_done/voices.yaml`:

```yaml
default: galadriel

# Allow AI agents (via MCP) to pick their own voice from available profiles.
# When false (default), agents always use the default profile.
agent_can_choose: false

voices:
  attenborough:
    voice: /path/to/david-attenborough-30s.wav
    speed: 1.0
    warmup: "... ..."
    persona: "David Attenborough — warm, dry naturalist wit."

  galadriel:
    voice: /path/to/galadriel-30s.wav
    speed: 1.25
    warmup: "... ..."
    persona: "Galadriel (Cate Blanchett) — tasteful, minimal ethereal word choice."

  protoss:
    voice: /path/to/protoss-advisor-30s.wav
    speed: 1.0
    warmup: "... ..."
    persona: >-
      StarCraft Protoss Executor advisor — commanding alien gravitas.
      Tactical, direct, slightly reverent toward the work.
```

Use a profile:
```bash
speak_when_done --profile galadriel --text "Tests passed"
speak_when_done --list-profiles  # show all configured profiles
speak_when_done --profile-json   # output resolved profile as JSON (useful for scripts)
```

Profile fields:
| Field | Description |
|-------|-------------|
| `voice` | Path to audio file or built-in voice name |
| `speed` | Playback speed multiplier (default: 1.0) |
| `warmup` | Text prepended before message for voice cloning warmup |
| `persona` | Description used by AI agents to shape the spoken message tone |

Config is also settable via environment variables:
- `SPEAK_WHEN_DONE_CONFIG` — path to config file
- `SPEAK_WHEN_DONE_PROFILE` — default profile name
- `SPEAK_WHEN_DONE_AGENT_CAN_CHOOSE` — `true`/`false`

## Meeting suppression

On macOS, speech is automatically suppressed when a microphone is active (e.g. during a video call). Override with `--ignore-meeting`.

## Built on pocket-tts

speak_when_done uses [Kyutai's Pocket TTS](https://github.com/kyutai-labs/pocket-tts) for speech generation. Pocket TTS is a small, fast text-to-speech model that runs on CPU. See the [pocket-tts docs](https://github.com/kyutai-labs/pocket-tts) for advanced options like custom model configs, temperature tuning, and the web UI.

## Troubleshooting

**"Command not found" error:**
Make sure `uvx` and `pocket-tts` are available in your PATH.

**No audio playback:**
Ensure your macOS audio is not muted and `afplay` is working:
```bash
afplay /System/Library/Sounds/Glass.aiff
```

**MCP not connecting in Claude Code:**
```bash
claude mcp list
claude mcp get speak_when_done
```

**MCP not connecting in Cursor:**
Check Settings → Features → MCP to ensure MCP is enabled, then verify your JSON config is valid.
