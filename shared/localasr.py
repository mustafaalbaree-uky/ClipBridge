"""
Transcription on this machine, through whisper.cpp.

The worker was never the fast part. Groq serves whisper-large-v3-turbo,
and that is the same model whisper.cpp runs from a file on disk, so the
text that comes back is the same text. What the worker adds is an
upload, a key, and a wait that grows without bound when the wifi is
poor: a note recorded on a bad connection sat in `pending` reporting
"transcribing" for as long as the request took to give up, and the
retry sweep stops at the first note that still fails, so everything
behind it stopped too.

Run locally none of that exists. Measured on an M3 Pro with the Metal
backend, a 2 minute 54 second note came back in 7.2 seconds and a 2
second note in 1.2, both including loading the 1.6 GB model.

whisper.cpp does its own windowing over long audio, so nothing here
chunks anything. That is what noteproc is for, and it is only needed
on the path that still uploads.

The recorder already writes 16 kHz mono PCM16, which is whisper.cpp's
native input, so the file goes to the binary untouched.
"""
import re
import shutil
import subprocess
import wave
from pathlib import Path

MODEL_NAME = 'ggml-large-v3-turbo.bin'

# Where the model is looked for, in order. ClipBridge's own directory comes
# first so it is not at the mercy of another project's cache, and lecpipe's
# copy is second because it is already on this machine and downloading a
# second 1.6 GB of identical weights buys nothing.
MODEL_PATHS = [
    Path.home() / '.clipbridge' / 'models' / MODEL_NAME,
    Path.home() / '.cache' / 'lecpipe' / 'models' / MODEL_NAME,
]

# whisper-cpp is the Homebrew formula. It installed as whisper-cpp for a
# while and as whisper-cli since, and both names are still out there.
CLI_NAMES = ['whisper-cli', 'whisper-cpp']
CLI_PATHS = [Path('/opt/homebrew/bin/whisper-cli'),
             Path('/usr/local/bin/whisper-cli')]

LANGUAGE = 'en'

# whisper.cpp hands the text it has already produced back to itself as
# context for the next window. That is what makes it repeat: once a
# sentence has come out twice it sits in its own context twice, and the
# decoder can stay there emitting it until the audio runs out. The audio
# under a loop is never transcribed at all, so the duplication is not the
# damage, it is the symptom. A four minute note on 17 Sep 2026 lost about
# a minute that way, under thirteen copies of one sentence, and nothing in
# the transcript said anything was missing.
#
# Capping the carried context leaves room for a sentence to run across a
# window boundary and not enough for a loop to feed itself. Zero also
# stops it, but zero means no prompt tokens either, which silently turns
# VOCABULARY below into nothing.
MAX_CONTEXT = 64

# Beyond about 25x realtime the run has hung rather than gone slowly, and
# the constant part covers loading the model on a cold page cache.
TIMEOUT_BASE_SEC  = 60
TIMEOUT_PER_SEC   = 1.0

# whisper.cpp narrates what it hears when it is not speech: [BLANK_AUDIO],
# (keyboard clicking), [Music]. That is a description of the recording, not
# something that was said, and it has no business landing on the clipboard.
_NON_SPEECH = re.compile(r'^\s*[\[(][^\])]*[\])]\s*$')

_cli_hint = None
_model_hint = None
_vocab = None
_cli_found = False
_model_found = False
_cli = None
_model = None


def configure(cli_path=None, model_path=None, vocabulary=None):
    """Point at a binary or a model other than the ones found by looking.
    Both come from the config file, and either being set skips the search
    for that one.

    `vocabulary` is the words whisper cannot get from the audio: a product
    name, a person, an acronym spoken as letters. It goes in as the initial
    prompt and is put back at the front of every window, because with the
    context capped it would otherwise be forgotten a few seconds in. Keep it
    to a sentence or two. It shares MAX_CONTEXT with the running text, and a
    long one crowds that out.
    """
    global _cli_hint, _model_hint, _vocab, _cli_found, _model_found
    _cli_hint = cli_path or None
    _model_hint = model_path or None
    _vocab = (vocabulary or '').strip() or None
    _cli_found = _model_found = False


def binary():
    """The whisper.cpp executable, or None when it is not installed."""
    global _cli, _cli_found
    if _cli_found:
        return _cli
    _cli_found = True
    _cli = None
    candidates = []
    if _cli_hint:
        candidates.append(Path(_cli_hint).expanduser())
    candidates += [Path(p) for name in CLI_NAMES
                   if (p := shutil.which(name))]
    candidates += CLI_PATHS
    for path in candidates:
        try:
            if path.is_file():
                _cli = path
                break
        except Exception:
            pass
    return _cli


def model():
    """The model file, or None when it has not been fetched."""
    global _model, _model_found
    if _model_found:
        return _model
    _model_found = True
    _model = None
    candidates = ([Path(_model_hint).expanduser()] if _model_hint else []) \
        + MODEL_PATHS
    for path in candidates:
        try:
            if path.is_file():
                _model = path
                break
        except Exception:
            pass
    return _model


def available():
    """Whether a note can be transcribed without the network."""
    return binary() is not None and model() is not None


def describe():
    """One line for the debug log, saying which half is missing when this
    machine cannot do it locally. Written for the log rather than the user,
    who is told nothing at all as long as the transcript arrives."""
    if available():
        vocab = f', vocabulary {len(_vocab)} chars' if _vocab else ''
        return f'on, {binary()} + {model().name}{vocab}'
    if binary() is None and model() is None:
        return 'off, no whisper.cpp and no model'
    if binary() is None:
        return 'off, no whisper.cpp (brew install whisper-cpp)'
    return f'off, no {MODEL_NAME} in {MODEL_PATHS[0].parent}'


def _duration(path):
    """Seconds of audio, for sizing the timeout. Zero when the header will
    not read, which leaves the base timeout to cover it."""
    try:
        with wave.open(str(path), 'rb') as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except Exception:
        return 0.0


def _clean(out):
    """Join what came back, dropping the lines that describe the recording
    instead of quoting it."""
    lines = [ln.strip() for ln in out.splitlines()]
    kept = [ln for ln in lines if ln and not _NON_SPEECH.match(ln)]
    return ' '.join(kept).strip()


def transcribe(path, language=LANGUAGE):
    """Transcribe an audio file and return the text. Raises RuntimeError,
    matching noteproc, so the caller cannot tell the two apart."""
    cli, mdl = binary(), model()
    if cli is None or mdl is None:
        raise RuntimeError('local transcription is not set up')
    timeout = TIMEOUT_BASE_SEC + TIMEOUT_PER_SEC * _duration(path)
    cmd = [str(cli), '-m', str(mdl), '-f', str(path),
           '-l', language, '-nt', '-np', '-mc', str(MAX_CONTEXT)]
    if _vocab:
        cmd += ['--prompt', _vocab, '--carry-initial-prompt']
    try:
        # whisper.cpp writes UTF-8. Said explicitly, because the bundled app
        # runs with an ASCII default, and one character outside it (ﷺ, an
        # accented name) failed the whole note on every retry.
        res = subprocess.run(cmd, capture_output=True, encoding='utf-8',
                             errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'whisper.cpp gave up after {int(timeout)}s')
    except Exception as e:
        raise RuntimeError(f'whisper.cpp could not run: {e}')
    if res.returncode != 0:
        tail = (res.stderr or '').strip().splitlines()
        detail = tail[-1] if tail else f'exit {res.returncode}'
        raise RuntimeError(f'whisper.cpp failed: {detail}')
    return _clean(res.stdout)
