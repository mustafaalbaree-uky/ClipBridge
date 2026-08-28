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
black capsule ringed by a drifting rainbow, slow while recording with an
elapsed counter, fast while transcribing, then a brief "Copied to
clipboard". Voice note status never uses notification banners; those are
kept for clipboard sync only.

Recording is done by AVAudioRecorder, which writes straight into
~/.clipbridge/pending, so the audio is on disk while it is still being
spoken rather than only after the fact. The file is deleted once it
transcribes; anything left there is a note whose upload failed, so a note
recorded offline, or one interrupted by a crash, survives. A background
sweep retries them on its own and deletes each one the moment it works.

Menu:
    Send to PC       push the current clipboard to the PC, instantly
    Fetch Now        one shot fetch of the latest incoming clip
    Record Note      toggle recording (same as the hotkey)
    Retry Pending    upload saved notes now instead of waiting
    Auto: ON         toggle background polling
    Quit

Voice notes are processed by shared/noteproc.py: silences truncated,
long audio chunked at quiet points and transcribed in parallel.

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
WAV_MIN_BYTES     = 44 + int(SAMPLERATE * 2 * WAV_MIN_SEC)

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
    """Latest clip addressed to this Mac. Returns (id, content)."""
    try:
        res = requests.get(
            f'{SUPA_URL}/rest/v1/clips',
            headers=SUPA_HEADERS,
            params={'select': 'id,content', 'source': 'eq.pc-to-mac',
                    'order': 'created_at.desc', 'limit': '1'},
            timeout=10,
        )
        if res.ok and res.json():
            row = res.json()[0]
            return row['id'], row['content']
    except Exception:
        pass
    return None, None


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
    """Where the recording in progress is written. It carries a .rec suffix
    so the retry sweep, which looks only at .wav, cannot pick up a note that
    is still being spoken. It becomes a .wav the moment recording stops."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    return PENDING_DIR / f'note-{stamp}-{int(time.time() * 1000) % 1000:03d}.rec'


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
        final = path.with_suffix('.wav')
        path.rename(final)
        return final
    except Exception:
        return None


def _recover_part_files():
    """A .rec left behind means the app died mid recording. Promote it so
    the retry sweep transcribes it instead of leaving it to rot."""
    try:
        for part in PENDING_DIR.glob('*.rec'):
            try:
                if part.stat().st_size >= WAV_MIN_BYTES:
                    part.rename(part.with_suffix('.wav'))
                else:
                    part.unlink()
            except Exception:
                pass
    except Exception:
        pass


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
        items = [
            rumps.MenuItem('Send to PC', callback=self._send_to_pc),
            rumps.MenuItem('Fetch Now',  callback=self._fetch_now),
        ]
        if CAN_RECORD:
            self._record_item = rumps.MenuItem('Record Note',
                                               callback=self._toggle_record)
            items += [None, self._record_item,
                      rumps.MenuItem('Retry Pending',
                                     callback=self._retry_now)]
        items += [
            None,
            self._auto_item,
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
        if HAS_APPKIT and RecordingHUD is not None:
            try:
                self._hud = RecordingHUD()
            except Exception:
                self._hud = None
        threading.Thread(target=self._poll, daemon=True).start()
        if CAN_RECORD:
            _recover_part_files()
            threading.Thread(target=self._retry_loop, daemon=True).start()
        self._hotkey_handle = None
        diag = {
            'started': time.strftime('%Y-%m-%d %H:%M:%S'),
            'can_record': CAN_RECORD, 'has_audio': HAS_AUDIO,
            'has_hotkey_lib': HAS_HOTKEY, 'hotkey': HOTKEY,
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
        while True:
            try:
                if self._auto:
                    row_id, content = _fetch()
                    if row_id is not None and not self._seeded:
                        # remember where the table is at launch without
                        # copying, so an old clip never stomps the clipboard
                        self._seeded  = True
                        self._last_id = row_id
                    elif content and row_id != self._last_id:
                        self._last_id = row_id
                        _copy(content)
                        _notify(content)
            except Exception:
                pass
            time.sleep(POLL_SEC)

    def _toggle_auto(self, item):
        self._auto = not self._auto
        item.title = f'Auto: {"ON" if self._auto else "OFF"}'

    def _fetch_now(self, _):
        def _run():
            row_id, content = _fetch()
            if content:
                self._last_id = row_id
                self._seeded  = True
                _copy(content)
                _notify(content)
            else:
                _notify('Nothing waiting.', title='ClipBridge')
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
            # when it heard nothing at all
            _discard_pending(final)
            if text:
                _copy(text)
                self._hud_flash('Copied to clipboard')
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
            _discard_pending(path)
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

    def _retry_now(self, _):
        threading.Thread(target=self._sweep_pending, daemon=True).start()


if __name__ == '__main__':
    ClipBridge().run()
