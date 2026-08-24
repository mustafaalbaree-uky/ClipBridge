"""
ClipBridge, Windows tray client.

Sits in the system tray and keeps this PC's clipboard in sync with the
other devices through a shared Supabase table.

Right click menu:
    Auto: ON/OFF     toggle background polling for incoming clips
    Fetch Now        one shot fetch of the latest incoming clip
    Send to Mac      push the current clipboard to the Mac, instantly
    Send to iPhone   push the current clipboard to the iPhone, instantly
    Record Note      (only when a transcription worker is configured)
    Quit

Left click deliberately does nothing, so a stray click never starts
anything by accident.

Config lives outside the repo, see config.example.json at the repo root.
Search order: %USERPROFILE%/.clipbridge/config.json, then config.json
next to the repo root.
"""
import io
import json
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import requests
import pyperclip
from pystray import Icon, Menu, MenuItem
from PIL import Image, ImageDraw

try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    HAS_AUDIO = True
except Exception:
    HAS_AUDIO = False


# ── Config ─────────────────────────────────────────────────────────────────────

def _load_config():
    candidates = [
        Path.home() / '.clipbridge' / 'config.json',
        Path(__file__).resolve().parent.parent / 'config.json',
    ]
    for path in candidates:
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            pass
    return None


_cfg = _load_config()
if not _cfg or 'supabase_url' not in _cfg or 'supabase_anon_key' not in _cfg:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        'ClipBridge',
        'No config found.\n\n'
        'Copy config.example.json to ~/.clipbridge/config.json '
        'and fill in your Supabase URL and anon key.')
    sys.exit(1)

SUPA_URL     = _cfg['supabase_url'].rstrip('/')
SUPA_ANON    = _cfg['supabase_anon_key']
SUPA_HEADERS = {'apikey': SUPA_ANON, 'Authorization': f'Bearer {SUPA_ANON}'}
POLL_SEC     = int(_cfg.get('poll_seconds', 3))
WORKER_URL   = _cfg.get('transcribe_worker_url', '')
SAMPLERATE   = 16000

CAN_RECORD = bool(WORKER_URL) and HAS_AUDIO

# ── Palette ────────────────────────────────────────────────────────────────────
BG      = '#1c1c1e'
SURFACE = '#2c2c2e'
WHITE   = '#ffffff'
MUTED   = '#8e8e93'
GREEN   = '#30d158'
RED     = '#ff453a'
ORANGE  = '#ff9f0a'
BLUE    = '#0a84ff'

# ── State ──────────────────────────────────────────────────────────────────────
_recording    = False
_audio_data   = []
_stream       = None
_icon         = None
_root         = None
_dialog       = None
_auto_mode    = True
_last_seen_id = None
_seeded       = False
_poll_stop    = threading.Event()


# ── Tray icon ──────────────────────────────────────────────────────────────────

def _make_icon(state='auto'):
    """Clipboard glyph on a rounded square, drawn at 4x and downscaled."""
    bg = {'auto': '#0a84ff', 'idle': '#6c6c70',
          'recording': '#ff453a', 'processing': '#ff9f0a'}[state]
    S = 256
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, S - 8, S - 8], radius=60, fill=bg)
    # clip tab, drawn first so the board overlaps its lower half
    d.rounded_rectangle([96, 36, 160, 92], radius=18, fill=WHITE)
    # board
    d.rounded_rectangle([60, 64, 196, 220], radius=20, fill=WHITE)
    # text lines on the board, in the background color
    for y in (108, 142, 176):
        d.rounded_rectangle([84, y, 172, y + 12], radius=6, fill=bg)
    return img.resize((64, 64), Image.LANCZOS)


def _idle_icon():
    return _make_icon('auto' if _auto_mode else 'idle')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_destroy(w):
    try:
        w.destroy()
    except Exception:
        pass


def _copy_to_clipboard(text):
    """Windows can refuse clipboard access while another app holds it,
    so retry a few times instead of letting the caller die."""
    for _ in range(4):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            time.sleep(0.3)
    return False


# ── Toast notification ─────────────────────────────────────────────────────────

