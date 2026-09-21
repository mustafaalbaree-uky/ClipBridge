#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook for ClipBridge pins.

A voice note pinned in a terminal leaves a marker such as ⟦note 3⟧ in the
prompt. ClipBridge swaps it for the transcript with keystrokes when it can
see the cursor sitting right after the marker (pin.py). This covers the rest:
a prompt sent before the transcript was back, a terminal that does not report
its screen, a window left in the background.
When the prompt is sent, this finds each marker, waits for its note to finish
transcribing if it has not yet, and hands Claude the transcript as what the
marker stands for. The prompt text itself is left as typed.

ClipBridge writes ~/.clipbridge/pins/<number>.json for every pin, with state
pending, ready, or failed, and waiting until the transcript has landed.
This marks each pin it hands over as no longer waiting.

Registered in ~/.claude/settings.json with a timeout above WAIT_SEC.
"""

import json
import re
import sys
import time
from pathlib import Path

PINS_DIR = Path.home() / '.clipbridge' / 'pins'
WAIT_SEC = 110
MARKER   = re.compile('⟦note (\\d+)⟧')


def _read(number):
    """The pin record, None when there is no such pin, or 'retry' when the
    file could not be read this time."""
    try:
        return json.loads((PINS_DIR / f'{number}.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except Exception:
        return 'retry'


def _mark_used(number, rec):
    """Tell ClipBridge this pin has landed, so marker numbers can start over."""
    try:
        rec['waiting'] = False
        tmp = PINS_DIR / f'{number}.json.hook'
        tmp.write_text(json.dumps(rec), encoding='utf-8')
        tmp.replace(PINS_DIR / f'{number}.json')
    except Exception:
        pass


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return
    numbers = list(dict.fromkeys(MARKER.findall(data.get('prompt') or '')))
    if not numbers:
        return
    records  = {}
    deadline = time.time() + WAIT_SEC
    while True:
        waiting = False
        for n in numbers:
            if n in records:
                continue
            rec = _read(n)
            if rec == 'retry' or (isinstance(rec, dict)
                                  and rec.get('state') == 'pending'):
                waiting = True
            else:
                records[n] = rec
        if not waiting or time.time() > deadline:
            break
        time.sleep(0.25)

    parts = []
    for n in numbers:
        marker = f'⟦note {n}⟧'
        rec = records.get(n)
        if isinstance(rec, dict) and rec.get('state') == 'ready' and rec.get('text'):
            parts.append(f'{marker} in the prompt is a placeholder for a voice '
                         f'note. Read the prompt as if this transcript were '
                         f'typed at that spot:\n{rec["text"]}')
        elif n not in records:
            parts.append(f'{marker} in the prompt is a voice note that was '
                         f'still transcribing after {WAIT_SEC} seconds, so '
                         f'its transcript is missing.')
        elif isinstance(rec, dict):
            parts.append(f'{marker} in the prompt is a voice note whose '
                         f'transcription failed, so its transcript is missing.')
    for n, rec in records.items():
        if isinstance(rec, dict) and rec.get('waiting', True):
            _mark_used(n, rec)
    if not parts:
        return
    out = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                  'additionalContext': '\n\n'.join(parts)}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
