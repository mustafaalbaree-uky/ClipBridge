"""
On screen state pill for ClipBridge voice notes.

A small black pill wrapped in a shifting rainbow ring. It drifts along
slowly while recording (with a live elapsed counter), speeds up while
transcribing, and flashes short confirmations like "Copied to clipboard".
It replaces the notification banners for the whole voice note flow.

The panel never takes focus and ignores every click, so it can sit over
anything without getting in the way. It picks its spot when it appears:
top center of whichever screen the pointer is on, just under the menu
bar, unless the pointer is already up there, in which case it drops to
the bottom center instead (where dictation style overlays usually live).

Every public method is safe to call from any thread; work is bounced to
the main thread internally.
"""
import time

import rumps

from AppKit import (NSColor, NSEvent, NSFont, NSPanel, NSScreen,
                    NSTextField, NSTextAlignmentCenter,
                    NSBackingStoreBuffered, NSStatusWindowLevel,
                    NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary,
                    NSAnimationContext)
from Foundation import NSMakeRect
from Quartz import (CALayer, CAGradientLayer, CAShapeLayer, CABasicAnimation,
                    CAMediaTimingFunction, kCAMediaTimingFunctionLinear,
                    CGPathCreateWithRoundedRect, CGRectMake)
from PyObjCTools import AppHelper

_HEIGHT   = 34          # pill height in points
_PAD      = 20          # horizontal text padding inside the pill
_MIN_W    = 132
_RING     = 2.5         # rainbow ring thickness
_EDGE_GAP = 14          # distance from the screen edge
_FADE_IN  = 0.18
_FADE_OUT = 0.30

# one full trip of the rainbow around the pill, seconds per loop
_SPEED_REC   = 4.0
_SPEED_BUSY  = 1.3
_SPEED_FLASH = 2.4


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


