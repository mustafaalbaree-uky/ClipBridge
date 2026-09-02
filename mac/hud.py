"""
On screen state pill for ClipBridge voice notes.

A small black pill wrapped in a shifting rainbow ring. It drifts along
slowly while recording (with a live elapsed counter), speeds up while
transcribing, and flashes short confirmations. It replaces the
notification banners for the whole voice note flow.

When a note lands on the clipboard the pill opens into two lines: a
quiet "copied to clipboard" caption over the first few words of what
you actually said. Those words are the hero, so they are the thing
carrying the spectrum: the rainbow moves out of the ring, which drops to
a faint halo, and into the glyphs themselves, which rise and bloom into
place and hold a slow drift of colour while you read them. One rainbow,
on the one element that matters, which is the point of showing the words
at all: proof it heard you, not just proof it finished.

The panel never takes focus and ignores every click, so it can sit over
anything without getting in the way. A pill that is waiting to time out
also leaves the moment you press a key or click, because the first thing
anyone does with a fresh transcript is paste it, and reading is over by
then. Moving the pointer is not pressing anything and never dismisses
it. It rides with the pointer: centered
just below the cursor, following it while it is on screen, and clamped
into the visible frame of whichever display the cursor is on, so a
pointer in a corner still leaves the whole pill readable. When there is
no room below the cursor it flips above instead.

Nothing here trusts a remembered "is it on screen" flag, and nothing
trusts the window either. macOS orders a borderless panel out on its own
across a display sleep, a display reconfiguration, or a Space change, and
it does that to the window without telling the app, so no notification
fires and the pill is simply gone for the rest of the process's life.
A freshly launched copy has slept and switched Spaces zero times, which
is exactly why the pill looks perfect for an hour and is missing by the
next day. So the app keeps its own answer to "should a pill be visible
right now", and the timer that follows the pointer also asks the real
window whether it is still visible and still on the Space you are
looking at, putting it back with its level and collection behavior when
it is not. Every state entry writes a line to ~/.clipbridge/mac_debug.log
if it throws, so a broken pill can never be invisible in the log too.

When the pill goes missing anyway, mac/HUD_DEBUGGING.md is the runbook:
why this failure recurs, what each shape of it looks like from the window
server, and the two tools in mac/tools that tell them apart. Read it
before concluding anything about the drawing code, which has been
innocent every time so far.

Every public method is safe to call from any thread; work is bounced to
the main thread internally.
"""
import functools
import time
import traceback
from pathlib import Path

from AppKit import (NSColor, NSEvent, NSFont, NSPanel, NSScreen, NSWorkspace,
                    NSTextField, NSTextAlignmentCenter,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSKernAttributeName,
                    NSBackingStoreBuffered, NSStatusWindowLevel,
                    NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSAnimationContext)
from Foundation import (NSMakeRect, NSAttributedString, NSTimer, NSRunLoop,
                        NSRunLoopCommonModes)
from Quartz import (CALayer, CAGradientLayer, CAShapeLayer, CATextLayer,
                    CABasicAnimation, CAMediaTimingFunction,
                    kCAMediaTimingFunctionLinear,
                    kCAMediaTimingFunctionEaseOut,
                    CGPathCreateWithRoundedRect, CGRectMake, CGSizeMake,
                    CGEventSourceCounterForEventType,
                    kCGEventSourceStateHIDSystemState,
                    kCGEventKeyDown, kCGEventLeftMouseDown,
                    kCGEventRightMouseDown, kCGEventOtherMouseDown)
from PyObjCTools import AppHelper

_HEIGHT   = 34          # one line pill height in points
_PAD      = 20          # horizontal text padding inside the pill
_VPAD     = 14          # vertical padding of the two line pill
_GAP      = 4           # between the caption and the words under it
_MIN_W    = 132
_MAX_W    = 460         # a preview wider than this gets trimmed instead
_RING     = 2.5         # rainbow ring thickness
_EDGE_GAP = 14          # distance from the screen edge
_CURSOR   = 24          # gap between the pointer and the pill
_FOLLOW   = 0.04        # pointer catch up, and how soon a keypress is felt
_RECLAIM  = 0.5         # how often a pill that went missing is put back
_FADE_IN   = 0.18
_FADE_OUT  = 0.30
_FADE_QUIT = 0.10       # dismissed by input: get out of the way, do not linger
_BLOOM    = 0.75        # how long the words take to rise and settle
_RISE     = 9.0         # how far below their place the words start

