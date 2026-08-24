"""Regenerate assets/icon.png, the ClipBridge app icon.

Same glyph the Windows tray draws at runtime, rendered at 512px for use
as the repo icon and the Mac bundle icon.

Run: python3 make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BLUE  = '#0a84ff'
WHITE = '#ffffff'


def draw(size=512):
    s = size / 256
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8 * s, 8 * s, size - 8 * s, size - 8 * s],
                        radius=60 * s, fill=BLUE)
    d.rounded_rectangle([96 * s, 36 * s, 160 * s, 92 * s],
                        radius=18 * s, fill=WHITE)
    d.rounded_rectangle([60 * s, 64 * s, 196 * s, 220 * s],
                        radius=20 * s, fill=WHITE)
    for y in (108, 142, 176):
        d.rounded_rectangle([84 * s, y * s, 172 * s, (y + 12) * s],
                            radius=6 * s, fill=BLUE)
    return img


if __name__ == '__main__':
    out = Path(__file__).parent / 'icon.png'
    draw().save(out)
    print(f'wrote {out}')
