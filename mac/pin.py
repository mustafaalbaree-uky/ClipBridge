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

A pin can also send: once the transcript is in, Return is pressed, and only
after the transcript reads back in place. In Terminal.app that goes through
the tab itself, found again by its tty: AppleScript's `do script` writes to a
tab's input as if typed, with a Return after it, whether or not the window is
in front, so the marker is swapped and the prompt sent while you are in some
other app. What the tab shows is read back the same way, so nothing is typed
until the marker is the last thing in Claude Code's input box, and the
prompt is seen to leave the box afterwards, with Return pressed again while
it has not. That takes Automation access to Terminal, asked for the first
time a pin that sends is set there. In a text field Return is pressed
when the field's app is in front and the field has focus, and otherwise
posted to that app alone, and only while the field is still what has focus
inside it.
"""

import re
import subprocess
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

# Where a pin that sends can reach a tab without it being in front.
TERMINAL_APP = 'com.apple.Terminal'

# How long a sent prompt is given to leave the input box before Return is
# pressed again, and how many more Returns that comes to at most. Claude Code
# can take a burst of text as a paste, and then the Return that came with it
# is typed into the box as a new line.
_SENT_CHECK_SEC = 1.5
_EXTRA_RETURNS  = 2
# How long a text field is given to let go of a sent transcript, as a chat
# box does once it sends.
_FIELD_SENT_SEC = 2.0
# The most one burst typed into Claude Code carries. Near 800 characters it
# is taken as a paste.
_PIECE_BYTES    = 400
# How often a Terminal tab is read while waiting for its input box to hold
# still, how long one read may take, and how long the marker is waited for.
# The box is hidden while Claude Code asks a question, and a read costs a few
# milliseconds, so a tab can be waited on far longer than a window in front.
_TAB_POLL_SEC   = 0.25
_TAB_TIMEOUT    = 10
_TAB_WAIT_SEC   = 600.0
# The first Apple event to Terminal can wait on the Automation prompt.
_TAB_ASK_SEC    = 90

_KEY_LEFT, _KEY_RIGHT, _KEY_BACKSPACE, _KEY_RETURN = 123, 124, 51, 36
_CTRL_E, _DEL = '\x05', '\x7f'


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


def post_to(pid, code):
    """Press one key in the app with this pid alone, in front or not."""
    src = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
    for down in (True, False):
        ev = Q.CGEventCreateKeyboardEvent(src, code, down)
        Q.CGEventSetFlags(ev, 0)
        Q.CGEventSetIntegerValueField(ev, Q.kCGEventSourceUserData, _EVENT_TAG)
        Q.CGEventPostToPid(pid, ev)
        time.sleep(0.004)


def flat(text):
    """Text safe to send as keystrokes: a typed newline would send a chat
    message or run a shell line, so each becomes a space."""
    return re.sub(r'\s*[\r\n]+\s*', ' ', text).strip()


def _plain(text):
    """flat, with any other control character made a space, for text
    written to a terminal's input, where one would act as a key."""
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', flat(text)).strip()


def _squash(s):
    """s without any spacing, so text a terminal wrapped onto several lines
    still matches the text it came from."""
    return re.sub(r'[\s ⠀]+', '', s)


def _tail(text):
    """The end of text, as it is looked for in an input box."""
    return _squash(text)[-24:]


def _rule(line):
    s = line.strip()
    return len(s) >= 10 and set(s) == {'─'}


def input_box(screen):
    """(text, True) inside Claude Code's input box, the part of the screen
    between the last two full width rules. Without those rules, (the
    screen, False), for a shell, where the input is the end of the screen."""
    return _split_screen(screen)[1:]


def _split_screen(screen):
    """(what is above the input box, the input box, whether it is Claude
    Code's), as input_box."""
    lines = screen.split('\n')
    rules = [i for i, line in enumerate(lines) if _rule(line)]
    if len(rules) >= 2:
        return ('\n'.join(lines[:rules[-2]]),
                '\n'.join(lines[rules[-2] + 1:rules[-1]]), True)
    return '', screen, False


def _pieces(text, limit=None):
    """text cut after spaces into pieces of at most limit bytes."""
    limit = limit or _PIECE_BYTES
    pieces, cur = [], ''
    for word in re.findall(r'\S*\s*', text):
        if cur and len((cur + word).encode('utf-8')) > limit:
            pieces.append(cur)
            cur = ''
        cur += word
    return pieces + [cur] if cur or not pieces else pieces


