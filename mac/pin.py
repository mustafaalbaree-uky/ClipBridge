"""
Pinning a voice note to a text field.

A pin types a marker such as ⟦note 3⟧ at the cursor, where the transcript
is meant to go. Everything around the marker is yours to keep typing. When
the transcript comes back, the marker is found again through the
accessibility API and replaced in place, so the text on either side stays
where you left it, and the app holding the field is never brought forward.

A pin is set two ways: the pin hotkey marks the text field that has focus,
and control option click marks the field under the pointer. The click is
taken off the event stream by an event tap and replayed as a plain click,
which is what puts the cursor there; the modifiers never reach the app, so
a browser does not read it as a right click and open a context menu.

Everything here needs Accessibility (System Settings > Privacy & Security >
Accessibility). Without it the tap cannot be created, keystrokes cannot be
posted, and fields cannot be read, so pinning stays off.

Chromium and Gecko build their accessibility tree only when something asks
for it, which AXManualAccessibility and AXEnhancedUserInterface do.

A note whose transcript already exists when it is pinned gets no marker: the
words are typed at the cursor, which works in any app that takes typing.

Gecko (Zen, Firefox) must never be sent AXSelectedText. Its handler passes
the selection's length where an end offset belongs, so with a marker at
offset 42 it deletes everything from offset 8 to the marker and then fails
the insert. There the marker is selected and the transcript typed over it.

In a terminal the marker is swapped by keystrokes: the cursor is walked back
to the marker, checked against the screen text to be sitting right after it,
and only then is the marker backspaced away and the transcript typed. When
that cannot be checked the marker stays, and pin_hook.py covers Claude Code.
"""

import re
import threading
import time

try:
    import ApplicationServices as AS
    import Quartz as Q
    from AppKit import NSRunningApplication, NSWorkspace
    from PyObjCTools import AppHelper
    HAS_AX = True
except Exception:
    HAS_AX = False

# Marks the events this module posts, so the tap lets its own replayed click
# through instead of reading it as another pin.
_EVENT_TAG = 0x434C4250

# Apps whose text box is drawn by whatever runs inside them, so a marker
# typed there cannot be found and replaced from outside. Claude Code in one
# of these gets its transcript from pin_hook.py when the prompt is sent.
TERMINALS = {'com.apple.Terminal', 'com.googlecode.iterm2',
             'com.mitchellh.ghostty', 'dev.warp.Warp-Stable',
             'net.kovidgoyal.kitty', 'org.alacritty',
             'com.github.wez.wezterm'}

# Browsers on Gecko, where AXSelectedText deletes the wrong text.
GECKO = {'app.zen-browser.zen', 'org.mozilla.firefox',
         'org.mozilla.firefoxdeveloperedition', 'org.mozilla.nightly',
         'org.mozilla.floorp', 'io.gitlab.librewolf-community.librewolf',
         'net.waterfox.waterfox', 'org.torproject.torbrowser',
         'net.mullvad.mullvadbrowser'}

# Roles a marker can be typed into.
_TEXT_ROLES = {'AXTextField', 'AXTextArea', 'AXComboBox', 'AXSearchField'}

# How long placing a pin keeps looking for the marker it typed, and how many
# elements one search of a page may visit.
_PLACE_WAIT_SEC = 3.0
_SEARCH_BUDGET  = 5000

# How long a replaced marker is given to show up as changed. Chromium applies
# accessibility edits in the renderer, so a read straight after a write can
# still see the old value.
_VERIFY_SEC = 4.0

# How many times the marker is selected before giving up on a field whose
# text keeps moving under it.
_SELECT_TRIES = 10

# A field being typed in is swapped only once its text has held still this
# long, and is waited on for at most _QUIET_MAX_SEC. A keystroke that lands
# while the marker is selected, or while the app is still applying the
# selection, splits the marker and the transcript goes in beside the pieces.
_QUIET_SEC     = 0.5
_QUIET_MAX_SEC = 120.0

