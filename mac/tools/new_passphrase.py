#!/usr/bin/env python3
"""Make the ClipBridge passphrase and keep it in the login Keychain.

Five words drawn with `secrets` from the EFF long word list (the four
hyphenated entries removed, 7772 left), about 64 bits. That is the whole
strength of the cb1 encryption: anyone can download the ciphertext and
guess offline, so this must stay random words, never something chosen.

    python3 tools/new_passphrase.py          # make one, store it, show it once
    python3 tools/new_passphrase.py --show   # show the stored one

The Mac app reads it at launch from service ClipBridge, account passphrase.
Type the same words into the Windows tray and into the QR Bridge page.
Making a new one locks out every device still holding the old one.
"""

import secrets
import subprocess
import sys
from pathlib import Path

SERVICE, ACCOUNT = 'ClipBridge', 'passphrase'


def stored():
    out = subprocess.run(['security', 'find-generic-password', '-s', SERVICE,
                          '-a', ACCOUNT, '-w'], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def main():
    if '--show' in sys.argv:
        phrase = stored()
        print(phrase or 'No ClipBridge passphrase in the Keychain.')
        return
    if stored() and input('A passphrase is already stored. Replace it? [y/N] ').lower() != 'y':
        return
    words = (Path(__file__).with_name('eff_large_wordlist.txt')
             .read_text().split())
    phrase = ' '.join(secrets.choice(words) for _ in range(5))
    subprocess.run(['security', 'add-generic-password', '-U', '-s', SERVICE,
                    '-a', ACCOUNT, '-l', 'ClipBridge passphrase', '-w', phrase],
                   check=True)
    print(f'\n    {phrase}\n')
    print('Stored in the login Keychain. Quit and reopen ClipBridge to use it.')


if __name__ == '__main__':
    main()