# ── Terminal.app tabs ─────────────────────────────────────────────────────────
# A tab is found by its tty, which stays the same while the tab is open, so a
# pin reaches the tab it was set in wherever that tab has gone since. Reads and
# writes go through one compiled script run in this process, a few
# milliseconds each; osascript takes a third of a second just to start.
# NSAppleScript is only safe on the main thread, so calls are handed there.

_TAB_SCRIPT = """
on tab_screen(wanted)
    tell application "Terminal"
        with timeout of 5 seconds
            set found to contents of (every tab of every window whose tty is wanted)
        end timeout
    end tell
    repeat with i from 1 to count of found
        if (count of item i of found) > 0 then return item 1 of item i of found
    end repeat
    return missing value
end tab_screen

on tab_send(wanted, txt)
    tell application "Terminal"
        with timeout of 5 seconds
            set found to (every tab of every window whose tty is wanted)
            repeat with i from 1 to count of found
                if (count of item i of found) > 0 then
                    do script txt in (item 1 of item i of found)
                    return "sent"
                end if
            end repeat
        end timeout
    end tell
    return missing value
end tab_send
"""
_tab_script = None


def _fourcc(code):
    return int.from_bytes(code.encode('ascii'), 'big')


def _on_main_wait(fn, timeout):
    """fn() run on the main thread, or None when it did not finish in time."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    out, done = [None], threading.Event()

    def run():
        try:
            out[0] = fn()
        finally:
            done.set()
    AppHelper.callAfter(run)
    return out[0] if done.wait(timeout) else None


def _tab_call(handler, *args):
    """The text a handler in _TAB_SCRIPT returns, or None. Main thread."""
    global _tab_script
    try:
        from Foundation import NSAppleEventDescriptor, NSAppleScript
        if _tab_script is None:
            script = NSAppleScript.alloc().initWithSource_(_TAB_SCRIPT)
            ok, _ = script.compileAndReturnError_(None)
            if not ok:
                return None
            _tab_script = script
        event = NSAppleEventDescriptor \
            .appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
                _fourcc('ascr'), _fourcc('psbr'),
                NSAppleEventDescriptor.currentProcessDescriptor(), -1, 0)
        event.setParamDescriptor_forKeyword_(
            NSAppleEventDescriptor.descriptorWithString_(handler),
            _fourcc('snam'))
        params = NSAppleEventDescriptor.listDescriptor()
        for i, arg in enumerate(args, 1):
            params.insertDescriptor_atIndex_(
                NSAppleEventDescriptor.descriptorWithString_(arg), i)
        event.setParamDescriptor_forKeyword_(params, _fourcc('----'))
        result, _ = _tab_script.executeAppleEvent_error_(event, None)
        return result.stringValue() if result is not None else None
    except Exception:
        return None


class TerminalTab:
    """One Terminal.app tab, read and typed into through AppleScript."""

    def __init__(self, tty):
        self.tty = tty

    @classmethod
    def front(cls):
        """The tab in front in Terminal's front window, or None, which is
        also what a refused Automation prompt comes to. Through osascript,
        so that waiting on the prompt holds up this thread and not the
        main one."""
        try:
            run = subprocess.run(
                ['osascript', '-e', 'tell application "Terminal" to get tty '
                 'of selected tab of front window'],
                capture_output=True, text=True, encoding='utf-8',
                timeout=_TAB_ASK_SEC)
        except Exception:
            return None
        tty = run.stdout.strip() if run.returncode == 0 else ''
        return cls(tty) if tty.startswith('/dev/') else None

    def screen(self):
        return _on_main_wait(lambda: _tab_call('tab_screen', self.tty),
                             _TAB_TIMEOUT)

    def send(self, text):
        """Type text into the tab, followed by Return."""
        return _on_main_wait(lambda: _tab_call('tab_send', self.tty, text),
                             _TAB_TIMEOUT) == 'sent'


def _sent_from_box(read, enter, looking_for, before):
    """Whether Claude Code's input box let go of what was just sent, with
    `before` the box as it read just before that Return. Sent is the box
    redrawn without looking_for or a paste in it, or looking_for showing
    above the box as a sent prompt. While the box still holds it, which is
    a Return that became a new line, Return is pressed again. Return in an
    empty box does nothing, so a press that was not needed does no harm.
    Only for Claude Code: in a shell a second Return would go to whatever
    the first one started, so there the answer is False straight away."""
    before = _squash(before)
    for attempt in range(_EXTRA_RETURNS + 1):
        if attempt and not enter():
            return False
        deadline = time.time() + _SENT_CHECK_SEC
        while time.time() < deadline:
            time.sleep(0.15)
            screen = read()
            if screen is None:
                return False
            above, box, is_box = _split_screen(screen)
            if not is_box:
                return False
            box = _squash(box)
            if looking_for in box or '[Pastedtext' in box:
                before = box
                continue
            if box != before or looking_for in _squash(above):
                return True
    return False


def _bundle_of(pid):
    """The bundle id of the app a process runs from, read from its
    executable path. NSRunningApplication answers from a list NSWorkspace
    keeps up to date on the main run loop, and can come back empty."""
    try:
        import ctypes
        from Foundation import NSBundle
        buf = ctypes.create_string_buffer(4096)
        n = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(
            int(pid), buf, ctypes.c_uint32(len(buf)))
        if n <= 0:
            return None
        path = buf.value.decode('utf-8', 'replace')
        at = path.find('.app/')
        if at < 0:
            return None
        bundle = NSBundle.bundleWithPath_(path[:at + 4])
        return bundle.bundleIdentifier() if bundle is not None else None
    except Exception:
        return None


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
    """Where one note's transcript goes, and whether Return follows it.

    deliver and type_now come back with 'pasted', or for a pin that sends,
    'sent' once the prompt or chat box let go of the text, 'pressed' when
    Return was pressed and that could not be seen, and 'unsent' when the
    text went in and Return was not pressed. 'hook' is a marker left in a
    terminal for pin_hook.py, 'hook-sent' a prompt sent with its marker
    still in it, which pin_hook.py fills in, and None is a transcript that
    went nowhere."""

    def __init__(self, marker, send=False):
        self.marker  = marker
        self.send    = send
        self.pid     = None
        self.app     = None
        self.bundle  = None
        self.element = None
        self.terminal = False
        self.gecko    = False
        # the Terminal.app tab a pin that sends goes back to
        self.tab      = None
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
            elif self.send and self.bundle == TERMINAL_APP:
                self.tab = self._own_tab()
            if log:
                value = _attr(self.element, 'AXValue') if self.element else None
                log(f'pin {self.marker} in {self.app}: '
                    f'role={_attr(self.element, "AXRole") if self.element else None} '
                    f'marker_readable={isinstance(value, str) and self.marker in value} '
                    f'terminal={self.terminal} send={self.send} '
                    f'tab={self.tab.tty if self.tab else None}')
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
        self.bundle   = running.bundleIdentifier() if running is not None else None
        if self.bundle is None:
            self.bundle = _bundle_of(self.pid)
        self.terminal = self.bundle in TERMINALS
        self.gecko    = self.bundle in GECKO

    def _own_tab(self):
        """The Terminal tab in front, once its screen shows this marker, so
        the tab is the one it was typed into. None without Automation
        access, which the first call asks for."""
        tab = TerminalTab.front()
        if tab is None:
            return None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            screen = tab.screen()
            if screen is not None and self.marker in screen:
                return tab
            time.sleep(0.2)
        return None

    def type_now(self, text, at=None, log=None):
        """For a transcript that already exists: type it at the cursor, with
        no marker, and press Return after it for a pin that sends. One of
        the results listed on the class, or None when nothing was typed.
        Off the main thread."""
        try:
            if at is not None:
                click(*at)
                time.sleep(0.15)
            self.pid, self.app, self.element = _focused()
            self._classify()
            if self.send and self.bundle == TERMINAL_APP:
                self.tab = TerminalTab.front()
            if self.tab is not None:
                screen = self.tab.screen()
                is_box = screen is not None and input_box(screen)[1]
                what, result = self._type_in_tab(_plain(text), 0, is_box)
                if log:
                    log(f'pin typed through {self.tab.tty}: {what} ({self.app})')
                return result
            type_text(flat(text))
            result = 'pasted'
            if self.send:
                result = self._return_in_terminal(text) if self.terminal \
                    else self._return_in_field(self.element, text)
            if log:
                log(f'pin typed at the cursor in {self.app}: {result}')
            return result
        except Exception as e:
            if log:
                log(f'pin failed to type: {e}')
            return None
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
        """Replace the marker with text, then press Return for a pin that
        sends. 'pasted' once the field reads back with the marker gone and
        the text in; the other results are listed on the class. Runs off the
        main thread.

        A field that could be taking keystrokes gets only its marker replaced.
        A browser's accessibility copy of a field trails what is being typed
        into it, so writing the whole value back would drop the newest words.
        A field nobody is typing in has its whole value written, since an
        unfocused field can accept a selection and then ignore it. Either way
        there is one attempt, never a second method after the first, since a
        slow one landing late would put the transcript in twice."""
        self.placed.wait(5)
        sending = self.send and bool(text)
        if self.tab is not None and sending:
            what, result = self._send_in_tab(text, gone)
            if log:
                log(f'pin {self.marker} through {self.tab.tty}: {what} '
                    f'({self.app})')
            return result
        if self.terminal:
            outcome = self._in_terminal(text, gone)
            if log:
                log(f'pin {self.marker} {outcome} ({self.app})')
            if outcome != 'delivered':
                return 'hook'
            if not sending:
                return 'pasted'
            result = self._return_in_terminal(text)
            if log:
                log(f'pin {self.marker} Return: {result}')
            return result
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
        if outcome != 'delivered':
            return None
        if not sending:
            return 'pasted'
        result = self._return_in_field(el, text)
        if log:
            log(f'pin {self.marker} Return: {result}')
        return result

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

    # ── Return ─────────────────────────────────────────────────────────────

    def _return_in_field(self, el, text):
        """Return in el once the transcript reads back in it and the field
        has held still. In front it is a keystroke. Behind, it is posted to
        the field's app alone, and only while the field is still what has
        focus inside that app. 'sent' when the field then lets go of the
        text, as a chat box does once it sends, 'pressed' when it keeps it,
        as a search field does, 'unsent' when Return was not pressed."""
        looking_for = _tail(flat(text))
        value = _attr(el, 'AXValue') if el is not None else None
        readable = isinstance(value, str)
        if readable:
            deadline = time.time() + _VERIFY_SEC
            while looking_for not in _squash(value or ''):
                if time.time() > deadline:
                    return 'unsent'
                time.sleep(0.05)
                value = _attr(el, 'AXValue')
            if not self._wait_quiet(el) or \
                    looking_for not in _squash(_attr(el, 'AXValue') or ''):
                return 'unsent'
        if el is None:
            # typed where nothing could be read, so Return follows the
            # keystrokes into whatever is in front, if it is still this app
            if _front_pid() != self.pid:
                return 'unsent'
            time.sleep(0.2)
            press(_KEY_RETURN)
            return 'pressed'
        if self._front_and_focused(el):
            press(_KEY_RETURN)
        elif self.pid is not None and \
                _attr(_wake_app(self.pid), 'AXFocusedUIElement') == el:
            post_to(self.pid, _KEY_RETURN)
        else:
            return 'unsent'
        if not readable:
            return 'pressed'
        deadline = time.time() + _FIELD_SENT_SEC
        while time.time() < deadline:
            time.sleep(0.1)
            now = _attr(el, 'AXValue')
            if isinstance(now, str) and looking_for not in _squash(now):
                return 'sent'
        return 'pressed'

    def _return_in_terminal(self, text):
        """Return by keystroke after the transcript went into a terminal in
        front: once the screen shows it and has held still, and only while
        the window is still in front with its text area focused."""
        el = self.element
        looking_for = _tail(flat(text))
        read = lambda: _attr(el, 'AXValue') if el is not None else None
        value, box = read(), ''
        if isinstance(value, str):
            deadline = time.time() + _VERIFY_SEC
            last, since = None, time.time()
            while True:
                if time.time() > deadline:
                    return 'unsent'
                time.sleep(0.05)
                value = read()
                box = input_box(value)[0] if isinstance(value, str) else ''
                if looking_for not in _squash(box):
                    last = None
                    continue
                if box != last:
                    last, since = box, time.time()
                elif time.time() - since >= 0.3:
                    break
        if el is None or not self._front_and_focused(el):
            return 'unsent'
        press(_KEY_RETURN)
        if not isinstance(value, str):
            return 'pressed'

        def enter():
            if not self._front_and_focused(el):
                return False
            press(_KEY_RETURN)
            return True
        return 'sent' if _sent_from_box(read, enter, looking_for, box) \
            else 'pressed'

    # ── Through a Terminal.app tab ─────────────────────────────────────────

    def _send_in_tab(self, text, gone=None):
        """Swap the marker and send the prompt through the tab it was set in,
        in front or not. The tab's cursor cannot be read this way, so nothing
        is typed until the marker is the last thing in the input box and the
        box has held still, which leaves the cursor right after it. Returns
        (what happened, result)."""
        text = _plain(text)
        deadline = time.time() + _TAB_WAIT_SEC
        last, since = None, time.time()
        while True:
            if gone is not None and gone():
                return 'marker already sent', 'hook'
            screen = self.tab.screen()
            if screen is None:
                return 'tab closed or unreadable', 'hook'
            box, is_box = input_box(screen)
            if box != last:
                last, since = box, time.time()
            elif self.marker in box and time.time() - since >= _QUIET_SEC:
                break
            if time.time() > deadline:
                return 'the input box never held the marker still', 'hook'
            time.sleep(_TAB_POLL_SEC)
        after = box[box.rfind(self.marker) + len(self.marker):]
        if not after.strip():
            what, result = self._type_in_tab(text, len(self.marker), is_box)
            return what, result or 'hook'
        if not is_box:
            return 'text after the marker, left for the hook', 'hook'
        # text typed after the marker could have the cursor anywhere in it,
        # so the marker stays, and pin_hook.py fills it in once this is sent
        if not self.tab.send(''):
            return 'Terminal did not take Return', 'hook'
        if _sent_from_box(self.tab.screen, lambda: self.tab.send(''),
                          _squash(self.marker), box):
            return 'sent with the marker in it, for the hook', 'hook-sent'
        return 'Return pressed with the marker in it, not confirmed', 'hook'

    def _type_in_tab(self, text, erase, is_box):
        """Type text into the tab, taking `erase` characters off before the
        cursor first, and press Return. Returns (what happened, result),
        with None for a result when nothing was typed.

        Claude Code takes one burst of about 800 characters or more as a
        paste: it hands that to Claude as pasted content rather than as
        your words, loses what follows a backspace in the same burst, and
        turns the Return after it into a new line. So a long transcript goes
        into Claude Code's input box in pieces. Each piece but the last ends
        in a backslash, which with the Return that follows makes a new line
        rather than sending, and the next piece starts by deleting that new
        line. A short burst loses the backslash to the new line, and a burst
        of a few hundred characters keeps it, so what the box shows decides
        whether the backslash is deleted too. In a shell a backslash would
        carry the command on to the next line, so there it is one burst."""
        pieces = _pieces(text) if is_box else [text]
        head = (_CTRL_E + _DEL * erase) if erase else ''
        typed = ''
        before = ''
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            if last:
                before = input_box(self.tab.screen() or '')[0]
            if not self.tab.send(head + piece + ('' if last else '\\')):
                if i == 0:
                    return 'Terminal did not take the text', None
                return f'Terminal stopped taking text after piece {i}', 'unsent'
            typed += piece
            if last:
                break
            box = self._box_shows(_tail(typed))
            if box is None:
                return f'piece {i + 1} of {len(pieces)} never showed', 'unsent'
            head = _DEL * (2 if box.endswith('\\') else 1)
        if not is_box:
            return 'typed with Return', 'pressed'
        if _sent_from_box(self.tab.screen, lambda: self.tab.send(''),
                          _tail(text), before):
            return f'sent in {len(pieces)} piece(s)', 'sent'
        return 'Return pressed, prompt still in the input box', 'pressed'

    def _box_shows(self, looking_for, wait_sec=3.0):
        """The input box, squashed, once it shows looking_for and has held
        still, or None."""
        deadline = time.time() + wait_sec
        last = None
        while time.time() < deadline:
            time.sleep(0.05)
            screen = self.tab.screen()
            box = _squash(input_box(screen)[0]) if screen is not None else ''
            if looking_for not in box:
                last = None
            elif box == last:
                return box
            else:
                last = box
        return None


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
