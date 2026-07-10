"""Keep only the dominant speaker's audio from a multi-speaker clip.

Turns interview/compilation sources into usable single-speaker voice-cloning
audio (see docs/voice-cloning-tips.md). Run it on demucs-separated vocals.

Usage: python filter_speaker.py IN.wav OUT.wav [MAX_SECONDS]

Dependencies (own venv recommended):
    pip install resemblyzer scikit-learn soundfile "setuptools<81"
    # setuptools<81 is required: newer versions drop pkg_resources,
    # which resemblyzer's webrtcvad dependency still imports.

Sliding-window d-vector embeddings (resemblyzer) -> KMeans(k=2) ->
keep the cluster with the most total duration (the compilation's host),
drop windows near the decision boundary, stitch with short crossfades.
Assumes the target voice dominates the clip; for a 50/50 conversation,
pick a different source or seed with a reference clip instead.
"""
import sys

import numpy as np
import soundfile as sf
from resemblyzer import VoiceEncoder, preprocess_wav
from sklearn.cluster import KMeans

SR = 16000

def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    max_s = float(sys.argv[3]) if len(sys.argv) > 3 else 180.0

    wav = preprocess_wav(in_path)  # mono 16k, normalized, trimmed silences
    encoder = VoiceEncoder("cpu")
    _, partials, splits = encoder.embed_utterance(wav, return_partials=True, rate=2.0)

    km = KMeans(n_clusters=2, n_init=10, random_state=0).fit(partials)
    labels = km.labels_
    # dominant cluster by total window count (host dominates a compilation)
    dom = int(np.bincount(labels).argmax())
    centroid = km.cluster_centers_[dom]
    centroid /= np.linalg.norm(centroid)
    sims = partials @ centroid  # partials are L2-normalized

    # keep confident dominant-speaker windows only
    keep = (labels == dom) & (sims > np.percentile(sims[labels == dom], 30))

    # merge adjacent kept windows into spans
    spans: list[list[int]] = []
    for k, sl in zip(keep, splits):
        if not k:
            continue
        s, e = sl.start, sl.stop
        if spans and s <= spans[-1][1] + int(0.2 * SR):
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    # drop very short spans (< 1.2 s) — likely boundary junk
    spans = [sp for sp in spans if sp[1] - sp[0] > int(1.2 * SR)]

    fade = int(0.05 * SR)
    ramp = np.linspace(0.0, 1.0, fade)
    pieces = []
    total = 0
    for s, e in spans:
        seg = wav[s:e].copy()
        seg[:fade] *= ramp
        seg[-fade:] *= ramp[::-1]
        pieces.append(seg)
        total += len(seg)
        if total >= max_s * SR:
            break
    if not pieces:
        raise SystemExit("no spans survived filtering")
    out = np.concatenate(pieces)
    sf.write(out_path, out, SR)
    kept_s = len(out) / SR
    print(
        f"kept {kept_s:.1f}s across {len(pieces)} spans "
        f"(dominant cluster {int(np.bincount(labels)[dom])}/{len(labels)} windows)"
    )

if __name__ == "__main__":
    main()
