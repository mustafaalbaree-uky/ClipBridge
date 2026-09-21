"""
ClipBridge, Mac menu bar client.

Polls the shared Supabase table for clips addressed to this Mac, copies
them to the clipboard, and shows a notification. Sending is instant:
whatever is on the clipboard right now goes out, no dialog.

Left click on the menu bar icon: send the clipboard to the PC.
Right click (or control click): open the menu.
Global hotkey (default ctrl+alt+space): toggle voice note recording. The
recording starts the instant the microphone comes up, the same hotkey
stops it, and the transcript lands on the clipboard only. Nothing is
pushed to the other devices unless you send it afterwards. The hotkey is registered
through Carbon (RegisterEventHotKey), so it needs no Input Monitoring
permission and works everywhere, including Terminal. While recording the
menu bar icon becomes a red dot; while transcribing, an ellipsis. A small
on screen pill (hud.py) also appears for the whole voice note flow: a
frosted slate capsule ringed in drifting water colour, slow while
recording with an elapsed counter, fast while transcribing. When the
transcript lands it opens into two lines: a quiet "copied to clipboard"
over the first few words of what you said, which rise into place under a
halo with a slow sheen crossing them. It rides just below the mouse pointer and follows
it, staying clamped inside the screen the pointer is on. Voice note
status never uses notification banners; those are kept for clipboard
sync only.

Notes can overlap. Pressing the hotkey while a note is still transcribing
starts the next one, and its pill appears under the pointer while the
older pills slide down beneath it, as many as you like. Transcripts reach
the clipboard in the order they were recorded, one at a time: the oldest
note not yet pasted holds the clipboard, and a note that finishes behind
it waits in its pill, dimmed and captioned "queued", until that one is
pasted. Then it takes the clipboard. A paste is seen without watching the
keyboard: the transcript goes on the clipboard as a promise, macOS asks
ClipBridge for the text at the moment something pastes it, and that request
is the paste. If something else is copied over a waiting note instead, the
queue stops there, and Next Note in the menu moves it on.

A note can be pinned to a text field instead (pin.py): the pin hotkey, or
control option click on a field, types a marker such as ⟦note 1⟧ at the
cursor, and the transcript replaces the marker when it arrives, wherever
you have gone since. A note that is already transcribed is typed at the
cursor with no marker. In a terminal the marker is swapped by keystrokes
once the screen shows the cursor sitting right after it; when that cannot
be seen the marker stays, and pin_hook.py gives Claude Code the transcript
when the prompt is sent. A pin that goes nowhere puts its note back in the
clipboard queue.

Recording is done by AVAudioRecorder, which writes straight into
~/.clipbridge/pending/partial, so the audio is on disk while it is still
being spoken rather than only after the fact, and moves up into
~/.clipbridge/pending the moment recording stops. Once it transcribes, the
audio moves to ~/.clipbridge/notes and its text is written beside it as a
.txt of the same name, so a transcript can be put back on the clipboard
without spending another upload, and a transcript that came back wrong can
be run again from the audio it came from. Five notes are kept, so filing a
sixth deletes the oldest, audio and text together. Anything left in
pending is a note whose upload failed, so a note recorded offline, or one
interrupted by a crash, survives. A background sweep retries them on its own
and files each one under notes the moment it works.

Menu:
    Send to PC       push the current clipboard to the PC, instantly
    Fetch Now        one shot fetch of the latest incoming clip
    Record Note      toggle recording (same as the hotkey)
    Next Note        skip the queued note on the clipboard, or restart a
                     queue stopped by something else being copied
    Recent Notes     the last five, by date and time; click one to copy
                     its transcript back to the clipboard
    Notes Folder     open the folder holding transcribed recordings
    Retry Pending    transcribe saved notes now instead of waiting
    Auto: ON         toggle background polling
    Quit

Voice notes are transcribed on this machine by whisper.cpp, through
shared/localasr.py. It runs the same model the worker does, so the text
is the same and the wifi cannot hold it up. The worker path
(shared/noteproc.py: long audio chunked at quiet points and uploaded in
parallel) is what runs when whisper.cpp or its model is not installed.

Run directly:   .venv/bin/python3 clipbridge.py
Build the app:  ./build.sh  (output: dist/ClipBridge.app)

Config search order: ~/.clipbridge/config.json, then config.json next to
the repo root. See config.example.json.
"""
import json
import subprocess
import sys
import threading
import time
import tempfile
from datetime import date
from pathlib import Path

import rumps
import requests

try:
    import objc
    from Foundation import NSObject, NSMakePoint
    from AppKit import (NSApp, NSEventTypeRightMouseUp, NSEventMaskLeftMouseUp,
                        NSEventMaskRightMouseUp, NSEventModifierFlagControl,
                        NSPasteboard, NSPasteboardItem, NSPasteboardTypeString)
    from PyObjCTools import AppHelper
    HAS_APPKIT = True
except Exception:
    HAS_APPKIT = False

try:
    import numpy as np
    import soundfile as sf
    import AVFoundation as _avf
    from AVFoundation import AVAudioRecorder
    from Foundation import NSURL
    HAS_AUDIO = True
except Exception:
    HAS_AUDIO = False

try:
    # Carbon hotkey registration: system level, needs no Input Monitoring
    # permission, and keeps firing even in Terminal with Secure Keyboard
    # Entry on (it is the same mechanism Spotlight's cmd+space uses)
    from quickmachotkey import quickHotKey, mask as _hk_mask
    import quickmachotkey.constants as _hk_const
    HAS_HOTKEY = True
except Exception:
    HAS_HOTKEY = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))
try:
    import noteproc
except Exception:
    noteproc = None

try:
    import localasr
except Exception:
    localasr = None

try:
    from hud import RecordingHUD, PillStack
except Exception:
    RecordingHUD = PillStack = None

try:
    import loginitem
except Exception:
    loginitem = None

try:
    import pin
except Exception:
    pin = None


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config():
    candidates = [
        Path.home() / '.clipbridge' / 'config.json',
        Path(__file__).resolve().parent.parent / 'config.json',
    ]
    for path in candidates:
        try:
            if path.is_file():
                # utf-8-sig also swallows a BOM left by Windows editors
                return json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            pass
    return None


_cfg = _load_config()
if not _cfg or 'supabase_url' not in _cfg or 'supabase_anon_key' not in _cfg:
    subprocess.run(['osascript', '-e',
        'display alert "ClipBridge" message "No config found. Copy '
        'config.example.json to ~/.clipbridge/config.json and fill in '
        'your Supabase URL and anon key."'])
    raise SystemExit(1)

SUPA_URL     = _cfg['supabase_url'].rstrip('/')
SUPA_ANON    = _cfg['supabase_anon_key']
SUPA_HEADERS = {'apikey': SUPA_ANON, 'Authorization': f'Bearer {SUPA_ANON}'}
POLL_SEC     = int(_cfg.get('poll_seconds', 3))
WORKER_URL   = _cfg.get('transcribe_worker_url', '')
if localasr is not None:
    localasr.configure(_cfg.get('whisper_cli_path'),
                       _cfg.get('whisper_model_path'),
                       _cfg.get('whisper_vocabulary'))
LOCAL_ASR    = localasr is not None and localasr.available()
HOTKEY       = _cfg.get('record_hotkey', '<ctrl>+<alt>+space')
PIN_HOTKEY   = _cfg.get('pin_hotkey', '<ctrl>+<alt>+v')
SAMPLERATE   = 16000

