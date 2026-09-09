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
    Recent Notes     the last five, by date and time; click one to copy
                     its transcript back to the clipboard
    Notes Folder     open the folder holding transcribed recordings
    Retry Pending    upload saved notes now instead of waiting
    Auto: ON         toggle background polling
    Quit

Voice notes are processed by shared/noteproc.py: long audio chunked at
quiet points and transcribed in parallel.

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
                        NSEventMaskRightMouseUp, NSEventModifierFlagControl)
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
    from hud import RecordingHUD
except Exception:
    RecordingHUD = None

try:
    import loginitem
except Exception:
    loginitem = None


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
HOTKEY       = _cfg.get('record_hotkey', '<ctrl>+<alt>+space')
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

CAN_RECORD = bool(WORKER_URL) and HAS_AUDIO and noteproc is not None


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
            self._notes_item = rumps.MenuItem('Recent Notes')
            # what the submenu currently shows, so it is only rebuilt when the
            # notes on disk have actually changed
            self._notes_shown = None
            items += [None, self._record_item, self._notes_item,
                      rumps.MenuItem('Notes Folder',
                                     callback=self._open_notes),
                      rumps.MenuItem('Retry Pending',
                                     callback=self._retry_now)]
        else:
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
        self._hud = None
        # notes the live upload is holding, so the retry sweep leaves them be
        self._inflight      = set()
        self._inflight_lock = threading.Lock()
        self._sweep_lock    = threading.Lock()
        hud_note = 'off, no AppKit' if not HAS_APPKIT else \
                   'off, hud.py did not import' if RecordingHUD is None else 'on'
        if HAS_APPKIT and RecordingHUD is not None:
            try:
                self._hud = RecordingHUD()
            except Exception as e:
                self._hud = None
                hud_note = f'off, {e}'
        threading.Thread(target=self._poll, daemon=True).start()
        if CAN_RECORD:
            _recover_part_files()
            _prune_notes()
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
        try:
            with open(Path.home() / '.clipbridge' / 'mac_debug.log', 'w') as f:
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
            with open(Path.home() / '.clipbridge' / 'mac_debug.log', 'a') as f:
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

    def _hud_flash(self, text, seconds=1.8):
        """Short on screen confirmation, falling back to a notification
        only when the HUD could not be built at all."""
        if self._hud:
            self._hud.flash(text, seconds)
        else:
            _notify(text, title='ClipBridge')

    def _hud_copied(self, text):
        """The transcript is on the clipboard. Showing its opening words
        is what makes the confirmation worth reading: it says the note
        was heard, not merely that something finished."""
        if self._hud:
            self._hud.copied(text)
        else:
            _notify(text, title='Copied to clipboard')

    def _toggle_record(self, _sender):
        if not CAN_RECORD:
            return
        if self._rec_opening:
            # Pressed again while the microphone is still coming up. Treat it
            # as "forget it", so the next press starts clean rather than
            # queueing a second recorder behind this one.
            self._rec_gen    += 1
            self._rec_opening = False
            self._set_state('idle')
            self._hud_flash('Canceled.')
        elif not self._rec_on:
            # Nothing on this thread may touch audio. Even Apple's recorder
            # can sit waiting on CoreAudio, and this is the thread that runs
            # the menu bar, the pill, and the Carbon hotkey.
            self._rec_opening = True
            self._rec_gen    += 1
            gen = self._rec_gen
            threading.Thread(target=self._open_worker, args=(gen,),
                             daemon=True).start()
            threading.Thread(target=self._open_watchdog, args=(gen,),
                             daemon=True).start()
        else:
            self._rec_on = False
            rec  = self._recorder
            path = self._rec_path
            self._recorder = None
            self._rec_path = None
            self._record_item.title = 'Record Note'
            self._set_state('busy')
            if self._hud:
                self._hud.processing()
            threading.Thread(target=self._finish_note, args=(rec, path),
                             daemon=True).start()

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
        self._record_item.title = 'Stop Recording'
        self._set_state('rec')
        if self._hud:
            self._hud.recording()

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
        self._set_state('idle')
        self._hud_flash(f'Could not open the microphone: {message}',
                        seconds=3.0)

    def _finish_note(self, rec, path):
        """Stop the recorder, then transcribe what it wrote. The audio is
        already on disk by the time we get here, so a failed upload loses
        nothing: the file stays and the retry sweep picks it up."""
        final = None
        try:
            final = _finish_recorder(rec, path)
            if final is None:
                self._hud_flash('Nothing recorded.')
                return
            with self._inflight_lock:
                self._inflight.add(str(final))
            audio, sr = sf.read(str(final), dtype='float32')
            text = noteproc.transcribe_note(audio, sr, WORKER_URL)
            # the worker answered, so there is nothing left to retry, even
            # when it heard nothing at all. The audio moves to NOTES_DIR with
            # its text beside it, so the note can be copied again without
            # another upload and a poor transcript can be run again.
            _keep_note(final, text)
            _on_main(self._rebuild_notes_menu)
            if text:
                _copy(text)
                self._hud_copied(text)
            else:
                self._hud_flash('Nothing heard.')
        except Exception as e:
            if final:
                self._hud_flash(f'{e}. Saved, will retry.', seconds=3.0)
            else:
                self._hud_flash(str(e), seconds=3.0)
        finally:
            if final:
                with self._inflight_lock:
                    self._inflight.discard(str(final))
            self._set_state('idle')

    def _sweep_pending(self):
        """One pass over the saved notes, oldest first. Stops at the first
        note that still fails, since that almost always means the worker or
        the network is down and the rest would fail the same way.

        Only one sweep runs at a time, and a note the live upload still has
        in hand is skipped, so a note can never be transcribed twice or land
        on the clipboard twice."""
        if not self._sweep_lock.acquire(blocking=False):
            return 0
        try:
            return self._sweep_once()
        finally:
            self._sweep_lock.release()

    def _sweep_once(self):
        _prune_pending()
        _prune_notes()
        recovered = 0
        for path in _list_pending():
            if self._rec_on:
                break
            with self._inflight_lock:
                busy = str(path) in self._inflight
            if busy:
                continue
            try:
                audio, sr = sf.read(str(path), dtype='float32')
            except Exception:
                _discard_pending(path)   # unreadable, retrying cannot help
                continue
            try:
                text = noteproc.transcribe_note(audio, sr, WORKER_URL)
            except Exception:
                break
            _keep_note(path, text)
            _on_main(self._rebuild_notes_menu)
            if text:
                recovered += 1
                _copy(text)
                _notify(text, title='Recovered voice note')
        if recovered:
            self._hud_flash(f'Recovered {recovered} saved note'
                            f'{"s" if recovered > 1 else ""}')
        return recovered

    def _retry_loop(self):
        """Retry saved notes on our own, so one recorded with the worker
        down or the network off lands as soon as it comes back."""
        first = True
        while True:
            time.sleep(RETRY_FIRST_SEC if first else RETRY_SEC)
            first = False
            if self._rec_on:
                continue     # never compete with a recording in progress
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
        text = _read_note_text(wav)
        if text:
            _copy(text)
            self._hud_copied(text)
            return
        if not wav.is_file():
            self._hud_flash('That note is gone.')
            _on_main(self._rebuild_notes_menu)
            return
        if self._rec_on or self._rec_opening:
            self._hud_flash('Already recording.')
            return
        self._set_state('busy')
        if self._hud:
            self._hud.processing()
        threading.Thread(target=self._replay_worker, args=(wav,),
                         daemon=True).start()

    def _replay_worker(self, wav):
        try:
            audio, sr = sf.read(str(wav), dtype='float32')
            text = noteproc.transcribe_note(audio, sr, WORKER_URL)
        except Exception as e:
            self._set_state('idle')
            self._hud_flash(str(e), seconds=3.0)
            return
        self._set_state('idle')
        if text:
            _write_note_text(wav, text)
            _copy(text)
            self._hud_copied(text)
            _on_main(self._rebuild_notes_menu)
        else:
            self._hud_flash('Nothing heard.')

    def _open_notes(self, _):
        try:
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        subprocess.run(['open', str(NOTES_DIR)])

    def _retry_now(self, _):
        threading.Thread(target=self._sweep_pending, daemon=True).start()


if __name__ == '__main__':
    ClipBridge().run()