# A terminal swap waits this long for its window to be in front with the
# marker on screen before the cursor, and tries this many cursor moves to
# land right after the marker.
_TERM_WAIT_SEC  = 90.0
_TERM_MOVES     = 8
# Drawn by Claude Code around its input box. One between the marker and the
# cursor means the marker is in the scrollback, not in what is being typed.
_TERM_RULES     = '\u2500\u2501\u2502\u256d\u256e\u256f\u2570'

_KEY_LEFT, _KEY_RIGHT, _KEY_BACKSPACE = 123, 124, 51


def trusted(prompt=False):
    if not HAS_AX:
        return False
    if prompt:
        return bool(AS.AXIsProcessTrustedWithOptions(
            {AS.kAXTrustedCheckOptionPrompt: True}))
    return bool(AS.AXIsProcessTrusted())


def _attr(el, name):
    try:
        err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
        return value if err == 0 else None
    except Exception:
        return None


def _set(el, name, value):
    try:
        return AS.AXUIElementSetAttributeValue(el, name, value) == 0
    except Exception:
        return False


def _u16(s):
    """Accessibility ranges count UTF-16 units, not Python characters."""
    return len(s.encode('utf-16-le')) // 2


def _get_range(el):
    ref = _attr(el, 'AXSelectedTextRange')
    if ref is None:
        return None
    try:
        ok, rng = AS.AXValueGetValue(ref, AS.kAXValueCFRangeType, None)
        return (rng[0], rng[1]) if ok else None
    except Exception:
        return None


def _set_range(el, loc, length):
    try:
        ref = AS.AXValueCreate(AS.kAXValueCFRangeType, (loc, length))
    except Exception:
        return False
    return _set(el, 'AXSelectedTextRange', ref)


def _wake_app(pid):
    """Ask a browser or Electron app to build its accessibility tree."""
    app = AS.AXUIElementCreateApplication(pid)
    if not _set(app, 'AXManualAccessibility', True):
        _set(app, 'AXEnhancedUserInterface', True)
    return app


def _focused():
    """(pid, app name, focused element) for whatever has keyboard focus."""
    system = AS.AXUIElementCreateSystemWide()
    app = _attr(system, 'AXFocusedApplication')
    if app is None:
        return None, None, None
    try:
        err, pid = AS.AXUIElementGetPid(app, None)
    except Exception:
        return None, None, None
    app = _wake_app(pid)
    name = _attr(app, 'AXTitle')
    el = None
    # the tree of an app woken just now can take a moment to appear
    for _ in range(10):
        el = _attr(app, 'AXFocusedUIElement')
        if el is not None:
            break
        time.sleep(0.1)
    return pid, name, el


def _post(ev):
    Q.CGEventSetIntegerValueField(ev, Q.kCGEventSourceUserData, _EVENT_TAG)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)


def type_text(text):
    """Type text at the cursor as keystrokes carrying no modifiers, even
    while the pin hotkey's own modifiers are still held down."""
    src = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
    for i in range(0, len(text), 16):
        chunk = text[i:i + 16]
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(src, 0, down)
            Q.CGEventKeyboardSetUnicodeString(ev, _u16(chunk), chunk)
            Q.CGEventSetFlags(ev, 0)
            _post(ev)
        time.sleep(0.01)


def press(code, times=1):
    """Press one key, with no modifiers, times over."""
    src = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
    for _ in range(times):
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(src, code, down)
            Q.CGEventSetFlags(ev, 0)
            _post(ev)
        time.sleep(0.004)


def flat(text):
    """Text safe to send as keystrokes: a typed newline would send a chat
    message or run a shell line, so each becomes a space."""
    return re.sub(r'\s*[\r\n]+\s*', ' ', text).strip()


def _front_pid():
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return front.processIdentifier() if front is not None else None


def _index(value, u16):
    """The Python index in value of an accessibility offset."""
    return len(value.encode('utf-16-le')[:u16 * 2].decode('utf-16-le', 'ignore'))