_PREVIEW_WORDS = 8      # most words of the transcript ever shown
_RING_QUIET    = 0.26   # ring opacity while the words hold the spectrum

# Ride along to every Space, including another app's full screen one.
#
# Stationary used to be in here and was wrong. Its documented meaning is
# "unaffected by Expose, stays visible and stationary, like the desktop
# window", and the desktop window is the one thing that belongs to a
# single Space: the panel was seen sliding sideways with the Space
# transition, thousands of points off the only display this Mac has,
# which is what a Space bound window does and what a window on all
# Spaces never does. A level 25 panel already floats above Expose
# without asking, so nothing is lost by dropping it.
_BEHAVIOR = (NSWindowCollectionBehaviorCanJoinAllSpaces
             | NSWindowCollectionBehaviorFullScreenAuxiliary)

_LOG = Path.home() / '.clipbridge' / 'mac_debug.log'


def _log(msg):
    try:
        with open(_LOG, 'a') as f:
            f.write(f'\nhud {time.strftime("%H:%M:%S")} {msg}')
    except Exception:
        pass


def _guard(fn):
    """Main thread entry point. An exception inside one of these is
    handed to the run loop, which prints it to a stderr no bundled app
    has, so the pill would just stop appearing with nothing to read."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            _log(f'{fn.__name__} failed\n{traceback.format_exc()}')
    return wrapper


# Presses that count as "I am doing something else now". Button downs
# only, so moving the pointer never dismisses anything, and modifier keys
# are absent because holding command down on the way to a paste is part
# of the paste, not a separate act.
_INPUT_EVENTS = (kCGEventKeyDown, kCGEventLeftMouseDown,
                 kCGEventRightMouseDown, kCGEventOtherMouseDown)


def _input_count():
    """How many keys and clicks the hardware has seen since login. Read
    from the HID state, which counts what a human actually pressed and
    ignores anything another program synthesised. It is a bare tally with
    no event content, so it needs neither accessibility nor input
    monitoring permission, which is the whole reason for counting instead
    of tapping the event stream."""
    try:
        return sum(CGEventSourceCounterForEventType(
            kCGEventSourceStateHIDSystemState, t) for t in _INPUT_EVENTS)
    except Exception:
        return None


def _reduce_motion():
    try:
        return bool(NSWorkspace.sharedWorkspace()
                    .accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


# one full trip of the rainbow, seconds per loop
_SPEED_REC   = 4.0
_SPEED_BUSY  = 1.3
_SPEED_FLASH = 2.4
_SPEED_WORDS = 7.0      # through the glyphs, slow enough to read over


def _rainbow_cgcolors():
    """Two full hue cycles plus a closing color, so sliding the gradient
    by half its width lands on an identical frame and the loop is
    seamless."""
    cols = []
    for _ in range(2):
        for i in range(6):
            c = NSColor.colorWithHue_saturation_brightness_alpha_(
                i / 6.0, 0.9, 1.0, 1.0)
            cols.append(c.CGColor())
    cols.append(cols[0])
    return cols


class _Ticker:
    """A repeating timer that keeps firing while a menu is open, a window
    is being dragged, or a scroll is under way.

    rumps.Timer registers itself in NSDefaultRunLoopMode alone, and the
    main run loop leaves that mode for the whole of any event tracking.
    So every rumps timer stops dead while a menu is down, which is
    exactly when the pill most needs to be following the pointer, and
    exactly when the window server is most likely to have taken it away.
    Common modes covers tracking as well as the default."""

    def __init__(self, fn, interval):
        self._fn       = fn
        self._interval = interval
        self._timer    = None

    def start(self):
        if self._timer is not None:
            return
        self._timer = NSTimer.timerWithTimeInterval_repeats_block_(
            self._interval, True, lambda _t: self._fn())
        NSRunLoop.currentRunLoop().addTimer_forMode_(
            self._timer, NSRunLoopCommonModes)

    def stop(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None


def _attributed(text, font, color=None, kern=0.0):
    attrs = {NSFontAttributeName: font}
    if color is not None:
        attrs[NSForegroundColorAttributeName] = color
    if kern:
        attrs[NSKernAttributeName] = kern
    return NSAttributedString.alloc().initWithString_attributes_(text, attrs)


class RecordingHUD:
    def __init__(self):
        self._panel      = None
        self._label      = None      # single line states
        self._pill       = None      # black rounded backdrop layer
        self._ring       = None      # layer holding the gradient, masked
        self._ring_mask  = None      # stroke shaped mask for the ring
        self._grad       = None      # the moving rainbow in the ring
        self._caption    = None      # "copied to clipboard"
        self._bloom      = None      # the pair below, rising and fading in
        self._glow       = None      # halo cast from the glyph shapes
        self._wrap       = None      # masked to the words
        self._words_mask = None      # the transcript preview, as a mask
        self._words      = None      # the moving rainbow inside the words
        self._mode       = 'line'
        self._width      = _MIN_W
        self._height     = _HEIGHT
        self._speed      = _SPEED_REC
        self._rec_t0     = None
        self._tick       = _Ticker(self._on_tick, 0.5)
        self._hide_at    = None
        # the input tally at the moment a timed pill went up, or None
        # while nothing is waiting to be dismissed by a keypress
        self._input_at   = None
        self._hider      = _Ticker(self._on_hider, 0.2)
        self._follow     = _Ticker(self._on_follow, _FOLLOW)
        # bumped by every show and every hide, so a fade out that is
        # overtaken by a new show does not order the panel out from under it
        self._gen        = 0
        self._finisher   = None
        # a pill is meant to be on screen right now. Not "was shown", but
        # "belongs there", which is the thing the follow timer checks the
        # real window against
        self._up         = False
        self._reclaim_at = 0.0
        # whether this pill has already reported being chased
        self._said       = False

    # ── public, callable from any thread ───────────────────────────────────────

    def recording(self):
        AppHelper.callAfter(self._enter_recording)

    def processing(self):
        AppHelper.callAfter(self._enter_processing)

    def flash(self, text, seconds=1.8):
        AppHelper.callAfter(self._enter_flash, text, seconds)

    def copied(self, transcript, seconds=2.9):
        """The clipboard now holds transcript. Show its opening words."""
        AppHelper.callAfter(self._enter_copied, transcript, seconds)

    def hide(self):
        AppHelper.callAfter(self._fade_out)

    # ── states (main thread) ───────────────────────────────────────────────────

    @_guard
    def _enter_recording(self):
        self._ensure()
        self._hide_at   = None
        self._input_at  = None
        self._rec_t0    = time.monotonic()
        self._set_line('Recording  0:00')
        self._set_speed(_SPEED_REC)
        self._tick.start()
        self._fade_in()

    @_guard
    def _enter_processing(self):
        self._ensure()
        self._hide_at  = None
        self._input_at = None
        self._stop_tick()
        self._set_line('Transcribing…')
        self._set_speed(_SPEED_BUSY)
        self._fade_in()

    @_guard
    def _enter_flash(self, text, seconds):
        self._flash_line(text, seconds)

    def _flash_line(self, text, seconds):
        self._ensure()
        self._stop_tick()
        self._set_line(text)
        self._set_speed(_SPEED_FLASH)
        self._fade_in()
        self._arm_hider(seconds)

    @_guard
    def _enter_copied(self, transcript, seconds):
        self._ensure()
        self._stop_tick()
        preview = self._preview(transcript or '')
        if not preview:
            # nothing worth quoting, so say it the plain way
            self._flash_line('Copied to clipboard', 1.8)
            return
        self._set_copied(preview)
        self._set_speed(_SPEED_FLASH)
        self._fade_in()
        self._bloom_in()
        self._arm_hider(seconds)

    def _arm_hider(self, seconds):
        """This pill leaves on its own after seconds, or sooner if you
        press anything, whichever comes first. The tally is read now, so
        the very keystroke that produced this pill is already behind it
        and cannot dismiss what it just caused."""
        self._hide_at  = time.monotonic() + seconds
        self._input_at = _input_count()
        self._hider.start()

    # ── timers ─────────────────────────────────────────────────────────────────

    def _stop_tick(self):
        try:
            self._tick.stop()
        except Exception:
            pass
        self._rec_t0 = None

    @_guard
    def _on_tick(self, _timer=None):
        if self._rec_t0 is None:
            return
        s = int(time.monotonic() - self._rec_t0)
        self._set_line(f'Recording  {s // 60}:{s % 60:02d}')

    @_guard
    def _on_hider(self, _timer=None):
        if self._hide_at is None:
            self._hider.stop()
            return
        if time.monotonic() >= self._hide_at:
            self._hide_at = None
            self._hider.stop()
            self._fade_out()

    # ── window plumbing ────────────────────────────────────────────────────────

    def _ensure(self):
        if self._panel is not None:
            return
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self._width, _HEIGHT), style,
            NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(_BEHAVIOR)
        view = panel.contentView()
        view.setWantsLayer_(True)
        scale = panel.backingScaleFactor() or 2.0

        pill = CALayer.layer()
        # opaque, not the 0.94 it used to be. Six percent of whatever is
        # behind still reads as ghost text against a ground this dark,
        # and the words need clean black to glow off
        pill.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(
            0.02, 0.02, 0.03, 1.0).CGColor())
        pill.setCornerRadius_(_HEIGHT / 2.0)
        view.layer().addSublayer_(pill)

        mask = CAShapeLayer.layer()
        mask.setFillColor_(None)
        mask.setStrokeColor_(NSColor.whiteColor().CGColor())
        mask.setLineWidth_(_RING)

        ring = CALayer.layer()
        ring.setMask_(mask)

        grad = CAGradientLayer.layer()
        grad.setColors_(_rainbow_cgcolors())
        grad.setStartPoint_((0.0, 0.5))
        grad.setEndPoint_((1.0, 0.5))
        ring.addSublayer_(grad)
        view.layer().addSublayer_(ring)

        label = NSTextField.labelWithString_('')
        label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(
            13.0, 0.23))  # 0.23 = NSFontWeightMedium
        label.setTextColor_(NSColor.colorWithWhite_alpha_(1.0, 0.92))
        label.setAlignment_(NSTextAlignmentCenter)
        view.addSubview_(label)

        caption = CATextLayer.layer()
        caption.setContentsScale_(scale)
        caption.setAlignmentMode_('center')
        caption.setHidden_(True)
        view.layer().addSublayer_(caption)

        # Three layers, one job each. The bloom wrapper is what rises and
        # fades. Inside it the glow casts its halo from the glyph shapes
        # themselves, which is why it is a text layer and not a shadow on
        # the wrapper: a shadow set on a masked layer is thrown by the
        # layer's rectangle, and that hazes the whole pill. Then the
        # words wrapper is masked to the text, so the rainbow sliding
        # inside it travels along letters that hold still.
        bloom = CALayer.layer()
        bloom.setHidden_(True)

        glow = CATextLayer.layer()
        glow.setContentsScale_(scale)
        glow.setAlignmentMode_('center')
        glow.setShadowColor_(NSColor.whiteColor().CGColor())
        glow.setShadowOffset_(CGSizeMake(0.0, 0.0))
        glow.setShadowRadius_(6.0)
        glow.setShadowOpacity_(0.6)
        bloom.addSublayer_(glow)

        words_mask = CATextLayer.layer()
        words_mask.setContentsScale_(scale)
        words_mask.setAlignmentMode_('center')
        words_mask.setForegroundColor_(NSColor.whiteColor().CGColor())

        wrap = CALayer.layer()
        wrap.setMask_(words_mask)

        words = CAGradientLayer.layer()
        words.setColors_(_rainbow_cgcolors())
        words.setStartPoint_((0.0, 0.5))
        words.setEndPoint_((1.0, 0.5))
        wrap.addSublayer_(words)
        bloom.addSublayer_(wrap)
        view.layer().addSublayer_(bloom)

        self._panel, self._label = panel, label
        self._pill, self._ring, self._ring_mask, self._grad = \
            pill, ring, mask, grad
        self._caption, self._bloom, self._glow, self._wrap = \
            caption, bloom, glow, wrap
        self._words_mask, self._words = words_mask, words

    # ── content ────────────────────────────────────────────────────────────────

    @property
    def _words_font(self):
        return NSFont.systemFontOfSize_weight_(15.0, -0.4)  # light

    @property
    def _caption_font(self):
        return NSFont.systemFontOfSize_weight_(10.0, 0.23)  # medium

    def _preview(self, transcript):
        """The opening words, cut to fit the widest pill we allow. Words
        first, then characters, because half a word reads as a glitch
        while a short quotation reads as a quotation."""
        words = ' '.join(transcript.split()).split(' ')
        words = [w for w in words if w]
        if not words:
            return ''
        font  = self._words_font
        avail = _MAX_W - 2 * _PAD
        keep  = words[:_PREVIEW_WORDS]
        while keep:
            text = ' '.join(keep)
            if len(keep) < len(words):
                text += ' …'
            if _attributed(text, font).size().width <= avail:
                return text
            keep = keep[:-1]
        one = words[0]
        while one and _attributed(one + '…', font).size().width > avail:
            one = one[:-1]
        return (one + '…') if one else ''

    def _set_line(self, text):
        """Single line state: the label carries the text, the ring carries
        the colour."""
        self._mode = 'line'
        self._label.setHidden_(False)
        self._caption.setHidden_(True)
        self._bloom.setHidden_(True)
        self._ring.setOpacity_(1.0)

        self._label.setStringValue_(text)
        self._label.sizeToFit()
        want_w = max(_MIN_W, self._label.frame().size.width + 2 * _PAD)
        if (abs(want_w - self._width) > 2 or self._height != _HEIGHT
                or self._grad.animationForKey_('slide') is None):
            self._width  = want_w
            self._height = _HEIGHT
            self._layout()
        self._label.setFrame_(NSMakeRect(
            0, (self._height - self._label.frame().size.height) / 2.0 - 0.5,
            self._width, self._label.frame().size.height))

    def _set_copied(self, preview):
        """Two line state: a quiet caption over the words, which take the
        spectrum off the ring."""
        self._mode = 'copied'
        self._label.setHidden_(True)
        self._caption.setHidden_(False)
        self._bloom.setHidden_(False)
        self._ring.setOpacity_(_RING_QUIET)

        cap_attr = _attributed('copied to clipboard', self._caption_font,
                               NSColor.colorWithWhite_alpha_(1.0, 0.5), 1.4)
        wrd_attr = _attributed(preview, self._words_font,
                               NSColor.whiteColor())
        # the halo is thrown by these glyphs and then hidden underneath
        # the coloured ones, so only its spill is ever seen
        glow_attr = _attributed(preview, self._words_font,
                                NSColor.colorWithWhite_alpha_(1.0, 0.55))
        cap_size, wrd_size = cap_attr.size(), wrd_attr.size()
        self._caption.setString_(cap_attr)
        self._glow.setString_(glow_attr)
        self._words_mask.setString_(wrd_attr)

        content = max(cap_size.width, wrd_size.width)
        self._width  = min(_MAX_W, max(_MIN_W, content + 2 * _PAD))
        self._height = round(cap_size.height + _GAP + wrd_size.height
                             + 2 * _VPAD)
        self._layout()

        w = self._width
        words_y = (self._height - (cap_size.height + _GAP
                                   + wrd_size.height)) / 2.0
        self._caption.setFrame_(CGRectMake(
            0, words_y + wrd_size.height + _GAP, w, cap_size.height))
        self._bloom.setFrame_(CGRectMake(0, words_y, w, wrd_size.height))
        self._glow.setFrame_(CGRectMake(0, 0, w, wrd_size.height))
        self._wrap.setFrame_(CGRectMake(0, 0, w, wrd_size.height))
        self._words_mask.setFrame_(CGRectMake(0, 0, w, wrd_size.height))
        self._words.setFrame_(CGRectMake(0, 0, 2 * w, wrd_size.height))
        self._slide(self._words, w, _SPEED_WORDS)

    def _layout(self):
        """Resize the window around the current content and rebuild the
        layer geometry. Grows around the middle of the spot it already
        occupies, so opening into two lines does not lurch downward."""
        w, h  = self._width, self._height
        frame = self._panel.frame()
        cx = frame.origin.x + frame.size.width / 2.0
        cy = frame.origin.y + frame.size.height / 2.0
        self._panel.setFrame_display_(
            NSMakeRect(cx - w / 2.0, cy - h / 2.0, w, h), True)

        radius = min(w, h) / 2.0
        self._pill.setFrame_(CGRectMake(0, 0, w, h))
        self._pill.setCornerRadius_(radius)
        self._ring.setFrame_(CGRectMake(0, 0, w, h))
        inset = _RING / 2.0
        self._ring_mask.setFrame_(CGRectMake(0, 0, w, h))
        self._ring_mask.setPath_(CGPathCreateWithRoundedRect(
            CGRectMake(inset, inset, w - _RING, h - _RING),
            radius - inset, radius - inset, None))
        self._grad.setFrame_(CGRectMake(0, 0, 2 * w, h))
        self._slide(self._grad, w, self._speed)

    def _set_speed(self, seconds_per_loop):
        if abs(seconds_per_loop - self._speed) > 0.01:
            self._speed = seconds_per_loop
            self._slide(self._grad, self._width, self._speed)

    def _slide(self, layer, distance, seconds):
        """The gradient is two hue cycles wide; sliding it left by one
        pill width lines the pattern back up with itself, so repeating
        that slide forever reads as one continuous drift. Reduce Motion
        gets the same spectrum, held still."""
        layer.removeAnimationForKey_('slide')
        if _reduce_motion():
            return
        anim = CABasicAnimation.animationWithKeyPath_('transform.translation.x')
        anim.setFromValue_(0.0)
        anim.setToValue_(-float(distance))
        anim.setDuration_(seconds)
        anim.setRepeatCount_(1.0e9)
        anim.setTimingFunction_(
            CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionLinear))
        layer.addAnimation_forKey_(anim, 'slide')

    def _bloom_in(self):
        """The words arrive rather than appear: they rise the last few
        points into place while the glow comes up under them. The rise
        lives on the wrapper, so it never fights the spectrum sliding
        inside it."""
        self._bloom.removeAnimationForKey_('bloom')
        self._bloom.removeAnimationForKey_('rise')
        if _reduce_motion():
            return
        ease = CAMediaTimingFunction.functionWithName_(
            kCAMediaTimingFunctionEaseOut)

        fade = CABasicAnimation.animationWithKeyPath_('opacity')
        fade.setFromValue_(0.0)
        fade.setToValue_(1.0)
        fade.setDuration_(_BLOOM)
        fade.setTimingFunction_(ease)
        self._bloom.addAnimation_forKey_(fade, 'bloom')

        rise = CABasicAnimation.animationWithKeyPath_('transform.translation.y')
        rise.setFromValue_(-_RISE)
        rise.setToValue_(0.0)
        rise.setDuration_(_BLOOM)
        rise.setTimingFunction_(ease)
        self._bloom.addAnimation_forKey_(rise, 'rise')

    def _dissolve(self, seconds=_FADE_OUT):
        """Leaving, the words carry on the way they came in and drift up
        out of the pill instead of just switching off with it. A pill
        dismissed by a keypress gets the same drift over its shorter
        fade, so it still leaves rather than blinking out."""
        if self._mode != 'copied' or _reduce_motion():
            return
        drift = CABasicAnimation.animationWithKeyPath_('transform.translation.y')
        drift.setFromValue_(0.0)
        drift.setToValue_(_RISE * 0.7)
        drift.setDuration_(seconds)
        drift.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
            kCAMediaTimingFunctionEaseOut))
        self._bloom.addAnimation_forKey_(drift, 'rise')

    # ── placement and fading ───────────────────────────────────────────────────

    def _screen_under(self, point):
        """The screen the pointer is on. Cursor coordinates and screen
        frames share one global space, so this is a containment test, and
        the main screen is the answer only when the pointer is nowhere,
        which happens while displays are being reconfigured."""
        for s in NSScreen.screens():
            f = s.frame()
            if (f.origin.x <= point.x <= f.origin.x + f.size.width and
                    f.origin.y <= point.y <= f.origin.y + f.size.height):
                return s
        return NSScreen.mainScreen()

    def _position(self):
        """Sit just below the pointer, centered on it, and never off the
        edge of its screen. Clamping is what makes a cursor in a corner
        still readable: the pill slides along the edge rather than
        hanging half off it."""
        mouse  = NSEvent.mouseLocation()
        screen = self._screen_under(mouse)
        if screen is None:
            return
        vf = screen.visibleFrame()
        w, h = self._width, self._height

        x = mouse.x - w / 2.0
        y = mouse.y - _CURSOR - h
        # no room under the pointer, so hang it above instead, where the
        # arrow does not cover the first character
        if y < vf.origin.y + _EDGE_GAP:
            y = mouse.y + _CURSOR

        left   = vf.origin.x + _EDGE_GAP
        right  = vf.origin.x + vf.size.width - w - _EDGE_GAP
        bottom = vf.origin.y + _EDGE_GAP
        top    = vf.origin.y + vf.size.height - h - _EDGE_GAP
        # a screen narrower than the pill would invert the range, so take
        # the left and bottom edges as the floor in that case
        x = max(left, min(x, right)) if right > left else left
        y = max(bottom, min(y, top)) if top > bottom else bottom
        self._panel.setFrameOrigin_((x, y))

    @_guard
    def _on_follow(self, _timer=None):
        """The fast timer, doing three things while the pill is up:
        keeping it under the pointer, putting it back when the window
        server has taken it away, and watching for the keystroke or click
        that sends it away. All three belong on the same short interval,
        since a dismissal any slower than this reads as lag rather than
        as the pill getting out of the way."""
        if self._panel is None:
            return
        if self._up:
            self._reclaim()
        elif not self._panel.isVisible():
            return
        self._position()
        if self._input_at is None:
            return
        now = _input_count()
        if now is not None and now != self._input_at:
            # you have started doing something else, so the reading is
            # over whether or not its few seconds are up
            self._hide_at = None
            self._fade_out(_FADE_QUIT)

    def _reclaim(self):
        """Put the pill back when it is meant to be up and is not. macOS
        orders a borderless panel out on its own across a display sleep,
        a display reconfiguration, or a Space change, and it does it to
        the window rather than telling the app, so nothing in AppKit
        fires and the pill is simply gone for the rest of the process's
        life. That is the whole bug: an app running for a day has slept
        and changed Spaces many times, while a freshly launched one has
        done neither, which is why it looks like the code works until it
        has been up a while.

        So this asks the window itself, on the same tick that already
        follows the pointer. It reasserts the level and the collection
        behavior too, since those are what carry the panel across a Space
        and are the properties a reconfiguration resets."""
        panel = self._panel
        if panel.isVisible() and panel.isOnActiveSpace():
            return
        now = time.monotonic()
        if now < self._reclaim_at:
            return
        # a panel that stays lost would otherwise reorder itself every
        # 40ms, so knock once every half second and let it settle
        self._reclaim_at = now + _RECLAIM
        if not self._said:
            # once per pill, not once per knock, so a panel that has to be
            # chased hard leaves one readable line instead of a wall
            self._said = True
            _log(f'reclaiming: visible={panel.isVisible()} '
                 f'onActiveSpace={panel.isOnActiveSpace()} '
                 f'level={panel.level()} behavior={panel.collectionBehavior()}')
        panel.setLevel_(NSStatusWindowLevel)
        panel.setCollectionBehavior_(_BEHAVIOR)
        panel.orderFrontRegardless()
        panel.setAlphaValue_(1.0)

    def _stop_follow(self):
        try:
            self._follow.stop()
        except Exception:
            pass

    def _fade_in(self):
        """Always order the panel front, and always reassert the level and
        the collection behavior with it. The window server can take a
        borderless panel off screen on its own, across a display sleep or
        a Space change, and the old code skipped this whole method
        whenever a Python flag said the pill was already up, so one such
        event hid it for the rest of the app's life. From here on the
        follow timer keeps checking, so a Space change while the pill is
        already up brings it along too."""
        self._gen += 1
        self._up   = True
        self._reclaim_at = 0.0
        self._said = False
        self._position()
        if not self._panel.isVisible():
            self._panel.setAlphaValue_(0.0)
        self._panel.setLevel_(NSStatusWindowLevel)
        self._panel.setCollectionBehavior_(_BEHAVIOR)
        self._panel.orderFrontRegardless()
        try:
            self._follow.start()
        except Exception:
            pass
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE_IN)
        self._panel.animator().setAlphaValue_(1.0)
        NSAnimationContext.endGrouping()

    @_guard
    def _fade_out(self, seconds=_FADE_OUT):
        if self._panel is None:
            return
        # the pill stops belonging on screen the moment it is asked to
        # leave, before the fade, so nothing reclaims it out from under
        # its own fade out. Every timer stops here too, including on the
        # path where the window server had already taken the panel away:
        # that early return used to leave the follow timer running at
        # 40ms for the rest of the app's life
        self._up       = False
        self._input_at = None
        self._stop_tick()
        self._stop_follow()
        if not self._panel.isVisible():
            return
        self._gen += 1
        gen = self._gen
        self._dissolve(seconds)
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(seconds)
        self._panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

        def _finish():
            self._finisher.stop()
            # a new state may have shown the pill again while this fade
            # was running, and that one owns the panel now
            if self._gen == gen and self._panel is not None:
                self._panel.orderOut_(None)
        # held on the instance, so nothing can collect the timer before
        # it fires and leave the panel stranded at zero alpha
        self._finisher = _Ticker(_finish, seconds + 0.05)
        self._finisher.start()