class RecordingHUD:
    def __init__(self):
        self._panel      = None
        self._label      = None
        self._pill       = None      # black rounded backdrop layer
        self._ring       = None      # layer holding the gradient, masked
        self._ring_mask  = None      # stroke shaped mask for the ring
        self._grad       = None      # the moving rainbow itself
        self._width      = _MIN_W
        self._speed      = _SPEED_REC
        self._visible    = False
        self._rec_t0     = None
        self._tick       = rumps.Timer(self._on_tick, 0.5)
        self._hide_at    = None
        self._hider      = rumps.Timer(self._on_hider, 0.2)

    # ── public, callable from any thread ───────────────────────────────────────

    def recording(self):
        AppHelper.callAfter(self._enter_recording)

    def processing(self):
        AppHelper.callAfter(self._enter_processing)

    def flash(self, text, seconds=1.8):
        AppHelper.callAfter(self._enter_flash, text, seconds)

    def hide(self):
        AppHelper.callAfter(self._fade_out)

    # ── states (main thread) ───────────────────────────────────────────────────

    def _enter_recording(self):
        self._ensure()
        self._hide_at = None
        self._rec_t0  = time.monotonic()
        self._set_text('Recording  0:00')
        self._set_speed(_SPEED_REC)
        self._tick.start()
        self._fade_in()

    def _enter_processing(self):
        self._ensure()
        self._hide_at = None
        self._stop_tick()
        self._set_text('Transcribing…')
        self._set_speed(_SPEED_BUSY)
        self._fade_in()

    def _enter_flash(self, text, seconds):
        self._ensure()
        self._stop_tick()
        self._set_text(text)
        self._set_speed(_SPEED_FLASH)
        self._fade_in()
        self._hide_at = time.monotonic() + seconds
        self._hider.start()

    # ── timers ─────────────────────────────────────────────────────────────────

    def _stop_tick(self):
        try:
            self._tick.stop()
        except Exception:
            pass
        self._rec_t0 = None

    def _on_tick(self, _timer=None):
        if self._rec_t0 is None:
            return
        s = int(time.monotonic() - self._rec_t0)
        self._set_text(f'Recording  {s // 60}:{s % 60:02d}')

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
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary)
        view = panel.contentView()
        view.setWantsLayer_(True)

        pill = CALayer.layer()
        pill.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(
            0.02, 0.02, 0.03, 0.94).CGColor())
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

        self._panel, self._label = panel, label
        self._pill, self._ring, self._ring_mask, self._grad = \
            pill, ring, mask, grad

    def _set_text(self, text):
        self._label.setStringValue_(text)
        self._label.sizeToFit()
        want = max(_MIN_W, self._label.frame().size.width + 2 * _PAD)
        if abs(want - self._width) > 2 or self._grad.animationForKey_('slide') is None:
            self._width = want
            self._layout()
        self._label.setFrame_(NSMakeRect(
            0, (_HEIGHT - self._label.frame().size.height) / 2.0 - 0.5,
            self._width, self._label.frame().size.height))

    def _layout(self):
        """Resize the window around the current text and rebuild the layer
        geometry. Keeps the pill centered on the spot it already occupies
        when only the width changes."""
        w = self._width
        frame = self._panel.frame()
        cx = frame.origin.x + frame.size.width / 2.0
        self._panel.setFrame_display_(
            NSMakeRect(cx - w / 2.0, frame.origin.y, w, _HEIGHT), True)
        self._pill.setFrame_(CGRectMake(0, 0, w, _HEIGHT))
        self._ring.setFrame_(CGRectMake(0, 0, w, _HEIGHT))
        inset = _RING / 2.0
        self._ring_mask.setFrame_(CGRectMake(0, 0, w, _HEIGHT))
        self._ring_mask.setPath_(CGPathCreateWithRoundedRect(
            CGRectMake(inset, inset, w - _RING, _HEIGHT - _RING),
            _HEIGHT / 2.0 - inset, _HEIGHT / 2.0 - inset, None))
        self._grad.setFrame_(CGRectMake(0, 0, 2 * w, _HEIGHT))
        self._restart_slide()

    def _set_speed(self, seconds_per_loop):
        if abs(seconds_per_loop - self._speed) > 0.01:
            self._speed = seconds_per_loop
            self._restart_slide()

    def _restart_slide(self):
        """The gradient is two hue cycles wide; sliding it left by one
        pill width lines the pattern back up with itself, so repeating
        that slide forever reads as one continuous drift."""
        self._grad.removeAnimationForKey_('slide')
        anim = CABasicAnimation.animationWithKeyPath_('transform.translation.x')
        anim.setFromValue_(0.0)
        anim.setToValue_(-float(self._width))
        anim.setDuration_(self._speed)
        anim.setRepeatCount_(1.0e9)
        anim.setTimingFunction_(
            CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionLinear))
        self._grad.addAnimation_forKey_(anim, 'slide')

    # ── placement and fading ───────────────────────────────────────────────────

    def _position(self):
        mouse  = NSEvent.mouseLocation()
        screen = None
        for s in NSScreen.screens():
            f = s.frame()
            if (f.origin.x <= mouse.x <= f.origin.x + f.size.width and
                    f.origin.y <= mouse.y <= f.origin.y + f.size.height):
                screen = s
                break
        if screen is None:
            screen = NSScreen.mainScreen()
        if screen is None:
            return
        vf = screen.visibleFrame()
        w  = self._width
        x  = vf.origin.x + (vf.size.width - w) / 2.0
        y  = vf.origin.y + vf.size.height - _HEIGHT - _EDGE_GAP
        # if the pointer is already parked where the pill wants to be,
        # drop to the bottom center instead of appearing under it
        near = 70
        if (x - near <= mouse.x <= x + w + near and mouse.y >= y - near):
            y = vf.origin.y + _EDGE_GAP + 46
        self._panel.setFrameOrigin_((x, y))

    def _fade_in(self):
        if self._visible:
            return
        self._visible = True
        self._position()
        self._panel.setAlphaValue_(0.0)
        self._panel.orderFrontRegardless()
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE_IN)
        self._panel.animator().setAlphaValue_(1.0)
        NSAnimationContext.endGrouping()

    def _fade_out(self):
        if not self._visible or self._panel is None:
            return
        self._visible = False
        self._stop_tick()
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE_OUT)
        self._panel.animator().setAlphaValue_(0.0)
        NSAnimationContext.endGrouping()

        def _finish(t):
            t.stop()
            if not self._visible and self._panel is not None:
                self._panel.orderOut_(None)
        rumps.Timer(_finish, _FADE_OUT + 0.05).start()
