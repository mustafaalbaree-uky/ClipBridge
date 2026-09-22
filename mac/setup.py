import sys
from pathlib import Path
from setuptools import setup

# so py2app's dependency walker finds the shared modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

APP = ['clipbridge.py']
OPTIONS = {
    'argv_emulation': False,
    'packages': ['rumps', 'requests', 'certifi', 'urllib3', 'idna',
                 'charset_normalizer', 'numpy', 'soundfile',
                 '_soundfile_data', 'quickmachotkey', 'AVFoundation',
                 'ApplicationServices', 'Quartz'],
    'includes': ['noteproc', 'localasr', 'hud', 'loginitem', 'pin'],
    'plist': {
        'LSUIElement': True,
        'CFBundleName': 'ClipBridge',
        'CFBundleDisplayName': 'ClipBridge',
        'CFBundleIdentifier': 'com.mustafaalbaree.clipbridge',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1',
        'NSUserNotificationAlertStyle': 'alert',
        'NSMicrophoneUsageDescription':
            'ClipBridge records voice notes so it can transcribe them '
            'onto your clipboard.',
        'NSAppleEventsUsageDescription':
            'ClipBridge types a pinned voice note into the Terminal tab it '
            'was pinned in and presses Return.',
    },
}
if Path('ClipBridge.icns').exists():
    OPTIONS['iconfile'] = 'ClipBridge.icns'

setup(
    name='ClipBridge',
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
