#!/usr/bin/env python3
"""Run the pill from source in a throwaway process, cycling its states.

This answers the one question worth answering first when the pill stops
appearing: is the drawing broken, or is the installed app's window stale?
This process is seconds old and has never slept, changed displays, or
changed Space, so if the pill shows here and not in the installed app,
the code is fine and the window is the problem. See mac/HUD_DEBUGGING.md.

    mac/.venv/bin/python3 mac/tools/hud_demo.py

Each state is held long enough to look at, and the panel's own view of
itself is printed alongside so it can be compared with hud_probe.py.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

from hud import RecordingHUD


def report(hud, tag):
    p = hud._panel
    if p is None:
        print(f'{tag}: no panel', flush=True)
        return
    print(f'{tag}: win={p.windowNumber()} visible={p.isVisible()} '
          f'alpha={p.alphaValue():.2f} onActiveSpace={p.isOnActiveSpace()} '
          f'level={p.level()} behavior={p.collectionBehavior()}', flush=True)


class Demo(rumps.App):
    def __init__(self):
        super().__init__('HUD demo')
        self.hud = RecordingHUD()
        threading.Thread(target=self.script, daemon=True).start()

    def script(self):
        time.sleep(1.0)
        # match the installed app, which is LSUIElement
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)

        self.hud.recording()
        time.sleep(4.0)
        report(self.hud, 'recording')

        self.hud.processing()
        time.sleep(2.5)
        report(self.hud, 'transcribing')

        # the copied pill times out on its own after 2.9s, so look at it
        # before that rather than after, or the panel is already discarded
        self.hud.copied('this is what a finished voice note looks like')
        time.sleep(2.0)
        report(self.hud, 'copied')

        time.sleep(3.0)
        report(self.hud, 'after it leaves')
        print('if you saw three pills, the drawing code is fine.', flush=True)
        rumps.quit_application()


if __name__ == '__main__':
    Demo().run()
