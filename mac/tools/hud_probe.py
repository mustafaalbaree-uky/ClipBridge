#!/usr/bin/env python3
"""Watch what the window server thinks of ClipBridge's pill.

Run this, then press the hotkey and record a short note. Every change to
the panel's state prints one line. This asks the window server rather
than the app, so it sees the panel even when the app believes the pill is
up and it is not, which is the whole failure mode this exists for.

    python3 mac/tools/hud_probe.py [seconds]

Reading the output is documented in mac/HUD_DEBUGGING.md. The short
version: alpha 1.0 with onscreen False means the window server took the
panel away, and an X far outside every screen means the panel is bound to
one Space and is sliding with a Space transition.
"""
import sys
import time

from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionAll,
                    kCGNullWindowID)
from AppKit import NSScreen

APP = 'ClipBridge'


def screens():
    return [(s.frame().origin.x, s.frame().origin.y,
             s.frame().size.width, s.frame().size.height)
            for s in NSScreen.screens()]


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    print(f'screens: {screens()}')
    print(f'watching {APP} windows for {seconds:.0f}s. '
          f'Press the hotkey and record a note.')
    prev = {}
    end  = time.time() + seconds
    while time.time() < end:
        live = set()
        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll,
                                            kCGNullWindowID):
            if w.get('kCGWindowOwnerName') != APP:
                continue
            num = w.get('kCGWindowNumber')
            live.add(num)
            b   = w.get('kCGWindowBounds', {})
            row = (round(w.get('kCGWindowAlpha', -1), 2),
                   bool(w.get('kCGWindowIsOnscreen', False)),
                   w.get('kCGWindowLayer'),
                   round(b.get('X', 0)), round(b.get('Y', 0)),
                   round(b.get('Width', 0)), round(b.get('Height', 0)))
            if prev.get(num) != row:
                prev[num] = row
                print(f'{time.strftime("%H:%M:%S")} win={num} alpha={row[0]} '
                      f'onscreen={row[1]} layer={row[2]} '
                      f'rect={row[3]},{row[4]} {row[5]}x{row[6]}', flush=True)
        for num in list(prev):
            if num not in live:
                del prev[num]
                print(f'{time.strftime("%H:%M:%S")} win={num} gone', flush=True)
        time.sleep(0.08)
    print('done')


if __name__ == '__main__':
    main()
