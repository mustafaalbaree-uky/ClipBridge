#!/usr/bin/env python3
"""mixednote: transcribe a ClipBridge note that is in Arabic, or mixes Arabic
and English, entirely on this Mac.

ClipBridge itself always asks Whisper for English, which is right for daily
notes and turns an Arabic conversation into invented English. This is the
separate path for the rare Arabic one. Daily ClipBridge is untouched.

    mixednote                 newest note in ~/.clipbridge/notes
    mixednote path/to.wav     any recording ffmpeg can read

Writes <recording>.mixed.txt next to the recording and prints its path.

How it works, and why:
- whisper.cpp with full large-v3. turbo is much weaker on Arabic.
- The recording is cut inside pauses into pieces of about 15 s, and each
  piece is transcribed on its own with no carried context. A long run lets
  one repeated sentence feed itself and swallow minutes of audio; a short
  independent piece cannot.
- The language is detected per piece, so English stretches stay English.
- Pieces that look wrong (repeats, too few words for their length, known
  Whisper inventions such as subtitle credits) are redone forced to Arabic
  and forced to English, and both versions are written under the first one
  marked "alt", for whoever cleans the text up to choose.
Nothing is uploaded.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NOTES = Path.home() / '.clipbridge' / 'notes'
MODEL = Path.home() / '.cache' / 'lecpipe' / 'models' / 'ggml-large-v3.bin'
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin'
TARGET, MAXLEN = 15.0, 25.0

# Text Whisper produces from silence or noise: subtitle credits and video
# intros it learned from training data. Never something that was said.
INVENTED = [r'ترجمة', r'نانسي قنقر', r'مرحبا بكم', r'مرحباً بكم', r'اشتركوا',
            r'شكرا للمشاهدة', r'Thanks for watching', r'Subtitles by']


def die(msg):
    print(f'mixednote: {msg}', file=sys.stderr)
    sys.exit(1)


def silences(src):
    log = subprocess.run(['ffmpeg', '-hide_banner', '-i', src, '-af',
                          'silencedetect=noise=-35dB:d=0.35', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', log)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', log)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def cut_points(src):
    dur = float(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', src]).strip())
    cuts, last = [0.0], 0.0
    for m in silences(src):
        if m - last >= TARGET:
            cuts.append(m)
            last = m
        while m - last > MAXLEN:      # no pause for too long: cut on the grid
            last += TARGET
            cuts.append(last)
    if len(cuts) > 1 and dur - cuts[-1] < 3:
        cuts.pop()
    return cuts + [dur]


def whisper(wav, lang):
    r = subprocess.run(['whisper-cli', '-m', str(MODEL), '-f', str(wav), '-l', lang,
                        '-mc', '0', '-nt', '-bs', '5'], capture_output=True, text=True)
    text = ' '.join(x.strip() for x in r.stdout.splitlines() if x.strip())
    m = re.search(r'auto-detected language: (\w+)', r.stderr)
    return text, (m.group(1) if m else lang)


def looks_wrong(text, seconds):
    words = text.split()
    if seconds > 6 and len(words) < seconds * 0.6:
        return True
    if any(re.search(p, text) for p in INVENTED):
        return True
    for n in (1, 2, 3):                    # the same word or phrase 3+ times running
        grams = [' '.join(words[i:i + n]) for i in range(0, len(words) - n + 1)]
        for i in range(len(grams) - 2 * n):
            if grams[i] == grams[i + n] == grams[i + 2 * n]:
                return True
    return False


def stamp(t):
    return f'{int(t // 60):02d}:{int(t % 60):02d}'


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        return
    if len(sys.argv) > 1:
        src = Path(sys.argv[1]).expanduser().resolve()
    else:
        wavs = sorted(NOTES.glob('*.wav'), key=lambda p: p.stat().st_mtime)
        if not wavs:
            die(f'no notes in {NOTES}')
        src = wavs[-1]
    if not src.exists():
        die(f'{src} not found')
    if not MODEL.exists():
        die(f'model missing: {MODEL}\n'
            f'  It is 3.1 GB. Run netcheck --fits 3.1, then:\n'
            f'  curl -L -o {MODEL} {MODEL_URL}')

    cuts = cut_points(str(src))
    pieces = list(zip(cuts, cuts[1:]))
    out_lines, redone = [], 0
    print(f'mixednote: {src.name}, {stamp(cuts[-1])} long, {len(pieces)} pieces',
          file=sys.stderr)
    with tempfile.TemporaryDirectory() as td:
        for i, (a, b) in enumerate(pieces):
            wav = Path(td) / 'piece.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(a), '-to', str(b),
                            '-i', str(src), '-ar', '16000', '-ac', '1', str(wav)],
                           check=True)
            text, lang = whisper(wav, 'auto')
            out_lines.append(f'[{stamp(a)}] ({lang}) {text}')
            if looks_wrong(text, b - a):
                redone += 1
                for alt in ('ar', 'en'):
                    alt_text, _ = whisper(wav, alt)
                    out_lines.append(f'    alt {alt}: {alt_text}')
            print(f'  {i + 1}/{len(pieces)}', end='\r', file=sys.stderr, flush=True)

    out = src.with_suffix('.mixed.txt')
    out.write_text('\n'.join(out_lines) + '\n')
    print(f'\nmixednote: {redone} of {len(pieces)} pieces looked wrong and have '
          f'alt lines', file=sys.stderr)
    print(out)


if __name__ == '__main__':
    main()