def _notify(text, source='recorded'):
    def _show():
        W = 300

        toast = tk.Toplevel(_root)
        toast.overrideredirect(True)
        toast.attributes('-topmost', True)
        toast.configure(bg=BG)

        tag, color = {
            'recorded':    ('Recorded on PC',       GREEN),
            'fetched':     ('Received from iPhone', BLUE),
            'fetched-mac': ('Received from Mac',    ORANGE),
            'sent':        ('Sent to iPhone',       BLUE),
            'sent-mac':    ('Sent to Mac',          ORANGE),
            'nothing':     ('Nothing new',          MUTED),
            'empty':       ('Clipboard is empty',   MUTED),
            'error':       ('Something went wrong', RED),
        }.get(source, ('Done', GREEN))

        tk.Frame(toast, bg=color, height=3).pack(fill='x')

        body = tk.Frame(toast, bg=BG, padx=14, pady=10)
        body.pack(fill='both', expand=True)

        row = tk.Frame(body, bg=BG)
        row.pack(fill='x', pady=(0, 5))
        tk.Label(row, text=tag, font=('Segoe UI', 9, 'bold'),
                 fg=color, bg=BG).pack(side='left')
        x_btn = tk.Label(row, text='✕', font=('Segoe UI', 9),
                         fg=MUTED, bg=BG, cursor='hand2')
        x_btn.pack(side='right')

        if text:
            preview = text[:100] + ('...' if len(text) > 100 else '')
            tk.Label(body, text=preview, font=('Segoe UI', 10),
                     fg=WHITE, bg=BG, wraplength=W - 32,
                     justify='left').pack(anchor='w')

        toast.update_idletasks()
        sw = toast.winfo_screenwidth()
        sh = toast.winfo_screenheight()
        h  = toast.winfo_height()
        toast.geometry(f'{W}x{h}+{sw - W - 14}+{sh - h - 54}')

        x_btn.bind('<Button-1>', lambda e: _safe_destroy(toast))
        toast.after(4500, lambda: _safe_destroy(toast))

    try:
        _root.after(0, _show)
    except Exception:
        pass


# ── Supabase ───────────────────────────────────────────────────────────────────

def _fetch_supabase():
    """Latest clip addressed to this PC. Returns (id, content, source)."""
    try:
        res = requests.get(
            f'{SUPA_URL}/rest/v1/clips',
            headers=SUPA_HEADERS,
            params={'select': 'id,content,source',
                    'or': '(source.is.null,source.eq.mac-to-pc,source.eq.ios)',
                    'order': 'created_at.desc', 'limit': '1'},
            timeout=10,
        )
        if res.ok:
            data = res.json()
            if data:
                return data[0]['id'], data[0]['content'], data[0].get('source')
    except Exception:
        pass
    return None, None, None


def _push_supabase(content, source='pc'):
    try:
        expires_at = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                   time.gmtime(time.time() + 24 * 3600))
        res = requests.post(
            f'{SUPA_URL}/rest/v1/clips',
            headers={**SUPA_HEADERS,
                     'Content-Type': 'application/json',
                     'Prefer': 'return=minimal'},
            json={'content': content, 'expires_at': expires_at, 'source': source},
            timeout=10,
        )
        return res.ok
    except Exception:
        return False


# ── Instant send ───────────────────────────────────────────────────────────────

def _send_clipboard(dest, notify_source):
    """Read the clipboard and push it, no dialog, no extra clicks."""
    def _run():
        try:
            text = pyperclip.paste()
        except Exception:
            text = ''
        text = (text or '').strip()
        if not text:
            _notify('', source='empty')
            return
        ok = _push_supabase(text, source=dest)
        _notify(text if ok else 'Push failed', source=notify_source if ok else 'error')
    threading.Thread(target=_run, daemon=True).start()


# ── Fetch ──────────────────────────────────────────────────────────────────────

def _fetch_now(icon=None, item=None):
    def _run():
        global _last_seen_id, _seeded
        row_id, content, src = _fetch_supabase()
        if content:
            _last_seen_id = row_id
            _seeded = True
            _copy_to_clipboard(content)
            _notify(content, source='fetched-mac' if src == 'mac-to-pc' else 'fetched')
        else:
            _notify('', source='nothing')
    threading.Thread(target=_run, daemon=True).start()


# ── Auto polling ───────────────────────────────────────────────────────────────

def _poll_loop():
    global _last_seen_id, _seeded
    while not _poll_stop.is_set():
        try:
            row_id, content, src = _fetch_supabase()
            if row_id is not None and not _seeded:
                # first sight of the table: remember where we are without
                # copying, so an old clip never stomps the clipboard at launch
                _seeded = True
                _last_seen_id = row_id
            elif content and row_id != _last_seen_id:
                _last_seen_id = row_id
                if _copy_to_clipboard(content):
                    _notify(content,
                            source='fetched-mac' if src == 'mac-to-pc' else 'fetched')
        except Exception:
            # never let one bad cycle kill the loop
            pass
        _poll_stop.wait(POLL_SEC)


def _toggle_auto(icon=None, item=None):
    global _auto_mode, _poll_stop
    _auto_mode = not _auto_mode
    if _auto_mode:
        _poll_stop = threading.Event()
        threading.Thread(target=_poll_loop, daemon=True).start()
        _icon.icon = _make_icon('auto')
    else:
        _poll_stop.set()
        _icon.icon = _make_icon('idle')


# ── Recording (optional, menu only, never on left click) ───────────────────────

