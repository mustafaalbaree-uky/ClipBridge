"""
Voice note processing shared by the Windows and Mac clients.

The paradigm is ported from ClipKeyboard (the author's iPhone app):
long recordings are split at the quietest moment near each target cut
so a word is never sliced in half, and uploads retry with exponential
backoff on transient failures. Unlike ClipKeyboard, chunks upload in
parallel.

There is no silence trim. A fixed loudness cut throws away real speech
along with the quiet, not just dead air: a sentence's soft trailing
words, or a speaker sitting farther from the microphone in a meeting
room, both read as silence and disappear from the transcript. Chunking
already caps each upload at about 45 seconds, so trimming would only
be shrinking uploads that are already small, and that is not worth the
missing words.

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
    """Chunk if long, upload (in parallel when chunked), and return the
    stitched transcript. Raises RuntimeError on failure."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return ''

    chunks = split_chunks(audio, sr)
    wavs = [_wav_bytes(c, sr) for c in chunks]

    if len(wavs) == 1:
        texts = [_post(wavs[0], worker_url, model, language)]
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            texts = list(pool.map(
                lambda w: _post(w, worker_url, model, language), wavs))

    return ' '.join(t for t in texts if t).strip()
