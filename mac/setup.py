import sys
from pathlib import Path
from setuptools import setup

# so py2app's dependency walker finds the shared noteproc module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'shared'))

APP = ['clipbridge.py']
OPTIONS = {
    'argv_emulation': False,
    'packages': ['rumps', 'requests', 'certifi', 'urllib3', 'idna',
                 'charset_normalizer', 'numpy', 'sounddevice', 'soundfile',
                 '_sounddevice_data', '_soundfile_data', 'quickmachotkey'],
    'includes': ['noteproc'],
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