def _show_dialog():
    global _dialog
    if _dialog:
        return

    _dialog = tk.Toplevel(_root)
    w = _dialog
    w.overrideredirect(True)
    w.attributes('-topmost', True)
    w.configure(bg=BG)

    sw = w.winfo_screenwidth()
    w.geometry(f'230x108+{sw - 246}+20')

    tk.Frame(w, bg=RED, height=3).pack(fill='x')

    body = tk.Frame(w, bg=BG, padx=16, pady=10)
    body.pack(fill='both', expand=True)

    top = tk.Frame(body, bg=BG)
    top.pack(fill='x', pady=(0, 9))

    dot = tk.Canvas(top, width=10, height=10, bg=BG, highlightthickness=0)
    dot.pack(side='left', padx=(0, 8), pady=2)
    dot_id = dot.create_oval(1, 1, 9, 9, fill=RED, outline='')

    tk.Label(top, text='Recording', font=('Segoe UI', 11, 'bold'),
             fg=WHITE, bg=BG).pack(side='left')

    timer_var = tk.StringVar(value='0:00')
    tk.Label(top, textvariable=timer_var, font=('Segoe UI', 9),
             fg=MUTED, bg=BG).pack(side='right')

    tk.Button(body, text='Stop', font=('Segoe UI', 10, 'bold'),
              bg=RED, fg=WHITE, relief='flat', activebackground='#cc2200',
              activeforeground=WHITE, cursor='hand2', pady=5, bd=0,
              command=_on_stop_clicked).pack(fill='x')

    t0 = time.time()
    vis = [True]

    def _tick():
        if not _dialog:
            return
        vis[0] = not vis[0]
        dot.itemconfig(dot_id, fill=RED if vis[0] else BG)
        elapsed = int(time.time() - t0)
        timer_var.set(f'{elapsed // 60}:{elapsed % 60:02d}')
        w.after(500, _tick)

    _tick()
    w.protocol('WM_DELETE_WINDOW', _on_stop_clicked)


def _close_dialog():
    global _dialog
    if _dialog:
        _safe_destroy(_dialog)
        _dialog = None


def _audio_callback(indata, frames, time_info, status):
    if _recording:
        _audio_data.append(indata.copy())


def _start_recording(icon=None, item=None):
    global _recording, _audio_data, _stream
    if _recording:
        return
    _recording  = True
    _audio_data = []
    _stream = sd.InputStream(samplerate=SAMPLERATE, channels=1,
                             dtype='float32', callback=_audio_callback)
    _stream.start()
    _icon.icon = _make_icon('recording')
    _root.after(0, _show_dialog)


def _stop_recording():
    global _recording, _stream
    if not _recording:
        return
    _recording = False
    if _stream:
        _stream.stop()
        _stream.close()
        _stream = None
    _icon.icon = _make_icon('processing')


def _on_stop_clicked():
    _root.after(0, _close_dialog)
    _stop_recording()
    if _audio_data:
        data = np.concatenate(_audio_data, axis=0)
        threading.Thread(target=_transcribe, args=(data,), daemon=True).start()
    else:
        _icon.icon = _idle_icon()


def _transcribe(audio):
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLERATE, format='WAV', subtype='PCM_16')
    buf.seek(0)
    try:
        res = requests.post(
            WORKER_URL,
            files={'file': ('audio.wav', buf, 'audio/wav')},
            data={'model': 'whisper-large-v3-turbo', 'language': 'en'},
            timeout=30,
        )
        if res.ok:
            text = res.json().get('text', '').strip()
            if text:
                _copy_to_clipboard(text)
                _push_supabase(text)
                _notify(text, source='recorded')
            else:
                _notify('', source='nothing')
        else:
            _notify(f'Error {res.status_code}', source='error')
    except Exception as e:
        _notify(str(e), source='error')
    finally:
        _icon.icon = _idle_icon()


# ── Tray ───────────────────────────────────────────────────────────────────────

def _on_quit(icon=None, item=None):
    if _recording:
        _stop_recording()
    _poll_stop.set()
    _icon.stop()
    _root.quit()


def _build_menu():
    items = [
        MenuItem(lambda item: f'Auto: {"ON  " if _auto_mode else "OFF"}', _toggle_auto),
        MenuItem('Fetch Now', _fetch_now),
        MenuItem('Send to Mac',
                 lambda icon, item: _send_clipboard('pc-to-mac', 'sent-mac')),
        MenuItem('Send to iPhone',
                 lambda icon, item: _send_clipboard('pc', 'sent')),
    ]
    if CAN_RECORD:
        items += [Menu.SEPARATOR, MenuItem('Record Note', _start_recording)]
    items += [Menu.SEPARATOR, MenuItem('Quit', _on_quit)]
    return Menu(*items)


# ── Entry point ────────────────────────────────────────────────────────────────

_root = tk.Tk()
_root.withdraw()

_icon = Icon('ClipBridge', _make_icon('auto'), 'ClipBridge', menu=_build_menu())

threading.Thread(target=_icon.run, daemon=True).start()
threading.Thread(target=_poll_loop, daemon=True).start()
_root.mainloop()
