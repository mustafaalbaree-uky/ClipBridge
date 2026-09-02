#!/bin/bash
# Build ClipBridge.app and install it to /Applications.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet rumps requests py2app
fi

# Bundle icon from the shared asset, if iconutil is available
if [ -f ../assets/icon.png ] && command -v iconutil >/dev/null; then
    rm -rf ClipBridge.iconset
    mkdir ClipBridge.iconset
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz ../assets/icon.png \
            --out "ClipBridge.iconset/icon_${sz}x${sz}.png" >/dev/null
    done
    iconutil -c icns ClipBridge.iconset -o ClipBridge.icns
    rm -rf ClipBridge.iconset
fi

rm -rf build dist
.venv/bin/python3 setup.py py2app

echo
echo "Built dist/ClipBridge.app"

if [ "${1:-}" = "--install" ]; then
    osascript -e 'quit app "ClipBridge"' 2>/dev/null || true
    sleep 1
    # a polite quit can be ignored; make sure the old instance is really
    # gone, or open -a will reactivate it instead of launching the new one
    if pgrep -f "ClipBridge.app" >/dev/null; then
        pkill -f "ClipBridge.app" || true
        sleep 1
    fi
    rm -rf "/Applications/ClipBridge.app"
    cp -R "dist/ClipBridge.app" /Applications/
    # by full path, never by name. LaunchServices has both this build
    # directory and /Applications registered under the same name, and it
    # picked dist last time, so a stale dist could be launched while the
    # installed copy sat there unused and the two looked identical
    open "/Applications/ClipBridge.app"
    echo "Installed to /Applications and launched."
fi
