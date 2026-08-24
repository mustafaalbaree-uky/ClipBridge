from pathlib import Path
from setuptools import setup

APP = ['clipbridge.py']
OPTIONS = {
    'argv_emulation': False,
    'packages': ['rumps', 'requests', 'certifi', 'urllib3', 'idna',
                 'charset_normalizer'],
    'plist': {
        'LSUIElement': True,
        'CFBundleName': 'ClipBridge',
        'CFBundleDisplayName': 'ClipBridge',
        'CFBundleIdentifier': 'com.mustafaalbaree.clipbridge',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSUserNotificationAlertStyle': 'alert',
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
