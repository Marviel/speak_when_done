# Voice cloning: source-audio preparation tips

Lessons from ~35 cloning rounds with pocket-tts across a dozen target voices.
The model matters less than the source clip — the same voice can rate 1/5 or
5/5 depending entirely on what you feed the exporter.

## What clones well (and what doesn't)

**TTS-safe voices** share three traits: a consistent register, clear
articulation, and distinctiveness that comes from *timbre* rather than
theatrics. Steady narrators, deadpan presenters, and even shrill-but-consistent
comedic voices all clone to 4-5/5.

**TTS-unsafe voices** fail at ≤3/5 no matter how clean the source:

- **Extreme dynamic range** — a performer who whispers one phrase and yells the
  next; the export averages into mush.
- **Non-standard phonation** — raspy whispers, croaks, character growls.
- **Pause-cadence-as-identity** — if the voice's signature is *where it stops*,
  TTS smooths the pauses out and the identity disappears with them.

Surprising corollary: consistency matters more than caricature. A shrill,
cartoonish voice clones fine if it's *always* shrill.

## Choosing source material

- **Studio voiceover beats character delivery**, even when the character is
  iconic. A plain radio ad read by your target voice will out-clone their most
  famous film monologue.
- **1–3 minutes of clean, single-speaker audio** is the sweet spot; with good
  filtering even ~60 seconds produces a keeper. More minutes ≠ better clones.
- **Super-concat**: concatenating 2–4 clean windows from different recordings
  of the same era usually beats one long window.
- **Stay in one era.** Voices age. A compilation spanning 15 years of a
  performer's career clones into someone who sounds "off" in every era at once.
- **Reverb kills cloning.** Baked-in studio or hall reverb can take an
  otherwise perfect source to 1/5. Prefer dry broadcast/podcast audio.
- **Beware AI-narrated compilations.** "Relaxing <famous voice> narration"
  YouTube channels are frequently AI impersonations — you'll clone the clone,
  and it sounds subtly wrong (accent drift is a common tell). Source only from
  verified/official uploads.

## Cleaning the audio

- **Music beds: separate vocals with [demucs](https://github.com/facebookresearch/demucs)**
  (`demucs --two-stems=vocals input.wav`). Works well on narration over music.
  It *hurts* character voices with unusual phonation — skip it there.
- **Multiple speakers: filter, don't discard.** See
  [`contrib/filter_speaker.py`](../contrib/filter_speaker.py) — sliding-window
  speaker embeddings, cluster, keep the dominant speaker's windows. Turns
  interview/compilation sources into usable single-speaker training audio.
  Run it on the demucs output.
- **Never loudnorm.** `ffmpeg loudnorm` introduces audible artifacts in the
  TTS output. If you need level, use plain linear gain (`-af volume=12dB`).

## Generation settings

`--eos-threshold -2 --frames-after-eos 0` prevents the synthesized voice from
"trailing off" at sentence ends — worth setting globally once you notice it.

## Auditioning

Audition with text written *in the voice's own register* (the kind of sentence
that voice would actually say), not a flat test sentence. The same clone reads
a full grade better with in-character material — flat lines hide keepers, and
what you actually ship is in-character anyway.
