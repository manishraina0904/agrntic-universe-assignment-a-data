"""
test_security.py — Tests for AES-GCM encryption, HMAC phone fingerprinting,
and phone masking.
"""
from __future__ import annotations

import pytest

from solution.security import decrypt, encrypt, hmac_phone, mask_phone


class TestEncryption:
    """AES-GCM encrypt/decrypt round-trip tests."""

    def test_round_trip_phone(self):
        plaintext = "+919876543210"
        token = encrypt(plaintext)
        assert token != plaintext  # ciphertext ≠ plaintext
        assert decrypt(token) == plaintext

    def test_round_trip_name(self):
        plaintext = "Aarav Sharma"
        assert decrypt(encrypt(plaintext)) == plaintext

    def test_different_ciphertexts_for_same_plaintext(self):
        """Each encrypt call uses a fresh nonce → different ciphertext."""
        pt = "+919876543210"
        assert encrypt(pt) != encrypt(pt)

    def test_no_plaintext_in_token(self):
        """The raw phone must not appear in the base64 token."""
        phone = "+919876543210"
        token = encrypt(phone)
        assert phone not in token
        assert "9876543210" not in token


class TestHmac:
    """HMAC-SHA256 phone fingerprinting."""

    def test_deterministic(self):
        """Same phone → same HMAC."""
        h1 = hmac_phone("+919876543210")
        h2 = hmac_phone("+919876543210")
        assert h1 == h2

    def test_different_phones_different_hmac(self):
        h1 = hmac_phone("+919876543210")
        h2 = hmac_phone("+917777777777")
        assert h1 != h2

    def test_no_plaintext_in_hmac(self):
        h = hmac_phone("+919876543210")
        assert "9876543210" not in h
        assert "+91" not in h

    def test_hmac_is_hex(self):
        h = hmac_phone("+919876543210")
        int(h, 16)  # will raise if not hex


class TestMasking:
    """Phone masking in reports."""

    def test_mask_format(self):
        assert mask_phone("+919876543210") == "+91******3210"

    def test_mask_different_last_4(self):
        assert mask_phone("+916666111122") == "+91******1122"

    def test_mask_starts_with_prefix(self):
        result = mask_phone("+917777654321")
        assert result.startswith("+91******")

    def test_mask_has_last_4_digits(self):
        result = mask_phone("+916543219876")
        assert result.endswith("9876")

