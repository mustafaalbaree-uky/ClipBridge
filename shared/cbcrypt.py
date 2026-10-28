"""
Clip encryption, the cb1 format. Every ClipBridge client speaks it: this
module for the Mac and Windows apps, and the matching block in QR-Bridge's
index.html for the browser. Change one and you must change the other.

The Supabase table is world readable and world writable through the anon
key, which is published. So the table is treated as hostile: clips are
encrypted before they leave a device, and a row that does not decrypt with
our key is dropped without a word, which is what stops anyone pushing text
onto the clipboard.

    content = "cb1." + b64url(iv) + "." + b64url(ciphertext + tag)

  * Key: PBKDF2-HMAC-SHA256 over the normalized passphrase, fixed salt,
    600,000 rounds, 32 bytes. The salt is fixed so a device derives the key
    once instead of once per clip; the passphrase is five random words,
    which is what carries the strength.
  * Cipher: AES-256-GCM, 12 byte random IV.
  * Associated data: the row's `source`, so a captured clip cannot be
    relabelled to reach a different device.
  * Plaintext: JSON {"v": 1, "t": text, "ts": unix ms}. A clip whose ts is
    more than 30 minutes old or 10 minutes ahead is refused, so a captured
    row cannot be inserted again later and replayed. The window is wide on
    purpose: clients on badly set clocks broke a tighter expiry before.
"""

import base64
import hashlib
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = 'cb1.'
SALT = b'clipbridge/v1'
ROUNDS = 600_000
MAX_AGE_MS = 30 * 60 * 1000
MAX_AHEAD_MS = 10 * 60 * 1000


def normalize(passphrase):
    """Case and spacing do not matter, so a phone's autocapitalize or a
    double space cannot lock anyone out."""
    return ' '.join(passphrase.lower().split())


def derive_key(passphrase):
    return hashlib.pbkdf2_hmac('sha256', normalize(passphrase).encode('utf-8'),
                               SALT, ROUNDS, 32)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def is_encrypted(content):
    return isinstance(content, str) and content.startswith(PREFIX)


def encrypt(key, text, source):
    iv = os.urandom(12)
    body = json.dumps({'v': 1, 't': text, 'ts': int(time.time() * 1000)},
                      ensure_ascii=False).encode('utf-8')
    sealed = AESGCM(key).encrypt(iv, body, (source or '').encode('utf-8'))
    return PREFIX + _b64(iv) + '.' + _b64(sealed)


def decrypt(key, content, source):
    """The clip's text, or None for anything that is not a fresh cb1 clip
    sealed with this key for this source."""
    if not is_encrypted(content):
        return None
    try:
        iv, sealed = content[len(PREFIX):].split('.')
        body = AESGCM(key).decrypt(_unb64(iv), _unb64(sealed),
                                   (source or '').encode('utf-8'))
        payload = json.loads(body)
        age = time.time() * 1000 - payload['ts']
        if payload.get('v') != 1 or age > MAX_AGE_MS or age < -MAX_AHEAD_MS:
            return None
        return payload['t']
    except (ValueError, KeyError, TypeError, InvalidTag):
        return None
