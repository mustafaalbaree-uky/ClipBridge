"""
ClipBridge, Mac menu bar client.

Polls the shared Supabase table for clips addressed to this Mac, copies
them to the clipboard, and shows a notification. Sending is instant:
"Send to PC" pushes whatever is on the clipboard right now, no dialog.

Menu:
    Send to PC   push the current clipboard to the PC, instantly
    Fetch Now    one shot fetch of the latest incoming clip
    Auto: ON     toggle background polling
    Quit

Run directly:   .venv/bin/python3 clipbridge.py
Build the app:  ./build.sh  (output: dist/ClipBridge.app)

Config search order: ~/.clipbridge/config.json, then config.json next to
the repo root. See config.example.json.
"""
import json
import subprocess
import threading
import time
import tempfile
from pathlib import Path

import rumps
import requests


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


# ── Menu bar icon ──────────────────────────────────────────────────────────────

def _make_icon_path():
    """Render the SF Symbol clipboard icon to a temp PNG so rumps can use
    it as a template image (adapts to light and dark menu bars)."""
    try:
        from AppKit import NSImage, NSBitmapImageRep
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            'doc.on.clipboard', 'ClipBridge')
        tiff = img.TIFFRepresentation()
        rep  = NSBitmapImageRep.imageRepWithData_(tiff)
        data = rep.representationUsingType_properties_(4, {})  # 4 = PNG
        path = tempfile.mktemp(suffix='Template.png')
        data.writeToFile_atomically_(path, True)
        return path
    except Exception:
        return None


_ICON_PATH = _make_icon_path()


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


# ── App ────────────────────────────────────────────────────────────────────────

class ClipBridge(rumps.App):
    def __init__(self):
        super().__init__('ClipBridge', icon=_ICON_PATH, template=True,
                         quit_button=None)
        self._auto_item = rumps.MenuItem('Auto: ON', callback=self._toggle_auto)
        self.menu = [
            rumps.MenuItem('Send to PC', callback=self._send_to_pc),
            rumps.MenuItem('Fetch Now',  callback=self._fetch_now),
            None,
            self._auto_item,
            None,
            rumps.MenuItem('Quit', callback=lambda _: rumps.quit_application()),
        ]
        self._last_id = None
        self._seeded  = False
        self._auto    = True
        threading.Thread(target=self._poll, daemon=True).start()

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


if __name__ == '__main__':
    ClipBridge().run()
