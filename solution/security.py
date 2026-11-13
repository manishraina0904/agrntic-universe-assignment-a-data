"""
security.py — PII encryption, HMAC phone fingerprinting, phone masking.

Uses AES-GCM-256 from the 'cryptography' library.
  - Each encrypt call generates a fresh 12-byte nonce.
  - The stored value is base64(nonce || ciphertext || tag).
  - Decryption is authenticated: tampering is detected.

HMAC-SHA256 produces a deterministic fingerprint of the canonical phone
(+91XXXXXXXXXX) so we can match events to leads without decrypting anything.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from solution.config import get_encryption_key, get_hmac_key


# ── Encryption ─────────────────────────────────────────────────────────────

def encrypt(plaintext: str) -> str:
    """
    Encrypt a string with AES-GCM-256.
    Returns a base64 string: nonce(12) + ciphertext + tag(16).
    """
    key = get_encryption_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    # ciphertext already includes the 16-byte authentication tag
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(token: str) -> str:
    """
    Decrypt a value produced by encrypt().
    Raises cryptography.exceptions.InvalidTag if the ciphertext was tampered with.
    """
    key = get_encryption_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(token)
    nonce = raw[:12]
    ciphertext = raw[12:]
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None).decode("utf-8")


# ── HMAC phone fingerprint ──────────────────────────────────────────────────

def hmac_phone(canonical_phone: str) -> str:
    """
    Return a hex HMAC-SHA256 of the canonical phone (+91XXXXXXXXXX).
    Deterministic: same phone always produces the same fingerprint.
    Used to match events to leads without decrypting the stored phone.
    """
    key = get_hmac_key()
    return hmac.new(key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


# ── Phone masking for reports ───────────────────────────────────────────────

def mask_phone(canonical_phone: str) -> str:
    """
    Return +91******XXXX where XXXX is the last 4 digits.
    canonical_phone must be in +91XXXXXXXXXX form (13 chars).

    Example: +919876543210 → +91******3210
    """
    # +91 followed by 10 digits — take last 4
    digits = canonical_phone[3:]   # strip "+91"
    return "+91******" + digits[-4:]

