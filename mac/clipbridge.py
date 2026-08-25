"""
ClipBridge, Mac menu bar client.

Polls the shared Supabase table for clips addressed to this Mac, copies
them to the clipboard, and shows a notification. Sending is instant:
whatever is on the clipboard right now goes out, no dialog.

Left click on the menu bar icon: send the clipboard to the PC.
Right click (or control click): open the menu.
Global hotkey (default ctrl+alt+r): toggle voice note recording. The
recording starts the instant the stream opens, the same hotkey stops it,
and the transcript lands on the clipboard only. Nothing is pushed to the
other devices unless you send it afterwards. The hotkey is registered
through Carbon (RegisterEventHotKey), so it needs no Input Monitoring
permission and works everywhere, including Terminal. While recording the
menu bar icon becomes a red dot; while transcribing, an ellipsis.

Menu:
    Send to PC       push the current clipboard to the PC, instantly
    Fetch Now        one shot fetch of the latest incoming clip
    Record Note      toggle recording (same as the hotkey)
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
    import sounddevice as sd
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
HOTKEY       = _cfg.get('record_hotkey', '<ctrl>+<alt>+r')
SAMPLERATE   = 16000

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
            items += [None, self._record_item]
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
        self._rec_on     = False
        self._rec_frames = []
        self._rec_stream = None
        threading.Thread(target=self._poll, daemon=True).start()
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
        try:
            AppHelper.callAfter(self._toggle_record, None)
        except Exception:
            self._toggle_record(None)

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

    def _toggle_record(self, _sender):
        if not CAN_RECORD:
            return
        if not self._rec_on:
            try:
                self._rec_frames = []
                self._rec_stream = sd.InputStream(
                    samplerate=SAMPLERATE, channels=1, dtype='float32',
                    callback=self._rec_callback)
                self._rec_stream.start()
            except Exception as e:
                _notify(f'Could not open the microphone: {e}',
                        title='ClipBridge')
                self._rec_stream = None
                return
            self._rec_on = True
            self._record_item.title = 'Stop Recording'
            self._set_state('rec')
        else:
            self._rec_on = False
            try:
                if self._rec_stream:
                    self._rec_stream.stop()
                    self._rec_stream.close()
            except Exception:
                pass
            self._rec_stream = None
            self._record_item.title = 'Record Note'
            frames = self._rec_frames
            self._rec_frames = []
            if not frames:
                self._set_state('idle')
                _notify('Nothing recorded.', title='ClipBridge')
                return
            self._set_state('busy')
            threading.Thread(target=self._transcribe, args=(frames,),
                             daemon=True).start()

    def _rec_callback(self, indata, frames, time_info, status):
        if self._rec_on:
            self._rec_frames.append(indata.copy())

    def _transcribe(self, frames):
        try:
            audio = np.concatenate(frames, axis=0)
            text = noteproc.transcribe_note(audio, SAMPLERATE, WORKER_URL)
            if text:
                _copy(text)
                _notify(text, title='Note transcribed')
            else:
                _notify('Nothing heard.', title='ClipBridge')
        except Exception as e:
            _notify(str(e), title='ClipBridge')
        finally:
            self._set_state('idle')


if __name__ == '__main__':
    ClipBridge().run()
