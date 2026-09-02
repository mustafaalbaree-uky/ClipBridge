"""
Voice note processing shared by the Windows and Mac clients.

The paradigm is ported from ClipKeyboard (the author's iPhone app):
silence is truncated before upload (20 ms RMS windows, about -38 dBFS
threshold, 120 ms of padding kept around speech), long recordings are
split at the quietest moment near each target cut so a word is never
sliced in half, and uploads retry with exponential backoff on transient
failures. Unlike ClipKeyboard, chunks upload in parallel.

The thresholds are scaled for desktop dictation and tuned for latency:
anything at or under a minute goes up as a single request (no seams at
all), longer audio is divided into equal pieces of about 45 seconds
that upload and transcribe in parallel. Equal pieces matter because the
slowest piece sets the total wait; each cut still snaps to the quietest
nearby moment so a word is never sliced in half.
"""
import io
import math
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
import soundfile as sf

# Silence trimming (ClipKeyboard SilenceTrimmer constants)
TRIM_WINDOW_SEC  = 0.02    # loudness measured per 20 ms window
TRIM_THRESHOLD   = 0.012   # about -38 dBFS, the loudest the cut may sit
TRIM_PAD_SEC     = 0.12    # keep this much around speech
TRIM_MIN_GAIN    = 0.25    # skip trimming if it saves less than this
# ClipKeyboard could use a fixed threshold because an iPhone applies its own
# input gain, so dictation always arrives at roughly the same level. A desktop
# microphone does not: the system input slider, the distance to the laptop and
# a headset's own gain all move it, and at a low setting ordinary speech peaks
# under -38 dBFS in its entirety. A fixed cut then reads the whole note as
# silence and deletes it. So the cut is placed relative to the note's own
# speech level as well, and never above the fixed value.
TRIM_REL         = 0.3     # cut this far below the note's own speech level
# Trimming exists to shrink an upload, not to decide what was said. When it
# wants to throw away more than this, its idea of the level is far likelier to
# be wrong than the recording is to be that empty, and the cost of being wrong
# is silent: a scrap of noise makes Whisper invent a sentence rather than fail.
TRIM_MAX_DROP    = 0.8

# Chunking (ClipKeyboard AudioChunker, tuned so the pieces are equal and
# short enough that parallel uploads actually pay off)
SINGLE_LIMIT_SEC = 60      # at or below this, one upload
TARGET_CHUNK_SEC = 45      # divide into equal pieces of at most about this
SNAP_WINDOW_SEC  = 5       # snap cuts to the quietest 100 ms within this
QUIET_WIN_SEC    = 0.1
MIN_CUT_GAP_SEC  = 15      # a snapped cut may not land before this gap

MAX_WORKERS      = 8
ATTEMPTS         = 3


def _window_rms(audio, win):
    """RMS per non overlapping window of `win` samples."""
    n = len(audio)
    count = (n + win - 1) // win
    padded = np.zeros(count * win, dtype=np.float32)
    padded[:n] = audio
    frames = padded.reshape(count, win)
    return np.sqrt(np.mean(frames * frames, axis=1))


def trim_silence(audio, sr):
    """Drop near silent stretches, keeping padding around speech.
    Returns the original array when there is no speech at all or when
    trimming would not meaningfully shrink the audio."""
    n = len(audio)
    win = max(1, int(sr * TRIM_WINDOW_SEC))
    pad = int(sr * TRIM_PAD_SEC)

    rms = _window_rms(audio, win)
    # The 95th percentile stands in for how loud this note's speech is: high
    # enough to sit inside speech rather than on a stray peak, low enough that
    # a note which is mostly pause still reports the speaking level.
    level = float(np.percentile(rms, 95))
    threshold = min(TRIM_THRESHOLD, level * TRIM_REL)

    loud = rms >= threshold
    idx = np.flatnonzero(loud)
    if idx.size == 0:
        return audio

    # consecutive loud windows -> runs -> padded sample ranges -> merge
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends   = np.concatenate((idx[breaks], [idx[-1]]))

    ranges = []
    for s, e in zip(starts, ends):
        lo = max(0, s * win - pad)
        hi = min(n, (e + 1) * win + pad)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])

    kept = sum(hi - lo for lo, hi in ranges)
    if kept >= n - int(sr * TRIM_MIN_GAIN):
        return audio      # saves too little to be worth the work
    if kept < n * (1.0 - TRIM_MAX_DROP):
        return audio      # wants to cut too much to be believed
    return np.concatenate([audio[lo:hi] for lo, hi in ranges])


def _quiet_point(audio, sr, target):
    """Seconds offset of the quietest 100 ms within the snap window
    around `target` seconds, or None."""
    lo = max(0, int((target - SNAP_WINDOW_SEC) * sr))
    hi = min(len(audio), int((target + SNAP_WINDOW_SEC) * sr))
    if hi <= lo:
        return None
    win = max(1, int(sr * QUIET_WIN_SEC))
    rms = _window_rms(audio[lo:hi], win)
    return (lo + int(np.argmin(rms)) * win) / sr


def split_chunks(audio, sr):
    """Ordered list of arrays to upload. A single element means the
    whole recording goes up as one request.

    Long audio is divided into pieces of equal length (the fewest that
    keeps each at or under the target), because the pieces run in
    parallel and the slowest one sets the total wait. Each nominal cut
    then snaps to the quietest nearby moment."""
    total = len(audio) / sr
    if total <= SINGLE_LIMIT_SEC:
        return [audio]

    n = math.ceil(total / TARGET_CHUNK_SEC)
    cuts = [0.0]
    for i in range(1, n):
        target = total * i / n
        snapped = _quiet_point(audio, sr, target)
        cut = max(snapped if snapped is not None else target,
                  cuts[-1] + MIN_CUT_GAP_SEC)
        cuts.append(min(cut, total))
    cuts.append(total)

    chunks = [audio[int(a * sr):int(b * sr)]
              for a, b in zip(cuts, cuts[1:]) if b - a > 0.5]
    return chunks or [audio]


def _wav_bytes(audio, sr):
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format='WAV', subtype='PCM_16')
    buf.seek(0)
    return buf


def _post(wav, worker_url, model, language):
    """One upload with retry and backoff on transient failures
    (ClipKeyboard Transcriber.send)."""
    last = 'no attempts made'
    for attempt in range(ATTEMPTS):
        try:
            wav.seek(0)
            res = requests.post(
                worker_url,
                files={'file': ('audio.wav', wav, 'audio/wav')},
                data={'model': model, 'language': language},
                timeout=60,
            )
            if res.ok:
                return res.json().get('text', '').strip()
            last = f'HTTP {res.status_code}'
            if res.status_code not in (403, 408, 429) and res.status_code < 500:
                break
        except requests.RequestException as e:
            last = str(e)
        if attempt < ATTEMPTS - 1:
            time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError(f'transcription failed: {last}')


def transcribe_note(audio, sr, worker_url,
                    model='whisper-large-v3-turbo', language='en'):
    """Silence trim, chunk if long, upload (in parallel when chunked),
    and return the stitched transcript. Raises RuntimeError on failure."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return ''

    audio = trim_silence(audio, sr)
    chunks = split_chunks(audio, sr)
    wavs = [_wav_bytes(c, sr) for c in chunks]

    if len(wavs) == 1:
        texts = [_post(wavs[0], worker_url, model, language)]
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            texts = list(pool.map(
                lambda w: _post(w, worker_url, model, language), wavs))

    return ' '.join(t for t in texts if t).strip()
