# When the pill stops appearing

The on screen pill (`hud.py`) has now gone missing twice, on 29 and 30 August
2026. Both times the reported symptom was the same: the hotkey works, the note
records and lands on the clipboard, and nothing is drawn on screen. Both times
the drawing code turned out to be fine. This file exists so the third time costs
minutes instead of an evening.

## Why it keeps happening

**The pill's problem has never been the code. It has been the age of the
window.**

The same `hud.py` renders perfectly in a process launched a minute ago and not
at all in the installed copy that has been running for hours. That is the whole
shape of the bug, and it is worth stating plainly because it makes the obvious
test worthless: rebuilding and installing always appears to fix it, because
rebuilding starts a new process with a new window. Feeling relieved at that
point is how this came back a second time.

macOS takes a borderless panel away from an app on its own across:

- a display sleep or wake
- a display reconfiguration, so a resolution change or a display attached or removed
- a Space change, including a swipe to or from a full screen app

and it does all of that to the window without telling the app. No notification
is posted, no delegate method fires, nothing is observable. So the app's own
state says the pill is up while AppKit's `isVisible` says it is not, and nothing
reconciles the two unless something explicitly asks. An app that has been up for
a day has been through all three many times. An app launched two minutes ago has
been through none of them.

The same events also reset the panel's **level** and **collection behavior**. So
ordering the window front again is not sufficient on its own: the level and the
behavior have to be reasserted with it, or the pill comes back on one Space only
and vanishes again the next time you swipe.

## What is in place now, as of 30 August 2026

1. **Collection behavior is `CanJoinAllSpaces | FullScreenAuxiliary`, which is
   257.** `NSWindowCollectionBehaviorStationary` used to be in there and was
   wrong: its documented meaning is "stays visible and stationary, like the
   desktop window", and the desktop window is the one thing that belongs to a
   single Space. A level 25 panel already floats above Expose without asking.
2. **Timers run in `NSRunLoopCommonModes`** (`_Ticker`). `rumps.Timer` registers
   itself in `NSDefaultRunLoopMode` alone, so every rumps timer stops dead for as
   long as a menu is open or a window is being dragged. That froze the pointer
   following and the reclaim at exactly the moments they were most needed.
3. **`_up` plus `_reclaim`.** The HUD keeps its own answer to "should a pill be
   on screen right now", separate from anything the window claims, and the 40ms
   timer that follows the pointer also checks the real window and puts it back
   with its level and behavior when it has gone. Rate limited to twice a second.
4. **The reclaim logs one line per pill** to `~/.clipbridge/mac_debug.log`.

## Tried, and rejected: a fresh panel for every pill

The obvious response to "the window goes stale" is to build a new one for each
pill and throw it away afterwards. It was written, and then taken back out on
30 August 2026, because a panel cannot be disposed of cleanly from PyObjC:

- `orderOut:` alone does not release it. AppKit keeps every window it has been
  handed, so the dead panels pile up one per voice note, both in `NSApp.windows`
  and in the window server. Measured: 1, 2, 3, 4, 5 across five cycles.
- `close()` with `setReleasedWhenClosed_(False)` leaks exactly the same way,
  because nothing is left to balance AppKit's retain.
- `close()` with `setReleasedWhenClosed_(True)` frees it and then segfaults,
  since PyObjC's proxy releases an object AppKit has already released.

So one panel is built lazily and kept, and `_reclaim` is what keeps it honest.
If you are tempted to try this again, the leak is measurable in about a minute:
run a few show and hide cycles and count `NSApplication.sharedApplication()
.windows()`, or count this process's windows in `CGWindowListCopyWindowInfo`.

## Debugging it next time, in order

### 1. How old is the running app?

```sh
ps -o lstart=,etime= -p "$(pgrep -f '/Applications/ClipBridge.app' | head -1)"
```

If it has been up for hours, this is very likely the stale window class of bug
again. If it was launched minutes ago and the pill is still missing, it is
something new, and the drawing code is back on the table.

### 2. Is the drawing broken, or is the window stale?

```sh
mac/.venv/bin/python3 mac/tools/hud_demo.py
```

This runs the pill from source in a throwaway accessory process that has never
slept or changed Space. Three pills appear over about ten seconds. If you see
them, the code is fine and the installed app's window is the problem.

### 3. Ask the window server what it sees

```sh
mac/.venv/bin/python3 mac/tools/hud_probe.py 120
```

Then press the hotkey and record a short note. This reads the window server
rather than the app, so it sees the panel even when the app is wrong about it.
Read the result like this:

| What the probe shows | What it means |
| --- | --- |
| No window at all while recording | The panel is never being built. Look for a `hud ... failed` traceback in `~/.clipbridge/mac_debug.log`, or a `"hud": "off, ..."` line in the startup block at the top of it. |
| `onscreen=False` with `alpha=1.0` | The window server took the panel away and the reclaim is not winning it back. This is the classic failure, and it is what both August incidents looked like. |
| `onscreen=False` with `alpha=0.0` | A normal completed fade out. The pill was shown and dismissed. If it was dismissed too fast, suspect the input tally in `_on_follow` firing on a keystroke that should not have counted. |
| `onscreen=True` but you cannot see it | Not a visibility problem, a placement one. Compare the rect against the `screens:` line the probe prints first. |
| An X far outside every screen | The app cannot produce this. `_position` clamps to a screen's visible frame, so a rect at X = -5088 on a single 1440 wide display is the window server sliding the panel with a Space transition. That means the panel is bound to one Space, so the collection behavior is not in effect. |

### 4. Read the log

```sh
cat ~/.clipbridge/mac_debug.log
```

The first block is written fresh at every launch and says whether the HUD was
built at all (`"hud": "on"`). After it, `hotkey fired` lines confirm the hotkey
reached the app, and any `hud <time> reclaiming: ...` line reports the exact
state a panel was in when it had to be chased: its `visible`, `onActiveSpace`,
`level`, and `behavior` at that moment. One such line per pill. A `behavior=0` or
`level=0` in that line is the window server having reset the panel.

## Things that look like the bug and are not

- **`hud.py` importing or the class constructing.** `"hud": "on"` in the log only
  means the object exists. The panel is built lazily on the first pill, so every
  interesting failure happens well after that line is written.
- **A rebuild fixing it.** It always does, briefly. See above.
- **The app being wedged.** In both incidents the hotkey, the recording, the
  transcription and the clipboard all kept working perfectly. Only the window was
  gone.
