"""
Whether ClipBridge starts itself when the Mac logs in.

The registration is macOS's, through SMAppService, so the login item shows up
in System Settings > General > Login Items under the app's own name and can be
switched off there like any other. What this module keeps on disk is only the
intent, in ~/.clipbridge/prefs.json, and the two are different facts.

They have to be, because build.sh installs by deleting /Applications/
ClipBridge.app and copying a new one over it. The registration is bound to that
bundle and its signature, so every install drops it. The app restates the
intent at launch, which is what puts it back; without the stored intent a
rebuild would silently stop starting at login and nothing would say so.

Three things here are less obvious than they look:

**The bundle identity is checked before anything is registered.** SMAppService's
mainAppService registers whatever bundle the process belongs to, and for a
plain `python3 clipbridge.py` that is the Python framework's own Python.app. It
does not refuse: it returns success and leaves a login item called "Python"
behind, pointing at Homebrew. So the guard is an exact match on ClipBridge's
bundle identifier, and everything else reports UNAVAILABLE and does nothing.

**ServiceManagement is loaded by hand rather than imported.** There is a PyObjC
binding for it, but it is one more wheel in requirements.txt, one more package
in py2app's list, and one more thing to be missing in a bundle that is
otherwise built. The framework is on every Mac, objc.loadBundle finds it, and
the two methods needed here come through with their NSError out parameters
handled.

**BLOCKED is not OFF.** Switching the item off in System Settings leaves the
registration in place and reports requiresApproval, and registering again does
not override it. Reading that as "not registered" would mean the app quietly
turning itself back on at every launch, which is the one behaviour a login item
switch must never have.
"""

import json
from pathlib import Path

BUNDLE_ID = 'com.mustafaalbaree.clipbridge'
PREFS = Path.home() / '.clipbridge' / 'prefs.json'

# macOS will start it at login.
ON = 'on'
# Registered, and switched off in System Settings > General > Login Items.
BLOCKED = 'blocked'
# No registration.
OFF = 'off'
# Not running as ClipBridge.app, so there is nothing to register.
UNAVAILABLE = 'unavailable'

# SMAppServiceStatus, which is a plain NSInteger over the wire.
_ENABLED = 1
_REQUIRES_APPROVAL = 2

_service = None
_looked_up = False


def _bundled():
    """Whether this process really is ClipBridge.app. See the module note."""
    try:
        from Foundation import NSBundle
        return NSBundle.mainBundle().bundleIdentifier() == BUNDLE_ID
    except Exception:
        return False


def _svc():
    global _service, _looked_up
    if _looked_up:
        return _service
    _looked_up = True
    if not _bundled():
        return None
    try:
        import objc
        objc.loadBundle(
            'ServiceManagement', globals(),
            bundle_path='/System/Library/Frameworks/ServiceManagement.framework')
        _service = objc.lookUpClass('SMAppService').mainAppService()
    except Exception:
        _service = None
    return _service


def state():
    """What macOS is actually doing, not what was asked for."""
    svc = _svc()
    if svc is None:
        return UNAVAILABLE
    try:
        status = svc.status()
    except Exception:
        return UNAVAILABLE
    if status == _ENABLED:
        return ON
    if status == _REQUIRES_APPROVAL:
        return BLOCKED
    return OFF


def intent():
    """What was last chosen. On unless it was turned off, because a clipboard
    bridge that is only running when it was launched by hand is not a bridge."""
    try:
        return bool(json.loads(PREFS.read_text(encoding='utf-8'))['open_at_login'])
    except Exception:
        return True


def _remember(on):
    try:
        prefs = json.loads(PREFS.read_text(encoding='utf-8')) if PREFS.is_file() else {}
    except Exception:
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    prefs['open_at_login'] = bool(on)
    try:
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(prefs, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass


def set_enabled(on):
    """Ask macOS for the given state, and remember it was asked for. Returns
    what the state is afterwards, which is not always what was requested."""
    _remember(on)
    svc = _svc()
    if svc is None:
        return UNAVAILABLE
    try:
        if on:
            svc.registerAndReturnError_(None)
        else:
            svc.unregisterAndReturnError_(None)
    except Exception:
        pass
    return state()


def reconcile():
    """Called once at launch. Restates the intent when the registration has
    gone, which is what an install does to it, and leaves BLOCKED alone."""
    wanted = intent()
    now = state()
    if (now == OFF and wanted) or (now == ON and not wanted):
        return set_enabled(wanted)
    return now