# Recordings waiting on a transcription that has not succeeded yet. Bounded
# so a worker that stays down cannot quietly fill the disk.
PENDING_DIR       = Path.home() / '.clipbridge' / 'pending'
# A recording in progress is written here instead, so the retry sweep, which
# globs PENDING_DIR itself and not below it, cannot pick up a note that is
# still being spoken. It has to keep a real .wav suffix: AVAudioRecorder picks
# its container from the file extension, and an extension it does not know
# gets it a CAF file, which then wore a .wav name once it was moved.
PARTIAL_DIR       = PENDING_DIR / 'partial'
# Where a note goes once its text has come back. The audio outlives the
# transcript so a bad one can be run again, rather than the recording being
# the thing that is lost.
NOTES_DIR         = Path.home() / '.clipbridge' / 'notes'
# One file per pinned note, named by its marker number, read by pin_hook.py.
PINS_DIR          = Path.home() / '.clipbridge' / 'pins'
PINS_KEEP_DAYS    = 2
PIN_ABANDON_SEC   = 3600
# How many notes survive, newest first. The transcript is written beside the
# audio, so a note is the pair and they are pruned together.
NOTES_KEEP        = 5
PENDING_MAX_FILES = 40
PENDING_MAX_DAYS  = 7
RETRY_SEC         = 60
RETRY_FIRST_SEC   = 15
# How long to wait for the microphone to come up before telling the user it
# is not answering. A cold open runs under a second, and a wedged one never
# returns at all, so this is set well clear of the slow end: waiting longer
# costs nothing in the case it exists to catch.
OPEN_TIMEOUT_SEC  = 8.0
# Shorter than this and there is no note in it, so it is treated as an empty
# recording rather than uploaded. It has to clear the block the recorder
# preallocates, which an accidental double press leaves behind on its own.
WAV_MIN_SEC       = 0.35
# AVAudioRecorder aligns the audio to a 4 KB boundary with a filler chunk, so
# a recording of no length at all still weighs this much on disk.
WAV_HEADER_BYTES  = 4096
WAV_MIN_BYTES     = WAV_HEADER_BYTES + int(SAMPLERATE * 2 * WAV_MIN_SEC)
# How long a transcript's pill stays up when no note is waiting behind it.
COPIED_LINGER_SEC = 2.9
# How long after a paste the next note waits before taking the clipboard.
# The app that pasted already has its text by then, and this covers one
# that asks a second time for the same paste.
PASTE_SETTLE_SEC  = 0.3
# How often the clipboard is checked for something else copied over a note
# that is waiting to be pasted.
CLIP_WATCH_SEC    = 0.5

# Either engine will do. The worker needs noteproc to chunk for it;
# whisper.cpp does its own windowing and needs nothing but the file.
CAN_RECORD = HAS_AUDIO and (
    LOCAL_ASR or (bool(WORKER_URL) and noteproc is not None))


def _transcribe(path):
    """The text of a recording. whisper.cpp on this machine when it is
    installed, and the worker only when it is not, because the two run the
    same model and only one of them can be held up by the network."""
    if LOCAL_ASR:
        return localasr.transcribe(str(path))
    if not (WORKER_URL and noteproc is not None):
        raise RuntimeError('no way to transcribe: no whisper.cpp, no worker')
    audio, sr = sf.read(str(path), dtype='float32')
    return noteproc.transcribe_note(audio, sr, WORKER_URL)


# ── Menu bar icon ──────────────────────────────────────────────────────────────

def _write_png(nsimage, suffix):
    from AppKit import NSBitmapImageRep
    tiff = nsimage.TIFFRepresentation()
    rep  = NSBitmapImageRep.imageRepWithData_(tiff)
    data = rep.representationUsingType_properties_(4, {})  # 4 = PNG
    path = tempfile.mktemp(suffix=suffix)
    data.writeToFile_atomically_(path, True)
    return path


def _render_symbol(name):
    """Render an SF Symbol to a temp PNG so rumps can use it as a template
    image (adapts to light and dark menu bars).

    The symbol's native raster is tiny, which looked blurry once the menu
    bar scaled it up on retina screens, so draw the vector symbol into a
    large image first and let rumps scale it down."""
    try:
        from AppKit import NSImage
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, 'ClipBridge')
        try:
            from AppKit import NSImageSymbolConfiguration
            cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                40.0, 0.0)
            img = img.imageWithSymbolConfiguration_(cfg)
        except Exception:
            pass
        w, h = img.size().width, img.size().height
        scale = 40.0 / max(w, h, 1)
        W, H = max(1, round(w * scale)), max(1, round(h * scale))
        out = NSImage.alloc().initWithSize_((W, H))
        out.lockFocus()
        img.drawInRect_fromRect_operation_fraction_(
            ((0, 0), (W, H)), ((0, 0), (0, 0)), 2, 1.0)  # 2 = source over
        out.unlockFocus()
        return _write_png(out, 'Template.png')
    except Exception:
        return None


def _render_record_dot():
    """A red filled circle, shown as the menu bar icon while recording.
    Not a template image, so it stays red in any menu bar theme."""
    try:
        from AppKit import NSImage, NSColor, NSBezierPath
        S = 36
        img = NSImage.alloc().initWithSize_((S, S))
        img.lockFocus()
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.27, 0.23, 1.0).set()
        NSBezierPath.bezierPathWithOvalInRect_(((8, 8), (S - 16, S - 16))).fill()
        img.unlockFocus()
        return _write_png(img, 'Rec.png')
    except Exception:
        return None


_ICON_PATH = _render_symbol('doc.on.clipboard')
_BUSY_PATH = _render_symbol('ellipsis')
_REC_PATH  = _render_record_dot()


# ── Supabase ───────────────────────────────────────────────────────────────────

def _fetch():
    """Latest clip addressed to this Mac. Returns (ok, id, content).

    `ok` says the database answered. An empty table is an answer, so it
    comes back as (True, None, None), while a failed request comes back as
    (False, None, None). The poll loop has to tell those two apart: it
    seeds itself from the table at launch, and a network blip read as an
    empty table would seed on the next real clip instead of copying it.
    """
    try:
        res = requests.get(
            f'{SUPA_URL}/rest/v1/clips',
            headers=SUPA_HEADERS,
            params={'select': 'id,content',
                    # 'pc-to-mac' is the desktop app's directed label; the
                    # QR-Bridge web page tags everything it pushes 'pc'.
                    'or': '(source.eq.pc-to-mac,source.eq.pc)',
                    'order': 'created_at.desc', 'limit': '1'},
            timeout=10,
        )
        if not res.ok:
            return False, None, None
        rows = res.json()
        if rows:
            return True, rows[0]['id'], rows[0]['content']
        return True, None, None
    except Exception:
        return False, None, None


def _push(content):
    try:
        expires_at = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                   time.gmtime(time.time() + 24 * 3600))
        res = requests.post(
            f'{SUPA_URL}/rest/v1/clips',
            headers={**SUPA_HEADERS, 'Content-Type': 'application/json',
                     'Prefer': 'return=minimal'},
            json={'content': content, 'expires_at': expires_at,
                  'source': 'mac-to-pc'},
            timeout=10,
        )
        return res.ok
    except Exception:
        return False


# ── Clipboard and notifications ────────────────────────────────────────────────

def _copy(text):
    subprocess.run(['pbcopy'], input=text.encode())


def _paste():
    try:
        out = subprocess.run(['pbpaste'], capture_output=True, timeout=5)
        return out.stdout.decode(errors='replace')
    except Exception:
        return ''


def _notify(message, title='Clip from PC'):
    preview = message[:100].replace('\\', '\\\\').replace('"', '\\"')
    subprocess.run(['osascript', '-e',
        f'display notification "{preview}" with title "{title}"'])


def _on_main(fn, *args):
    """Run fn on the main thread, or here and now if AppKit is not around."""
    try:
        AppHelper.callAfter(fn, *args)
    except Exception:
        fn(*args)


def _debug_line(msg):
    try:
        with open(Path.home() / '.clipbridge' / 'mac_debug.log', 'a', encoding='utf-8') as f:
            f.write(f'\n{msg} {time.strftime("%H:%M:%S")}')
    except Exception:
        pass




