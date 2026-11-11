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

The pill takes its colour from where the sun is, so two flags exist for
looking at an hour that is not the current one:

    ... hud_demo.py --at 21:30      every state, lit as it would be then
    ... hud_demo.py --sweep         one pill an hour, midnight to midnight

Both pin the clock the colours are read from and nothing else, so the
sweep runs in a couple of minutes rather than a day.

    ... hud_demo.py --stack         three overlapping notes in a column
"""
import argparse
import datetime
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

from hud import RecordingHUD, PillStack


def report(hud, tag):
    p = hud._panel
    if p is None:
        print(f'{tag}: no panel', flush=True)
        return
    print(f'{tag}: win={p.windowNumber()} visible={p.isVisible()} '
          f'alpha={p.alphaValue():.2f} onActiveSpace={p.isOnActiveSpace()} '
          f'level={p.level()} behavior={p.collectionBehavior()}', flush=True)


def _pin(hud, hhmm):
    """Read the colours as of hhmm today. None puts the real clock back."""
    if hhmm is None:
        hud._clock = None
        return
    h, m = (int(x) for x in hhmm.split(':'))
    hud._clock = datetime.datetime.now().replace(
        hour=h, minute=m, second=0, microsecond=0).timestamp()


class Demo(rumps.App):
    def __init__(self, at=None, sweep=False, stack=False):
        super().__init__('HUD demo')
        self.hud = RecordingHUD()
        self.at, self.sweep, self.stack = at, sweep, stack
        threading.Thread(target=self.script, daemon=True).start()

    def script(self):
        time.sleep(1.0)
        # match the installed app, which is LSUIElement
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)

        if self.sweep:
            self.day()
            return
        if self.stack:
            self.column()
            return
        _pin(self.hud, self.at)

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

    def day(self):
        """A whole day of light in two minutes: the pill says the hour and
        wears it. Held five seconds each, which is longer than the ring
        takes to come round, so the full run of colour is visible."""
        for hour in range(24):
            label = f'{hour:02d}:00'
            _pin(self.hud, label)
            self.hud.flash(label, 4.6)
            time.sleep(5.0)
        print('midnight to midnight.', flush=True)
        rumps.quit_application()

    def column(self):
        """What overlapping notes look like, driven the way clipbridge.py
        drives them: note one is pasted on a timer here instead of by Cmd+V."""
        stack = PillStack()
        one = stack.acquire()
        one.recording()
        time.sleep(2.5)
        one.processing()
        time.sleep(0.6)

        two = stack.acquire()
        two.recording()
        time.sleep(2.5)
        one.copied('the first note, on the clipboard now', None)
        time.sleep(1.5)
        two.processing()
        time.sleep(0.6)

        three = stack.acquire()
        three.recording()
        time.sleep(1.5)
        two.queued('the second one waits for a paste')
        time.sleep(3.0)
        for tag, p in (('three', three), ('two', two), ('one', one)):
            report(p, tag)

        one.hide()                       # as if Cmd+V
        two.copied('the second one waits for a paste', None)
        time.sleep(3.0)
        three.processing()
        time.sleep(1.5)
        two.hide()
        three.copied('and the third, last in line')
        time.sleep(4.0)
        print('recording on top, older notes below, queue in order.',
              flush=True)
        rumps.quit_application()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--at', metavar='HH:MM',
                    help='light the pill as it would be at this time today')
    ap.add_argument('--sweep', action='store_true',
                    help='one pill an hour, midnight to midnight')
    ap.add_argument('--stack', action='store_true',
                    help='three overlapping notes in a column')
    args = ap.parse_args()
    Demo(at=args.at, sweep=args.sweep, stack=args.stack).run()
