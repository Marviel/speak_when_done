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
| `-l` | `--list-voices` | List built-in voices, personas, and the active worktree persona |
| | `--list-profiles` | List configured voice profiles |
| | `--profile-json` | Output resolved profile as JSON |
| | `--validate-personas` | Check persona files against `PERSONA_VOICES`; exit non-zero on drift |
| | `--tail [N]` | Show the last N call-log records (observability) |
| | `--tail-desync [N]` | Show the last N drift events (empty output is the goal) |

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

Only the first 30 seconds of the audio file are used. Voice cloning exports are **automatically cached** as safetensors files in `~/.cache/speak_when_done/voices/`. The first call with a new voice file takes ~5 extra seconds for the export; subsequent calls use the cache. The cache auto-invalidates when the source audio file changes (keyed by SHA256).

You can also manually pre-export with [pocket-tts](https://github.com/kyutai-labs/pocket-tts):
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

## Per-worktree personas

For multi-agent setups (many concurrent coding-agent sessions, each in its own
git worktree), speak_when_done can give every worktree a stable, distinct
**persona**: a full spoken register (who the character is, how triumph/failure/
tedium bend them, what to avoid) paired with a voice, a speed, and a language.

- **Registers live in `personas/`** — one Markdown file per persona, plus
  `_common.md` for shared discipline and TTS-friendly writing rules. The files
  are re-read on every call, so edits apply without a restart. The audio
  plumbing (voice path, speed, language, fallback tagline) lives in
  `PERSONA_VOICES` in `speak_when_done/__init__.py`.
- **Assignment is deterministic**: SHA-256 of the worktree path over the sorted
  roster. The same worktree always gets the same persona, across sessions and
  restarts. Callers pass their `cwd`; when they can't, the library walks up the
  process tree to discover it.
- **Persona and voice resolve together** (`_resolve_active_persona_and_voice`),
  so the register that composed the message and the timbre that speaks it can
  never drift apart. Runtime drift checks (`drift` in every response) plus an
  offline validator (`speak_when_done --validate-personas`) catch mismatches:
  missing voice files, orphaned or thin register files, wrong headers.
- **Cloned voices** go in `~/.claude/voices/<persona>.safetensors` (override
  with `SPEAK_WHEN_DONE_VOICES_DIR`). A persona whose file is missing falls
  back to the `alba` builtin voice but keeps its register. Three personas
  (`flightdeck`, `gumshoe`, `expediter`) use Pocket TTS builtin voices and work
  with no cloned files at all.
- **Observability**: every `speak()`/`list_voices()` call appends a JSONL
  record to `logs/calls.jsonl` (drift events also go to `logs/desync.jsonl`).
  Inspect with `speak_when_done --tail` / `--tail-desync`.

- **Pinning**: `personas/assignments.json` (machine-local, gitignored; format
  in `personas/assignments.example.json`) pins specific worktrees to specific
  personas. Pins win over the hash and are re-read on every call. This is both
  the manual override and the roster-expansion safety net: the hash is modular
  over the roster size, so adding a persona would reshuffle every existing
  worktree's voice — pin the worktrees you know about first, then grow the
  roster.

Agents consume this through `list_voices()`: the response includes
`active_persona` and `active_style` (the shared discipline + the active
persona's full register) so the agent can compose its spoken sign-off in
character before calling `speak`.

Env overrides: `SPEAK_WHEN_DONE_PERSONA_DIR`, `SPEAK_WHEN_DONE_VOICES_DIR`,
`SPEAK_WHEN_DONE_LOG_DIR`, `SPEAK_WHEN_DONE_LANGUAGE`, `SPEAK_WHEN_DONE_VOICE`
(a persona's safetensors path locks that persona; a builtin name keeps the
worktree persona's register but swaps the timbre).

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