def _parse_hotkey(spec):
    """'<ctrl>+<alt>+r' -> (virtualKey, modifierMask), or None if the spec
    is not understood. Letters, digits, and space are supported."""
    mods = {'<cmd>': _hk_const.cmdKey, '<ctrl>': _hk_const.controlKey,
            '<alt>': _hk_const.optionKey, '<opt>': _hk_const.optionKey,
            '<shift>': _hk_const.shiftKey}
    chosen = []
    key = None
    for tok in spec.lower().split('+'):
        tok = tok.strip()
        if tok in mods:
            chosen.append(mods[tok])
        elif tok == 'space':
            key = _hk_const.kVK_Space
        elif len(tok) == 1 and (tok.isalpha() or tok.isdigit()):
            key = getattr(_hk_const, f'kVK_ANSI_{tok.upper()}', None)
        else:
            return None
    if key is None or not chosen:
        return None
    return key, _hk_mask(*chosen)


# ── Audio input ────────────────────────────────────────────────────────────────
# Recording goes through AVAudioRecorder, not PortAudio. PortAudio's macOS
# backend queries the audio unit from inside its own realtime callback, so
# stopping a stream can deadlock: the stopping thread holds the audio unit
# lock while waiting for the callback to drain, and the callback is waiting
# on that same lock. That lock is process wide, so once it happened every
# later open deadlocked too and voice notes stayed dead until the app was
# relaunched, with the app still sitting there looking perfectly healthy.
# AVAudioRecorder is Apple's own recorder, writes straight to a file, and
# has no callback of ours to race against.

kAudioFormatLinearPCM = 0x6C70636D   # 'lpcm'


def _recorder_settings():
    return {
        _avf.AVFormatIDKey:             kAudioFormatLinearPCM,
        _avf.AVSampleRateKey:           float(SAMPLERATE),
        _avf.AVNumberOfChannelsKey:     1,
        _avf.AVLinearPCMBitDepthKey:    16,
        _avf.AVLinearPCMIsFloatKey:     False,
        _avf.AVLinearPCMIsBigEndianKey: False,
    }


def _new_recording_path():
    """Where the recording in progress is written. It sits in PARTIAL_DIR,
    out of the retry sweep's reach, and moves up into PENDING_DIR the moment
    recording stops."""
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    return PARTIAL_DIR / f'note-{stamp}-{int(time.time() * 1000) % 1000:03d}.wav'


def _start_recorder(path):
    """Build a recorder writing to path and start it. Returns the recorder,
    or raises with a message worth putting in front of the user."""
    url = NSURL.fileURLWithPath_(str(path))
    rec, err = AVAudioRecorder.alloc().initWithURL_settings_error_(
        url, _recorder_settings(), None)
    if rec is None:
        detail = err.localizedDescription() if err is not None else None
        raise RuntimeError(str(detail) if detail else
                           'the recorder could not be created')
    if not rec.record():
        raise RuntimeError('the microphone refused to start')
    return rec


def _finish_recorder(rec, path):
    """Stop the recorder and turn the part file into a finished note.
    Returns the .wav path, or None when nothing usable was written."""
    try:
        rec.stop()
    except Exception:
        pass
    try:
        if not path.exists() or path.stat().st_size < WAV_MIN_BYTES:
            _discard_pending(path)
            return None
        final = PENDING_DIR / path.name
        path.rename(final)
        return final
    except Exception:
        return None


def _recover_part_files():
    """A file left in PARTIAL_DIR means the app died mid recording. Promote it
    so the retry sweep transcribes it instead of leaving it to rot. The .rec
    files are what builds before this one left behind, in PENDING_DIR itself
    and holding a CAF rather than a WAV, which soundfile reads either way."""
    for part, final in ([(p, PENDING_DIR / p.name)
                         for p in _glob(PARTIAL_DIR, '*.wav')] +
                        [(p, p.with_suffix('.wav'))
                         for p in _glob(PENDING_DIR, '*.rec')]):
        try:
            if part.stat().st_size >= WAV_MIN_BYTES:
                part.rename(final)
            else:
                part.unlink()
        except Exception:
            pass


def _glob(directory, pattern):
    try:
        return list(directory.glob(pattern))
    except Exception:
        return []


# ── Pending notes ──────────────────────────────────────────────────────────────
# A recording lives only in memory until it transcribes, so a failed upload
# used to lose it outright. Every note is now written here first and removed
# once its text comes back.

def _discard_pending(path):
    """Drop a note we no longer need. Called once its text has come back."""
    try:
        if path:
            Path(path).unlink()
    except Exception:
        pass


def _keep_note(path, text=None):
    """File a note whose text came back: the audio moves into NOTES_DIR and
    the transcript is written beside it. Keeping the text is what lets a note
    be put back on the clipboard later without another upload, and keeping the
    audio is what lets a bad transcript be run again. Falls back to deleting
    only if the move itself fails."""
    if not path:
        return None
    path = Path(path)
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        kept = NOTES_DIR / path.name
        if kept.exists():
            kept = NOTES_DIR / f'{path.stem}-{int(time.time() * 1000)}{path.suffix}'
        path.rename(kept)
        _write_note_text(kept, text)
        _prune_notes()
        return kept
    except Exception:
        _discard_pending(path)
        return None


# ── Saved notes ────────────────────────────────────────────────────────────────
# Each note is a pair: the .wav it was spoken into and a .txt of the same name
# holding what came back. The text is what the Recent Notes menu hands back to
# the clipboard; the audio is the fallback when there is no text yet, and the
# reason a note recorded before this existed can still be recovered.

def _pin_waiting(rec, now):
    """A pin still owed somewhere: its transcript has not been swapped into a
    field or read by the hook. One left alone for PIN_ABANDON_SEC is taken as
    a marker that was deleted rather than sent."""
    return (rec.get('waiting', True)
            and now - rec.get('time', 0) < PIN_ABANDON_SEC)


def _next_pin_number():
    """Back to 1 once no pin is waiting. While one is, keep counting up, so a
    marker still in an unsent prompt never names a newer note."""
    counter = PINS_DIR / 'last'
    now = time.time()
    try:
        PINS_DIR.mkdir(parents=True, exist_ok=True)
        last = int(counter.read_text(encoding='utf-8').strip() or 0)
    except Exception:
        last = 0
    waiting = False
    for f in _glob(PINS_DIR, '*.json'):
        try:
            if _pin_waiting(json.loads(f.read_text(encoding='utf-8')), now):
                waiting = True
                break
        except Exception:
            continue
    number = last + 1 if waiting else 1
    try:
        counter.write_text(str(number), encoding='utf-8')
    except Exception:
        pass
    return number


def _pin_sent(number):
    """Whether pin_hook.py has handed this pin's marker to Claude Code."""
    try:
        rec = json.loads((PINS_DIR / f'{number}.json').read_text(encoding='utf-8'))
        return rec.get('waiting') is False
    except Exception:
        return False


def _write_pin(number, state, text=None, waiting=True):
    """state is pending, ready, or failed; waiting is cleared once the
    transcript has landed. pin_hook.py clears it too, when it hands a
    transcript to Claude Code. Written to a temporary name and renamed, so
    neither side ever reads half a file."""
    try:
        PINS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PINS_DIR / f'{number}.json.tmp'
        tmp.write_text(json.dumps({'state': state, 'text': text,
                                   'waiting': waiting, 'time': time.time()}),
                       encoding='utf-8')
        tmp.replace(PINS_DIR / f'{number}.json')
    except Exception as e:
        _debug_line(f'pin file {number} not written: {e}')