def click(x, y):
    src = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
    for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
        ev = Q.CGEventCreateMouseEvent(src, kind, (x, y), Q.kCGMouseButtonLeft)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, 1)
        Q.CGEventSetFlags(ev, 0)
        _post(ev)
        time.sleep(0.02)


class Pin:
    """Where one note's transcript goes."""

    def __init__(self, marker):
        self.marker  = marker
        self.pid     = None
        self.app     = None
        self.element = None
        self.terminal = False
        self.gecko    = False
        # set once the marker has been typed, so a transcript that comes back
        # first waits for it rather than looking for a marker not there yet
        self.placed  = threading.Event()

    def place(self, at=None, log=None):
        """Type the marker, clicking at `at` first when the pin came from a
        click. Runs off the main thread: it sleeps, and accessibility calls
        into another app can stall."""
        try:
            if at is not None:
                click(*at)
                time.sleep(0.15)
            self.pid, self.app, self.element = _focused()
            self._classify()
            type_text(self.marker)
            if not self.terminal:
                found = self._find(_PLACE_WAIT_SEC)
                if found is not None:
                    self.element = found
            if log:
                value = _attr(self.element, 'AXValue') if self.element else None
                log(f'pin {self.marker} in {self.app}: '
                    f'role={_attr(self.element, "AXRole") if self.element else None} '
                    f'marker_readable={isinstance(value, str) and self.marker in value} '
                    f'terminal={self.terminal}')
        except Exception as e:
            if log:
                log(f'pin {self.marker} failed to place: {e}')
        finally:
            self.placed.set()

    def _classify(self):
        if self.pid is None:
            return
        running = NSRunningApplication \
            .runningApplicationWithProcessIdentifier_(self.pid)
        bundle = running.bundleIdentifier() if running is not None else None
        self.terminal = bundle in TERMINALS
        self.gecko    = bundle in GECKO

    def type_now(self, text, at=None, log=None):
        """For a transcript that already exists: type it at the cursor, with
        no marker. True once the keystrokes are sent. Off the main thread."""
        try:
            if at is not None:
                click(*at)
                time.sleep(0.15)
            self.pid, self.app, self.element = _focused()
            type_text(flat(text))
            if log:
                log(f'pin typed at the cursor in {self.app}')
            return True
        except Exception as e:
            if log:
                log(f'pin failed to type: {e}')
            return False
        finally:
            self.placed.set()

    def _has_marker(self, el):
        value = _attr(el, 'AXValue') if el is not None else None
        return isinstance(value, str) and self.marker in value

    def _find(self, wait_sec=0.0):
        """The element holding the marker, or None. A browser woken for the
        first time reports a bare container as focused until its page tree
        is built, so this keeps looking for up to wait_sec: first at what has
        focus, then through the page around it."""
        deadline = time.time() + wait_sec
        while True:
            app = _wake_app(self.pid)
            focused = _attr(app, 'AXFocusedUIElement')
            for el in (self.element, focused):
                if self._has_marker(el):
                    return el
            found = self._search(focused)
            if found is not None or time.time() >= deadline:
                return found
            time.sleep(0.2)

    def _search(self, start):
        """Walk the web page, or failing that the window, holding start for
        a text element whose value carries the marker."""
        root, el = None, start
        for _ in range(30):
            if el is None:
                break
            role = _attr(el, 'AXRole')
            if role == 'AXWebArea':
                root = el
                break
            if role == 'AXWindow':
                root = root or el
                break
            el = _attr(el, 'AXParent')
        if root is None:
            return None
        budget = [_SEARCH_BUDGET]

        def walk(node, depth):
            budget[0] -= 1
            if budget[0] < 0 or depth > 80:
                return None
            if _attr(node, 'AXRole') in _TEXT_ROLES and self._has_marker(node):
                return node
            for child in _attr(node, 'AXChildren') or []:
                hit = walk(child, depth + 1)
                if hit is not None:
                    return hit
            return None
        return walk(root, 0)

    def deliver(self, text, log=None, gone=None):
        """Replace the marker with text. 'pasted' once the field reads back
        with the marker gone and the text in, 'hook' for a marker left in a
        terminal for pin_hook.py, None when it went nowhere. Runs off the
        main thread.

        A field that could be taking keystrokes gets only its marker replaced.
        A browser's accessibility copy of a field trails what is being typed
        into it, so writing the whole value back would drop the newest words.
        A field nobody is typing in has its whole value written, since an
        unfocused field can accept a selection and then ignore it. Either way
        there is one attempt, never a second method after the first, since a
        slow one landing late would put the transcript in twice."""
        self.placed.wait(5)
        if self.terminal:
            outcome = self._in_terminal(text, gone)
            if log:
                log(f'pin {self.marker} {outcome} ({self.app})')
            return 'pasted' if outcome == 'delivered' else 'hook'
        # the field's element can be rebuilt by a page redrawing it, so look
        # for the marker again rather than trusting the element from placing
        el = self.element if self._has_marker(self.element) else None
        if el is None and self.pid is not None:
            el = self._find()
        if el is None:
            if log:
                log(f'pin {self.marker} not delivered: marker not found '
                    f'({self.app})')
            return None
        if self._typing_here(el):
            outcome = self._by_selection(el, text)
        else:
            outcome = self._by_value(el, text)
        if log:
            log(f'pin {self.marker} {outcome} ({self.app})')
        return 'pasted' if outcome == 'delivered' else None

    def _front_and_focused(self, el):
        return _front_pid() == self.pid and _attr(el, 'AXFocused') is not False

    def _typing_here(self, el):
        """Whether this field could be taking keystrokes right now: its app
        is in front and the field has focus, or its text is still changing."""
        if self._front_and_focused(el):
            return True
        before = _attr(el, 'AXValue')
        time.sleep(0.3)
        return _attr(el, 'AXValue') != before

    def _by_value(self, el, text):
        """Write the whole field back with the marker swapped out. Only for a
        field nobody is typing in, where a selection cannot be set."""
        value = _attr(el, 'AXValue')
        if not isinstance(value, str) or self.marker not in value:
            return 'marker gone before the swap'
        if not _set(el, 'AXValue', value.replace(self.marker, text, 1)):
            return 'not delivered: field would not take the text'
        if not self._landed(el, value, text, _VERIFY_SEC):
            return 'sent, not confirmed by reading the field back'
        return 'delivered'

    def _landed(self, el, before, text, wait_sec):
        """Whether the field now reads as `before` with the marker swapped
        for text. The text is checked together with a little of what sat on
        each side of the marker, so a transcript that went in next to a
        broken marker, or over the user's words, does not count. Typing that
        carries on at the end of the field does not spoil the match."""
        at = before.find(self.marker)
        left = before[max(0, at - 12):at]
        right = before[at + len(self.marker):][:12]
        probe = left + text + right
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            value = _attr(el, 'AXValue')
            if isinstance(value, str) and self.marker not in value \
                    and probe in value:
                return True
            time.sleep(0.05)
        return False

    def _wait_quiet(self, el):
        """Wait until the field's text has stopped changing. False when it
        never does within _QUIET_MAX_SEC."""
        deadline = time.time() + _QUIET_MAX_SEC
        last, since = _attr(el, 'AXValue'), time.time()
        while time.time() < deadline:
            time.sleep(0.05)
            value = _attr(el, 'AXValue')
            if value != last:
                last, since = value, time.time()
            elif time.time() - since >= _QUIET_SEC:
                return True
        return False

    def _unconfirmed(self, el, before, text):
        """The swap did not read back. When the words that stood before the
        marker are gone from the field as well, the app mishandled the edit
        the way Gecko does, and the field is written back whole."""
        at = before.find(self.marker)
        value = _attr(el, 'AXValue')
        if at > 0 and before[:at].strip() and isinstance(value, str) \
                and not value.startswith(before[:at]):
            if _set(el, 'AXValue', before.replace(self.marker, text, 1)):
                return 'not confirmed, text before the marker was lost and ' \
                       'the field was written back'
        return 'sent, not confirmed by reading the field back'

    def _caret_back(self, el, want, ahead):
        """Put the caret at offset want, which is `ahead` characters past
        the transcript. Gecko takes a caret position and then ignores it, so
        the position is read back, and arrow keys cover the rest."""
        # the app is still reporting the caret moves of the edit itself,
        # and one of those landing late would undo this
        time.sleep(0.15)
        _set_range(el, want, 0)
        time.sleep(0.15)
        now = _get_range(el)
        if now is None or now == (want, 0) or not 0 < ahead <= 400 \
                or not self._front_and_focused(el):
            return
        press(_KEY_RIGHT, ahead)

    def _by_selection(self, el, text):
        """Select the marker and type over it, which keeps undo and fires
        the page's input events the way typing does. The selection is read
        back before anything is replaced: the marker's position comes from a
        copy of the field that can be a few keystrokes old, and replacing a
        selection that is not exactly the marker would eat the user's text."""
        if not self._wait_quiet(el):
            return 'not delivered: the field never stopped changing'
        if self.gecko and not self._front_and_focused(el):
            # typing would land somewhere else, and nobody is typing here
            return self._by_value(el, text)
        # read once, before any try has selected the marker
        caret = _get_range(el)
        for _ in range(_SELECT_TRIES):
            value = _attr(el, 'AXValue')
            if not isinstance(value, str) or self.marker not in value:
                return 'marker gone before the swap'
            start = _u16(value[:value.find(self.marker)])
            length = _u16(self.marker)
            if not _set_range(el, start, length):
                return 'not delivered: field would not take a selection'
            selected = _attr(el, 'AXSelectedText')
            if (selected is not None and selected != self.marker) or \
                    _get_range(el) not in (None, (start, length)) or \
                    _attr(el, 'AXValue') != value:
                time.sleep(0.1)
                continue
            if self.gecko:
                # never AXSelectedText here, see the top of this file
                text = flat(text)
                if text:
                    type_text(text)
                else:
                    press(_KEY_BACKSPACE)
            elif not _set(el, 'AXSelectedText', text):
                return 'not delivered: field would not take the text'
            if not self._landed(el, value, text, _VERIFY_SEC):
                return self._unconfirmed(el, value, text)
            if caret is not None and caret[0] >= start + length:
                # typing after the marker carries on where it was
                self._caret_back(el, caret[0] + _u16(text) - length,
                                 _index(value, caret[0])
                                 - _index(value, start + length))
            return 'delivered'
        return 'not delivered: selection never matched the marker'


    # ── Terminals ──────────────────────────────────────────────────────────

    def _screen(self):
        """(screen text, cursor index) of the terminal, or None when the app
        does not report them or the screen moved between the two reads."""
        el = self.element
        first = _get_range(el)
        value = _attr(el, 'AXValue')
        if first is None or not isinstance(value, str) \
                or _get_range(el) != first:
            return None
        return value, _index(value, first[0])

    def _marker_before(self, value, cursor):
        """Index of the marker being typed around, or -1. It has to sit
        before the cursor with none of Claude Code's box between the two."""
        at = value.rfind(self.marker, 0, cursor)
        between = value[at + len(self.marker):cursor]
        if at < 0 or any(ch in _TERM_RULES for ch in between):
            return -1
        # a new line in what is being typed is indented, as Claude Code
        # draws it. Any other new line is output, or a fresh shell prompt,
        # and the marker above it has been run already.
        if re.search(r'\n(?!  )', between):
            return -1
        return at

    def _in_terminal(self, text, gone=None):
        """Swap the marker for text with keystrokes. Nothing is deleted until
        the screen shows the cursor sitting right after the marker. gone()
        says the marker has been sent already, so there is nothing to swap."""
        if self.element is None:
            return 'left for the hook: no terminal element'
        text = flat(text)
        deadline = time.time() + _TERM_WAIT_SEC
        last, since = None, time.time()
        while True:
            if time.time() > deadline:
                return 'left for the hook: terminal never ready for the swap'
            if gone is not None and gone():
                return 'left for the hook: marker already sent'
            time.sleep(0.1)
            if not self._front_and_focused(self.element):
                last = None
                continue
            screen = self._screen()
            if screen is None:
                last = None
                continue
            value, cursor = screen
            at = self._marker_before(value, cursor)
            if at < 0:
                last = None
                continue
            # what is being typed has to hold still, as in a text field
            typing = value[at:cursor]
            if typing != last:
                last, since = typing, time.time()
            elif time.time() - since >= _QUIET_SEC:
                break
        end = at + len(self.marker)
        origin, moved = cursor, 0
        for _ in range(_TERM_MOVES):
            if cursor == end:
                break
            if cursor > end:
                # wrapped lines put a newline and padding on screen that the
                # cursor does not step through, so this is a guess, and the
                # screen is read again after it
                step = max(1, len(re.sub(r'\s*\n\s*', ' ', value[end:cursor])))
                press(_KEY_LEFT, step)
                moved += step
            else:
                step = end - cursor
                press(_KEY_RIGHT, step)
                moved -= step
            screen = self._settled(cursor)
            if screen is None or screen[1] == cursor:
                # the keys are not moving this cursor
                break
            value, cursor = screen
            at = self._marker_before(value, cursor + len(self.marker))
            if at < 0:
                break
            end = at + len(self.marker)
        screen = self._screen()
        if screen is None or screen[0][:screen[1]][-len(self.marker):] != self.marker \
                or not self._front_and_focused(self.element):
            if screen is not None and screen[1] != origin:
                press(_KEY_RIGHT, max(0, moved))
            return 'left for the hook: cursor could not be put after the marker'
        press(_KEY_BACKSPACE, len(self.marker))
        type_text(text)
        press(_KEY_RIGHT, max(0, moved))
        return 'delivered'

    def _settled(self, was):
        """The screen once the cursor has moved off `was`, or after a wait."""
        deadline = time.time() + 0.5
        screen = None
        while time.time() < deadline:
            time.sleep(0.04)
            screen = self._screen()
            if screen is not None and screen[1] != was:
                time.sleep(0.04)
                return self._screen() or screen
        return screen


def focused_is(el):
    return bool(_attr(el, 'AXFocused'))


class ClickTap:
    """Takes control option clicks off the event stream when want() says a
    note is there to pin, and hands their location to on_click on the main
    thread. Every other click passes through untouched."""

    def __init__(self, want, on_click):
        self._want     = want
        self._on_click = on_click
        self._eat_up   = False
        self._tap      = None
        self._source   = None

    def start(self):
        if self._tap is not None:
            return True
        if not HAS_AX:
            return False
        mask = (Q.CGEventMaskBit(Q.kCGEventLeftMouseDown)
                | Q.CGEventMaskBit(Q.kCGEventLeftMouseUp))
        tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                 Q.kCGEventTapOptionDefault, mask,
                                 self._callback, None)
        if tap is None:
            return False
        self._tap    = tap
        self._source = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetMain(), self._source,
                             Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(tap, True)
        return True

    def _callback(self, proxy, type_, event, refcon):
        try:
            if type_ in (Q.kCGEventTapDisabledByTimeout,
                         Q.kCGEventTapDisabledByUserInput):
                Q.CGEventTapEnable(self._tap, True)
                return event
            if Q.CGEventGetIntegerValueField(
                    event, Q.kCGEventSourceUserData) == _EVENT_TAG:
                return event
            if type_ == Q.kCGEventLeftMouseDown:
                flags = Q.CGEventGetFlags(event)
                if (flags & Q.kCGEventFlagMaskControl
                        and flags & Q.kCGEventFlagMaskAlternate
                        and not flags & Q.kCGEventFlagMaskCommand
                        and self._want()):
                    self._eat_up = True
                    loc = Q.CGEventGetLocation(event)
                    AppHelper.callAfter(self._on_click, loc.x, loc.y)
                    return None
            elif type_ == Q.kCGEventLeftMouseUp and self._eat_up:
                self._eat_up = False
                return None
        except Exception:
            pass
        return event