def _prune_pins():
    cutoff = time.time() - PINS_KEEP_DAYS * 86400
    for p in _glob(PINS_DIR, '*.json'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


def _note_text_path(wav):
    return Path(wav).with_suffix('.txt')


def _write_note_text(wav, text):
    """Save a transcript beside its audio. An empty transcript writes nothing,
    so 'the worker heard nothing' and 'this note has no text yet' stay the same
    thing: both are answered by transcribing the audio again."""
    if not text:
        return None
    try:
        out = _note_text_path(wav)
        out.write_text(text, encoding='utf-8')
        return out
    except Exception:
        return None


def _read_note_text(wav):
    """The saved transcript, or None when there is not one to read."""
    try:
        txt = _note_text_path(wav)
        if txt.is_file():
            body = txt.read_text(encoding='utf-8').strip()
            return body or None
    except Exception:
        pass
    return None


def _note_time(wav):
    """When the note was spoken, from its own name. Every recording is named
    note-YYYYMMDD-HHMMSS-mmm.wav at the moment the microphone comes up, so the
    name is the recording time and the file's mtime is when the write finished.
    A name that does not parse falls back to the mtime."""
    wav = Path(wav)
    parts = wav.stem.split('-')
    if len(parts) >= 3:
        try:
            return time.mktime(time.strptime(f'{parts[1]}-{parts[2]}',
                                             '%Y%m%d-%H%M%S'))
        except Exception:
            pass
    try:
        return wav.stat().st_mtime
    except Exception:
        return 0.0


def _note_label(ts, with_seconds=False):
    """A menu title for a note: the day and the time, never the filename.
    Recent days are named rather than dated, since 'Yesterday 9:04 AM' is the
    thing being looked for and 'note-20260908-090412-202' is not."""
    when = time.localtime(ts)
    clock = time.strftime('%-I:%M:%S %p' if with_seconds else '%-I:%M %p', when)
    # Counted in calendar days rather than elapsed seconds, so the hour a
    # clock goes forward cannot turn yesterday into a weekday name.
    days = (date.today() - date(when.tm_year, when.tm_mon, when.tm_mday)).days
    if days == 0:
        day = 'Today'
    elif days == 1:
        day = 'Yesterday'
    elif 0 < days < 7:
        day = time.strftime('%A', when)
    else:
        day = time.strftime('%b %-d,', when)
    return f'{day} {clock}'


def _list_notes():
    """Saved notes, newest first."""
    notes = [(p, _note_time(p)) for p in _glob(NOTES_DIR, '*.wav')]
    notes.sort(key=lambda pair: pair[1], reverse=True)
    return notes


def _list_pending():
    try:
        return sorted(PENDING_DIR.glob('*.wav'), key=lambda p: p.stat().st_mtime)
    except Exception:
        return []


def _prune_pending():
    """Bound the folder by age first, then by count, oldest going first."""
    files = _list_pending()
    cutoff = time.time() - PENDING_MAX_DAYS * 86400
    kept = []
    for p in files:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                continue
        except Exception:
            continue
        kept.append(p)
    excess = len(kept) - PENDING_MAX_FILES
    for p in kept[:excess] if excess > 0 else []:
        _discard_pending(p)


def _prune_notes():
    """Keep the NOTES_KEEP most recent notes and drop the rest, audio and
    transcript together. A .txt with no .wav beside it goes too, so a note
    never half survives its own pruning."""
    kept = set()
    for wav, _ in _list_notes()[:NOTES_KEEP]:
        kept.add(wav)
        kept.add(_note_text_path(wav))
    for p in _glob(NOTES_DIR, '*.wav') + _glob(NOTES_DIR, '*.txt'):
        if p in kept:
            continue
        try:
            p.unlink()
        except Exception:
            pass


# ── Click routing ──────────────────────────────────────────────────────────────
# rumps attaches the menu to the status item, which makes every click open
# it. To get "left click sends, right click opens the menu" we detach the
# menu after launch, point the status button's action at ourselves, and pop
# the menu up manually only for right clicks.

if HAS_APPKIT:
    class _StatusButtonHandler(NSObject):
        def initWithOwner_(self, owner):
            self = objc.super(_StatusButtonHandler, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def statusItemClicked_(self, sender):
            self._owner._handle_status_click()

    class _ClipPromise(NSObject, protocols=[
            objc.protocolNamed('NSPasteboardItemDataProvider')]):
        """A transcript on the clipboard that is handed over only when
        something pastes it, so the handing over is the paste."""

        def initWithOwner_note_(self, owner, note):
            self = objc.super(_ClipPromise, self).init()
            if self is None:
                return None
            self._owner  = owner
            self._note   = note
            self._served = False
            return self

        def pasteboard_item_provideDataForType_(self, pasteboard, item, type_):
            text = self._note.text or ''
            # a note behind this one means both are headed for the same
            # paste, one after the other, so give them a space rather than
            # running the two transcripts together
            if text and self._owner._behind_head():
                text += ' '
            item.setString_forType_(text, type_)
            if not self._served:
                self._served = True
                AppHelper.callLater(PASTE_SETTLE_SEC,
                                    self._owner._note_pasted, self._note)


# ── Notes in flight ────────────────────────────────────────────────────────────

class _Note:
    """One voice note between the hotkey and the clipboard, with the pill
    that shows where it is."""
    __slots__ = ('pill', 'state', 'text', 'held', 'pin', 'missed')

    def __init__(self, pill):
        self.pill  = pill
        self.state = 'recording'     # recording, transcribing, ready, copied
        self.text  = None
        # a copied note whose pill is kept up because a note is behind it
        self.held  = False
        # set when the transcript goes into a text field instead of the queue
        self.pin   = None
        self.missed = False     # a pin that went nowhere, now on the clipboard


class _NoPill:
    """Stands in for a pill when the HUD could not be built, so the queue
    runs the same way and reports through notifications instead."""
    on_gone = None

    def recording(self):
        pass

    def processing(self):
        pass

    def queued(self, transcript):
        pass

    def pasted(self, transcript, caption='pasted', seconds=COPIED_LINGER_SEC):
        _notify(transcript, title=caption)
        self.hide()

    def hold(self):
        pass

    def linger(self, seconds=COPIED_LINGER_SEC):
        self.hide()

    def flash(self, text, seconds=1.8):
        _notify(text, title='ClipBridge')
        self.hide()

    def failed(self, text, seconds=3.0):
        self.flash(text, seconds)

    def copied(self, transcript, seconds=COPIED_LINGER_SEC):
        _notify(transcript, title='Copied to clipboard')
        if seconds is not None:
            self.hide()

    def hide(self):
        callback, self.on_gone = self.on_gone, None
        if callback is not None:
            _on_main(callback)


# ── App ────────────────────────────────────────────────────────────────────────

class ClipBridge(rumps.App):
    def __init__(self):
        super().__init__('ClipBridge', icon=_ICON_PATH, template=True,
                         quit_button=None)
        self._auto_item = rumps.MenuItem('Auto: ON', callback=self._toggle_auto)
        self._login_item = rumps.MenuItem('Open at Login',
                                          callback=self._toggle_login)
        items = [
            rumps.MenuItem('Send to PC', callback=self._send_to_pc),
            rumps.MenuItem('Fetch Now',  callback=self._fetch_now),
        ]
        if CAN_RECORD:
            self._record_item = rumps.MenuItem('Record Note',
                                               callback=self._toggle_record)
            # no callback until there is a queue to move, which greys it out
            self._next_item = rumps.MenuItem('Next Note')
            self._notes_item = rumps.MenuItem('Recent Notes')
            # what the submenu currently shows, so it is only rebuilt when the
            # notes on disk have actually changed
            self._notes_shown = None
            items += [None, self._record_item, self._next_item, self._notes_item,
                      rumps.MenuItem('Notes Folder',
                                     callback=self._open_notes),
                      rumps.MenuItem('Retry Pending',
                                     callback=self._retry_now)]
        else:
            self._next_item   = None
            self._notes_item  = None
            self._notes_shown = None
        items += [
            None,
            self._auto_item,
            self._login_item,
            None,
            rumps.MenuItem('Quit', callback=lambda _: rumps.quit_application()),
        ]
        self.menu = items
        self._last_id = None
        self._seeded  = False
        self._auto    = True
        self._rec_on   = False
        self._recorder = None
        self._rec_path = None
        # A recorder that has been asked for but has not come up yet, and a
        # counter that invalidates it. Every toggle bumps the counter, so a
        # recorder that finally starts after we gave up on it is thrown away
        # instead of turning into a phantom recording.
        self._rec_opening = False
        self._rec_gen     = 0
        self._pills = None
        # every note not yet pasted, oldest first, including the one being
        # recorded. Touched on the main thread only.
        self._notes    = []
        self._rec_note = None
        self._replaying = False
        # the note on the clipboard as a promise, the pasteboard's change
        # count right after it went there, and the timer comparing the two
        self._offer_note  = None
        self._offer_count = None
        self._clip_timer  = None
        self._promises    = []
        # set when something else was copied over a waiting note, which
        # stops the queue until Next Note
        self._paused      = False
        # notes the live upload is holding, so the retry sweep leaves them be
        self._inflight      = set()
        self._inflight_lock = threading.Lock()
        # notes pinned to a text field, out of the queue, until delivered
        self._pinned    = []
        self._tap       = None
        self._sweep_lock    = threading.Lock()
        hud_note = 'off, no AppKit' if not HAS_APPKIT else \
                   'off, hud.py did not import' if PillStack is None else 'on'
        if HAS_APPKIT and PillStack is not None:
            try:
                self._pills = PillStack()
            except Exception as e:
                self._pills = None
                hud_note = f'off, {e}'
        threading.Thread(target=self._poll, daemon=True).start()
        if CAN_RECORD:
            _recover_part_files()
            _prune_notes()
            _prune_pins()
            self._rebuild_notes_menu()
            threading.Thread(target=self._retry_loop, daemon=True).start()
        self._hotkey_handle = None
        # Asked for once per launch. An install deletes /Applications/
        # ClipBridge.app and copies a new one over it, which takes the login
        # registration with it, so the intent has to be restated by the app
        # that the new bundle starts.
        login_state = loginitem.reconcile() if loginitem else 'loginitem.py did not import'
        self._sync_login_item()
        diag = {
            'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'can_record': CAN_RECORD, 'has_audio': HAS_AUDIO,
            'has_hotkey_lib': HAS_HOTKEY, 'hotkey': HOTKEY,
            'hud': hud_note, 'login_item': login_state,
            'paste_signal': 'clipboard promise' if HAS_APPKIT else 'off, no AppKit',
            'local_asr': localasr.describe() if localasr else
                         'off, localasr.py did not import',
        }
        if CAN_RECORD and HAS_HOTKEY and HOTKEY:
            try:
                parsed = _parse_hotkey(HOTKEY)
                diag['parsed'] = repr(parsed)
                if parsed:
                    vk, mods = parsed

                    @quickHotKey(virtualKey=vk, modifierMask=mods)
                    def _fire():
                        self._hotkey_fired()

                    self._hotkey_handle = _fire
                    diag['registered'] = True
            except Exception as e:
                import traceback
                diag['register_error'] = traceback.format_exc()
        self._pin_handle = None
        diag['pin'] = 'off, pin.py did not import' if pin is None else \
                      'off, no accessibility bindings' if not pin.HAS_AX else \
                      f'hotkey {PIN_HOTKEY}, control option click'
        if CAN_RECORD and HAS_HOTKEY and pin is not None and pin.HAS_AX:
            diag['accessibility'] = pin.trusted(prompt=True)
            try:
                parsed = _parse_hotkey(PIN_HOTKEY)
                if parsed:
                    vk, mods = parsed

                    @quickHotKey(virtualKey=vk, modifierMask=mods)
                    def _fire_pin():
                        _on_main(self._pin_hotkey_fired)

                    self._pin_handle = _fire_pin
            except Exception as e:
                diag['pin_register_error'] = str(e)
            self._tap = pin.ClickTap(self._pin_target, self._pin_click)
            # the tap can only be made once Accessibility is granted, which
            # may happen after launch, so keep trying until it is
            if not self._tap.start():
                self._tap_timer = rumps.Timer(self._retry_tap, 3)
                self._tap_timer.start()
        try:
            with open(Path.home() / '.clipbridge' / 'mac_debug.log', 'w', encoding='utf-8') as f:
                json.dump(diag, f, indent=2)
        except Exception:
            pass
        if HAS_APPKIT:
            self._nsmenu       = None
            self._handler      = None
            self._wire_tries   = 0
            self._wire_timer   = rumps.Timer(self._wire_click, 0.5)
            self._wire_timer.start()

    def _wire_click(self, timer=None):
        """Runs on the main thread shortly after launch, once the status
        item exists. Falls back to normal rumps behavior if it cannot."""
        self._wire_tries += 1
        try:
            item = self._nsapp.nsstatusitem
            btn  = item.button()
            if btn is None:
                raise RuntimeError('status button not ready')
            self._handler = _StatusButtonHandler.alloc().initWithOwner_(self)
            btn.setTarget_(self._handler)
            btn.setAction_('statusItemClicked:')
            btn.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
            self._nsmenu = item.menu()
            item.setMenu_(None)
            self._wire_timer.stop()
        except Exception:
            if self._wire_tries >= 20:
                self._wire_timer.stop()

    def _handle_status_click(self):
        is_right = False
        try:
            event = NSApp.currentEvent()
            if event is not None:
                if event.type() == NSEventTypeRightMouseUp:
                    is_right = True
                elif event.modifierFlags() & NSEventModifierFlagControl:
                    is_right = True
        except Exception:
            is_right = True
        if is_right:
            self._pop_menu()
        else:
            self._send_to_pc(None)

    def _pop_menu(self):
        self._rebuild_notes_menu()
        try:
            item = self._nsapp.nsstatusitem
            btn  = item.button()
            self._nsmenu.popUpMenuPositioningItem_atLocation_inView_(
                None, NSMakePoint(0, btn.bounds().size.height + 4), btn)
        except Exception:
            try:
                self._nsapp.nsstatusitem.popUpStatusItemMenu_(self._nsmenu)
            except Exception:
                pass

    def _poll(self):
        # Seed first, then poll. Whatever is already in the table at launch
        # is history and must not land on the clipboard, but an empty table
        # is just as good a starting point as a full one, so the seed is
        # taken as soon as the database answers at all.
        #
        # Seeding from inside the loop instead, on the first row it happened
        # to see, meant an empty table left the client unseeded, and then the
        # next clip to arrive was taken as the seed and silently dropped.
        # Rows are held for fifteen minutes, so an empty table is the normal
        # state and that dropped clip was very nearly every clip.
        while not self._seeded:
            ok, row_id, _ = _fetch()
            if ok:
                self._last_id = row_id
                self._seeded  = True
                break
            time.sleep(POLL_SEC)

        while True:
            try:
                if self._auto:
                    ok, row_id, content = _fetch()
                    if ok and content and row_id != self._last_id:
                        self._last_id = row_id
                        _copy(content)
                        _notify(content)
            except Exception:
                pass
            time.sleep(POLL_SEC)

    def _toggle_auto(self, item):
        self._auto = not self._auto
        item.title = f'Auto: {"ON" if self._auto else "OFF"}'

    # ── Open at login ──────────────────────────────────────────────────────

    def _toggle_login(self, _):
        """Flip the login item, or, when it has been switched off in System
        Settings, open the pane that owns that switch. Registering again would
        not lift it, so sending you to the one control that will is the only
        thing this can honestly do."""
        if loginitem is None:
            return
        if loginitem.state() == loginitem.BLOCKED:
            subprocess.run(['open', 'x-apple.systempreferences:'
                            'com.apple.LoginItems-Settings.extension'])
            return
        loginitem.set_enabled(loginitem.state() != loginitem.ON)
        self._sync_login_item()

    def _sync_login_item(self):
        """Show what macOS is doing, never what was last asked for. The two
        come apart on every install, since the registration lives and dies with
        the bundle that build.sh replaces.

        The checkmark has a third setting, and BLOCKED is what it is for: the
        item is registered and will not start, which is neither of the other
        two and should not be drawn as either."""
        if loginitem is None:
            self._login_item.set_callback(None)
            return
        state = loginitem.state()
        if state == loginitem.UNAVAILABLE:
            self._login_item.state = 0
            self._login_item.set_callback(None)
        elif state == loginitem.BLOCKED:
            self._login_item.state = -1
        else:
            self._login_item.state = 1 if state == loginitem.ON else 0

    def _fetch_now(self, _):
        def _run():
            ok, row_id, content = _fetch()
            if content:
                self._last_id = row_id
                self._seeded  = True
                _copy(content)
                _notify(content)
            elif ok:
                self._seeded = True
                _notify('Nothing waiting.', title='ClipBridge')
            else:
                _notify('Could not reach the database.', title='ClipBridge')
        threading.Thread(target=_run, daemon=True).start()

    def _send_to_pc(self, _):
        def _run():
            text = _paste().strip()
            if not text:
                _notify('Clipboard is empty.', title='ClipBridge')
                return
            ok = _push(text)
            _notify(text if ok else 'Push failed.',
                    title='Sent to PC' if ok else 'ClipBridge')
        threading.Thread(target=_run, daemon=True).start()

    # ── Voice notes ────────────────────────────────────────────────────────────

    def _hotkey_fired(self):
        try:
            with open(Path.home() / '.clipbridge' / 'mac_debug.log', 'a', encoding='utf-8') as f:
                f.write(f'\nhotkey fired {time.strftime("%H:%M:%S")}')
        except Exception:
            pass
        # recording touches UI, so make sure we are on the main thread
        _on_main(self._toggle_record, None)

    def _set_state(self, state):
        """Swap the menu bar icon: red dot while recording, ellipsis while
        transcribing, the clipboard otherwise. No overlay text, so nothing
        ever draws over neighboring menu bar items."""
        def apply():
            try:
                if state == 'rec' and _REC_PATH:
                    self.template = False
                    self.icon = _REC_PATH
                elif state == 'busy' and _BUSY_PATH:
                    self.template = True
                    self.icon = _BUSY_PATH
                else:
                    self.template = True
                    self.icon = _ICON_PATH
            except Exception:
                pass
        try:
            AppHelper.callAfter(apply)
        except Exception:
            apply()

    def _refresh_icon(self):
        """The menu bar icon for everything going on at once: a recording
        outranks a transcription, which outranks nothing."""
        if self._rec_on:
            self._set_state('rec')
        elif self._replaying or any(n.state == 'transcribing'
                                    for n in self._notes + self._pinned):
            self._set_state('busy')
        else:
            self._set_state('idle')

    def _pill(self):
        """A new pill at the top of the stack, under the pointer. Falls
        back to notifications only when the HUD could not be built at all."""
        return self._pills.acquire() if self._pills else _NoPill()

    def _hud_flash(self, text, seconds=1.8):
        """Short on screen confirmation in a pill of its own."""
        self._pill().flash(text, seconds)

    def _hud_copied(self, text):
        """The transcript is on the clipboard. Showing its opening words
        is what makes the confirmation worth reading: it says the note
        was heard, not merely that something finished."""
        self._pill().copied(text)

    def _toggle_record(self, _sender):
        if not CAN_RECORD:
            return
        if self._rec_opening:
            # Pressed again while the microphone is still coming up. Treat it
            # as "forget it", so the next press starts clean rather than
            # queueing a second recorder behind this one.
            self._rec_gen    += 1
            self._rec_opening = False
            self._refresh_icon()
            self._sync_head()
            self._hud_flash('Canceled.')
        elif not self._rec_on:
            # Nothing on this thread may touch audio. Even Apple's recorder
            # can sit waiting on CoreAudio, and this is the thread that runs
            # the menu bar, the pill, and the Carbon hotkey.
            self._rec_opening = True
            self._rec_gen    += 1
            gen = self._rec_gen
            # a note is on its way, so a transcript still showing has to
            # wait for its paste from here on
            self._sync_head()
            threading.Thread(target=self._open_worker, args=(gen,),
                             daemon=True).start()
            threading.Thread(target=self._open_watchdog, args=(gen,),
                             daemon=True).start()
        else:
            self._rec_on = False
            rec  = self._recorder
            path = self._rec_path
            note = self._rec_note
            self._recorder = None
            self._rec_path = None
            self._rec_note = None
            self._record_item.title = 'Record Note'
            note.state = 'transcribing'
            note.pill.processing()
            self._refresh_icon()
            threading.Thread(target=self._finish_note,
                             args=(note, rec, path), daemon=True).start()

    def _open_worker(self, gen):
        """Bring the recorder up off the main thread, so a microphone that
        never answers cannot take the menu bar and the hotkey down with it."""
        path = None
        try:
            path = _new_recording_path()
            rec  = _start_recorder(path)
        except Exception as e:
            _discard_pending(path)
            _on_main(self._open_failed, gen, str(e))
            return
        if gen != self._rec_gen:
            # canceled or timed out while the microphone was coming up
            _discard_pending(_finish_recorder(rec, path))
            return
        _on_main(self._open_ready, gen, rec, path)

    def _open_watchdog(self, gen):
        """Give up on a recorder that never comes up, so the app says so and
        the next press can try again instead of pressing into silence."""
        time.sleep(OPEN_TIMEOUT_SEC)
        if self._rec_gen == gen and self._rec_opening:
            _on_main(self._open_failed, gen, 'it did not respond')

    def _open_ready(self, gen, rec, path):
        if gen != self._rec_gen:
            threading.Thread(
                target=lambda: _discard_pending(_finish_recorder(rec, path)),
                daemon=True).start()
            return
        self._rec_opening = False
        self._recorder    = rec
        self._rec_path    = path
        self._rec_on      = True
        note = _Note(self._pill())
        note.pill.on_gone = lambda n=note: self._note_gone(n)
        self._rec_note = note
        self._notes.append(note)
        self._record_item.title = 'Stop Recording'
        self._refresh_icon()
        note.pill.recording()
        self._sync_head()

    def _open_failed(self, gen, message):
        if gen != self._rec_gen:
            return
        # bump, so a recorder that starts after we have written it off gets
        # thrown away by _open_worker rather than recording unannounced
        self._rec_gen    += 1
        self._rec_opening = False
        self._rec_on      = False
        self._recorder    = None
        self._rec_path    = None
        self._record_item.title = 'Record Note'
        self._refresh_icon()
        self._sync_head()
        self._pill().failed(f'Could not open the microphone: {message}')

    def _finish_note(self, note, rec, path):
        """Stop the recorder, then transcribe what it wrote. The audio is
        already on disk by the time we get here, so a failed upload loses
        nothing: the file stays and the retry sweep picks it up. What
        happens to the clipboard is the queue's call, made on the main
        thread, since another note may be ahead of this one."""
        final = None
        try:
            final = _finish_recorder(rec, path)
            if final is None:
                _on_main(self._note_failed, note, 'Nothing recorded.', 1.8)
                return
            with self._inflight_lock:
                self._inflight.add(str(final))
            text = _transcribe(final)
            # the transcript came back, so there is nothing left to retry, even
            # when it heard nothing at all. The audio moves to NOTES_DIR with
            # its text beside it, so the note can be copied again without
            # another upload and a poor transcript can be run again.
            _keep_note(final, text)
            _on_main(self._rebuild_notes_menu)
            if text:
                _on_main(self._note_ready, note, text)
            else:
                _on_main(self._note_failed, note, 'Nothing heard.', 1.8)
        except Exception as e:
            _debug_line(f'note failed: {e}')
            message = f'{e}. Saved, will retry.' if final else str(e)
            _on_main(self._note_failed, note, message, 3.0, True)
        finally:
            if final:
                with self._inflight_lock:
                    self._inflight.discard(str(final))

    # ── The queue ──────────────────────────────────────────────────────────────
    # Transcripts reach the clipboard in the order their notes were recorded.
    # The oldest note in self._notes owns the clipboard. A note that finishes
    # behind it waits, dimmed, and takes the clipboard once that one is pasted.
    # Something else copied over a waiting note stops the queue until Next Note.
    # A copied note with nothing behind it leaves the way a single pill always
    # has, after a few seconds or at the next key or click. Main thread only.

    def _behind_head(self):
        return len(self._notes) > 1 or self._rec_opening

    def _note_ready(self, note, text):
        if note.pin is not None:
            self._pin_ready(note, text)
            return
        if note not in self._notes:
            return
        note.text  = text
        note.state = 'ready'
        if note is not self._notes[0] or self._paused:
            note.pill.queued(text)
        self._advance()

    def _note_failed(self, note, message, seconds, error=False):
        if note.pin is not None:
            self._pin_failed(note)
        if note in self._notes:
            self._notes.remove(note)
        if error:
            note.pill.failed(message, seconds)
        else:
            note.pill.flash(message, seconds)
        self._advance()

    def _note_gone(self, note):
        """A note's pill left on its own. For a copied note that is the end
        of it, unless a note got in line behind it while the pill was on its
        way out: then it still owes a paste, so it comes back."""
        if note in self._pinned and note.state == 'done':
            self._pinned.remove(note)
            return
        if note not in self._notes:
            return
        if note.state == 'copied' and note.held and note is self._notes[0]:
            note.pill = self._pill()
            note.pill.on_gone = lambda n=note: self._note_gone(n)
            note.pill.copied(note.text, None)
            return
        self._notes.remove(note)
        self._advance()

    def _note_pasted(self, note):
        if note not in self._notes:
            return
        _debug_line('paste seen, next note up')
        self._notes.remove(note)
        if self._offer_note is note:
            self._offer_note = None
        note.pill.hide()
        self._advance()

    def _note_replaced(self, note):
        """Something else went on the clipboard while note was waiting to be
        pasted. Loading the next note now would throw away what was just
        copied, so the queue stops here until Next Note."""
        if note not in self._notes:
            return
        _debug_line('clipboard replaced, queue stopped')
        self._notes.remove(note)
        note.pill.hide()
        self._paused = bool(self._notes)
        self._advance()

    def _advance(self):
        """Give the clipboard to the oldest note once it is ready, unless
        the queue has stopped."""
        if not self._notes:
            self._paused = False
        head = self._notes[0] if self._notes else None
        if head is not None and head.state == 'ready' and not self._paused:
            head.state = 'copied'
            head.held  = self._behind_head()
            linger = None if head.held else COPIED_LINGER_SEC
            if head.missed:
                head.pill.pasted(head.text, 'not pinned, copied to clipboard',
                                 linger)
            else:
                head.pill.copied(head.text, linger)
            self._offer(head)
        self._sync_head()
        self._refresh_icon()

    def _sync_head(self):
        """Keep the copied note's pill up while anything is behind it, and
        let it go the usual way once nothing is."""
        head = self._notes[0] if self._notes else None
        if head is not None and head.state == 'copied':
            behind = self._behind_head()
            if behind and not head.held:
                head.held = True
                head.pill.hold()
            elif not behind and head.held:
                head.held = False
                head.pill.linger(COPIED_LINGER_SEC)
        self._sync_next_item()

    def _sync_next_item(self):
        if self._next_item is None:
            return
        head = self._notes[0] if self._notes else None
        can = head is not None and (head.state == 'copied' or self._paused)
        self._next_item.set_callback(self._next_note if can else None)

    def _next_note(self, _):
        """Move the queue on by hand: skip the note on the clipboard, or
        restart a queue that stopped because something else was copied."""
        head = self._notes[0] if self._notes else None
        if head is None:
            return
        if head.state == 'copied':
            self._note_pasted(head)
        elif self._paused:
            self._paused = False
            self._advance()

    # ── Pinned notes ───────────────────────────────────────────────────────────
    # A pin sends a note's transcript to a marker typed where it goes, and takes
    # the note out of the clipboard queue for good. In a text field pin.py swaps
    # the transcript in. Every pin is also written to PINS_DIR, which is how
    # pin_hook.py hands Claude Code the transcript for a marker in a terminal.
    # A note that is already transcribed gets no marker: it is typed at the
    # cursor. In a terminal the marker is swapped by keystrokes when the screen
    # can be read, and left for the hook when it cannot. A transcript that went
    # nowhere goes back in the clipboard queue.

    def _pin_target(self):
        """The note a pin would take: the newest one whose transcript has
        not been pasted or pinned yet. Main thread."""
        for note in reversed(self._notes):
            if note.state in ('recording', 'transcribing', 'ready', 'copied'):
                return note
        return None

    def _retry_tap(self, timer):
        if self._tap is not None and self._tap.start():
            timer.stop()
            _debug_line('click pin tap started')

    def _pin_hotkey_fired(self):
        note = self._pin_target()
        if note is None:
            self._hud_flash('No note to pin.')
        elif not pin.trusted():
            pin.trusted(prompt=True)
            self._hud_flash('Accessibility is off for ClipBridge.')
        else:
            self._start_pin(note, None)

    def _pin_click(self, x, y):
        note = self._pin_target()
        if note is not None:
            self._start_pin(note, (x, y))

    def _start_pin(self, note, at):
        self._notes.remove(note)
        self._pinned.append(note)
        if self._offer_note is note:
            self._offer_note = None
        if note.state in ('ready', 'copied'):
            # the words exist, so they go in where a marker would have
            note.pin = pin.Pin(None)
            note.pin.number = None
            note.state = 'delivering'

            def run():
                ok = note.pin.type_now(note.text, at, _debug_line)
                _on_main(self._pin_done, note, 'pasted' if ok else None)
            threading.Thread(target=run, daemon=True).start()
        else:
            number = _next_pin_number()
            note.pin = pin.Pin(f'\u27e6note {number}\u27e7')
            note.pin.number = number
            _write_pin(number, 'pending')
            threading.Thread(target=note.pin.place, args=(at, _debug_line),
                             daemon=True).start()
        self._advance()

    def _pin_ready(self, note, text):
        note.text  = text
        note.state = 'delivering'
        _write_pin(note.pin.number, 'ready', text)

        def run():
            number = note.pin.number
            result = note.pin.deliver(text, log=_debug_line,
                                      gone=lambda: _pin_sent(number))
            _on_main(self._pin_done, note, result)
        threading.Thread(target=run, daemon=True).start()
        self._refresh_icon()

    def _pin_done(self, note, result):
        """result is 'pasted', 'hook' for a marker left in a terminal for
        pin_hook.py, or None for a transcript that went nowhere."""
        marker = note.pin.marker
        if result != 'hook' and note.pin.number is not None:
            _write_pin(note.pin.number, 'ready', note.text, waiting=False)
        if result is None:
            # back in line for the clipboard, behind whatever holds it now
            if note in self._pinned:
                self._pinned.remove(note)
            note.pin    = None
            note.missed = True
            note.state  = 'ready'
            self._notes.append(note)
            if note is not self._notes[0] or self._paused:
                note.pill.queued(note.text)
            self._advance()
            return
        note.state = 'done'
        if result == 'pasted':
            note.pill.pasted(note.text)
        else:
            note.pill.pasted(note.text, f'ready for {marker}')
        self._refresh_icon()

    def _pin_failed(self, note):
        """No transcript is coming. The pin file says so, and a marker in a
        text field is taken back out."""
        if note in self._pinned:
            self._pinned.remove(note)

        def run():
            note.pin.placed.wait(5)
            # in a terminal the marker may still be sent, and the hook should
            # say the transcript is missing, so that pin stays waiting
            _write_pin(note.pin.number, 'failed', waiting=note.pin.terminal)
            if not note.pin.terminal:
                note.pin.deliver('', _debug_line)
        threading.Thread(target=run, daemon=True).start()

    def _offer(self, note):
        """Put note on the clipboard as a promise rather than as text. macOS
        asks this app for the text at the moment something pastes it, and
        that request is how the queue learns of the paste, with no key
        watched and no permission involved.

        Watching the keyboard for Cmd+V was the first attempt and saw
        nothing at all: without Input Monitoring, CGEventSourceKeyState
        reads every key as up."""
        if not HAS_APPKIT:
            _copy(note.text)
            _on_main(self._note_pasted, note)
            return
        promise = _ClipPromise.alloc().initWithOwner_note_(self, note)
        item = NSPasteboardItem.alloc().init()
        item.setDataProvider_forTypes_(promise, [NSPasteboardTypeString])
        pb = NSPasteboard.generalPasteboard()
        self._offer_note = None
        pb.clearContents()
        pb.writeObjects_([item])
        # held here as well as by the pasteboard, so a provider can never be
        # collected while the clipboard still names it
        self._promises = (self._promises + [promise])[-8:]
        self._offer_note  = note
        self._offer_count = pb.changeCount()
        if self._clip_timer is None:
            self._clip_timer = rumps.Timer(self._on_clip_tick, CLIP_WATCH_SEC)
            self._clip_timer.start()

    def _on_clip_tick(self, _timer=None):
        """Notice something else copied over the note on offer. The
        pasteboard does not announce it: its notice to the old provider only
        arrives the next time this app touches the pasteboard, so the change
        counter is compared instead. That notice is also why the provider
        implements nothing but the handover: asking the pasteboard anything
        from inside it re-enters the pasteboard until Python's recursion
        limit, as a test found."""
        note = self._offer_note
        if note is None:
            self._stop_clip_timer()
            return
        try:
            count = NSPasteboard.generalPasteboard().changeCount()
        except Exception:
            return
        if count != self._offer_count:
            self._offer_note = None
            self._stop_clip_timer()
            self._note_replaced(note)

    def _stop_clip_timer(self):
        if self._clip_timer is not None:
            self._clip_timer.stop()
            self._clip_timer = None

    def _sweep_pending(self, pill=None):
        """One pass over the saved notes, oldest first. Stops at the first
        note that still fails, since that almost always means the worker or
        the network is down and the rest would fail the same way.

        Only one sweep runs at a time, and a note the live upload still has
        in hand is skipped, so a note can never be transcribed twice or land
        on the clipboard twice.

        pill is given when Retry Pending was pressed. It is already showing
        Transcribing, and it is where the outcome goes. A timed sweep has no
        pill and says nothing unless a note came back. A pressed retry waits
        for a timed sweep in progress rather than being dropped."""
        if not self._sweep_lock.acquire(blocking=pill is not None):
            return 0
        try:
            return self._sweep_once(pill)
        finally:
            self._sweep_lock.release()

    def _sweep_once(self, pill=None):
        _prune_pending()
        _prune_notes()
        recovered, last, error, heard_nothing = 0, None, None, 0
        for path in _list_pending():
            # a recovered note goes straight to the clipboard, so it waits
            # until no note of this session is still in line for it
            if self._rec_on or self._notes:
                break
            with self._inflight_lock:
                busy = str(path) in self._inflight
            if busy:
                continue
            try:
                sf.info(str(path))
            except Exception:
                _discard_pending(path)   # unreadable, retrying cannot help
                continue
            try:
                text = _transcribe(path)
            except Exception as e:
                _debug_line(f'retry failed on {path.name}: {e}')
                error = e
                break
            _keep_note(path, text)
            _on_main(self._rebuild_notes_menu)
            if text:
                recovered += 1
                last = text
                _copy(text)
                if pill is None:
                    _notify(text, title='Recovered voice note')
            else:
                heard_nothing += 1
        if pill is None:
            if recovered:
                self._hud_flash(f'Recovered {recovered} saved note'
                                f'{"s" if recovered > 1 else ""}')
        elif error is not None:
            if recovered:
                pill.failed(f'Recovered {recovered}, then failed: {error}')
            else:
                pill.failed(str(error))
        elif recovered == 1:
            pill.copied(last)
        elif recovered:
            pill.flash(f'Recovered {recovered} saved notes')
        elif heard_nothing:
            pill.flash('Nothing heard.')
        elif self._rec_on or self._notes:
            pill.flash('Voice notes are still queued.')
        else:
            # a timed sweep got there while this one waited, and has
            # already said so in a pill of its own
            pill.hide()
        return recovered

    def _retry_loop(self):
        """Retry saved notes on our own, so one recorded with the worker
        down or the network off lands as soon as it comes back."""
        first = True
        while True:
            time.sleep(RETRY_FIRST_SEC if first else RETRY_SEC)
            first = False
            if self._rec_on or self._notes:
                continue     # never compete with notes in progress
            try:
                self._sweep_pending()
            except Exception:
                pass

    # ── Recent notes ───────────────────────────────────────────────────────────

    def _rebuild_notes_menu(self):
        """Fill the Recent Notes submenu from what is on disk. Runs just
        before the menu opens and again whenever a note is filed, so it can
        never offer a note that has been pruned or omit one that just landed.

        Rebuilding only when the folder has actually changed keeps a menu
        opened repeatedly from stacking up NSMenuItems that rumps holds a
        reference to for the life of the app."""
        if self._notes_item is None:
            return
        notes = _list_notes()[:NOTES_KEEP]
        shown = [(str(wav), ts, _note_text_path(wav).is_file())
                 for wav, ts in notes]
        if shown == self._notes_shown:
            return
        try:
            if self._notes_item._menu is not None:
                self._notes_item.clear()
        except Exception:
            pass
        if not notes:
            # An empty submenu greys its parent out, which is the standard way
            # a menu says there is nothing under it. A placeholder item cannot
            # do that job: an inert row is drawn greyed too, and a submenu with
            # nothing enabled in it does not open to show the row at all.
            self._notes_shown = shown
            return
        # Two notes in the same minute read alike to the minute, so both are
        # shown to the second rather than one of the pair being the odd one.
        labels = [_note_label(ts) for _, ts in notes]
        clashes = {t for t in labels if labels.count(t) > 1}
        used = set()
        for (wav, ts), label in zip(notes, labels):
            title = _note_label(ts, with_seconds=True) if label in clashes \
                    else label
            # rumps keys a menu item by its title and silently drops a second
            # item under a title already taken, so a title has to be unique
            # whatever the clock says. A hair space is padding that makes it
            # so without changing what the item reads as.
            while title in used:
                title += '\u200a'
            used.add(title)
            item = rumps.MenuItem(title)
            item.set_callback(
                lambda _sender, path=wav: self._replay_note(path))
            self._notes_item.add(item)
        self._notes_shown = shown

    def _replay_note(self, wav):
        """Put a saved note back on the clipboard. The transcript beside the
        audio is what is normally handed back. A note recorded before
        transcripts were saved, or one whose text never landed, has only the
        audio, so that is transcribed again and the text kept this time."""
        wav = Path(wav)
        # the clipboard belongs to the queue while any note is in it
        if self._rec_on or self._rec_opening:
            self._hud_flash('Already recording.')
            return
        if self._notes:
            self._hud_flash('Voice notes are still queued.')
            return
        text = _read_note_text(wav)
        if text:
            _copy(text)
            self._hud_copied(text)
            return
        if not wav.is_file():
            self._hud_flash('That note is gone.')
            _on_main(self._rebuild_notes_menu)
            return
        self._replaying = True
        self._refresh_icon()
        pill = self._pill()
        pill.processing()
        threading.Thread(target=self._replay_worker, args=(wav, pill),
                         daemon=True).start()

    def _replay_worker(self, wav, pill):
        try:
            text = _transcribe(wav)
        except Exception as e:
            _debug_line(f'replay failed: {e}')
            pill.failed(str(e))
        else:
            if text:
                _write_note_text(wav, text)
                _on_main(self._rebuild_notes_menu)
            _on_main(self._replay_done, pill, text)
        finally:
            self._replaying = False
            _on_main(self._refresh_icon)

    def _replay_done(self, pill, text):
        if not text:
            pill.flash('Nothing heard.')
        elif self._rec_on or self._notes:
            # a note was started while this one transcribed, and the
            # clipboard is its now
            pill.flash('Transcript saved to Recent Notes.')
        else:
            _copy(text)
            pill.copied(text)

    def _open_notes(self, _):
        try:
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        subprocess.run(['open', str(NOTES_DIR)])

    def _retry_now(self, _):
        if self._rec_on or self._rec_opening:
            self._hud_flash('Already recording.')
            return
        if self._notes:
            self._hud_flash('Voice notes are still queued.')
            return
        with self._inflight_lock:
            waiting = [p for p in _list_pending()
                       if str(p) not in self._inflight]
        if not waiting:
            self._hud_flash('No saved notes to retry.')
            return
        pill = self._pill()
        pill.processing()
        threading.Thread(target=self._sweep_pending, args=(pill,),
                         daemon=True).start()


if __name__ == '__main__':
    ClipBridge().run()
