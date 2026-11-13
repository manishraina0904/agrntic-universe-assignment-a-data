"""
config.py — environment and path configuration.

Reads ENCRYPTION_KEY from the environment (or .env file).
Provides the database path and schema directory.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

# Try to load .env if python-dotenv is available; silently skip if not.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────
# The repo root is the parent of the solution/ package.
REPO_ROOT = Path(__file__).parent.parent
SCHEMA_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DB_PATH = Path(os.getenv("DB_PATH", str(REPO_ROOT / "ledger.db")))

# ── Encryption key ─────────────────────────────────────────────────────────
def get_encryption_key() -> bytes:
    """
    Return the 32-byte AES key decoded from the ENCRYPTION_KEY env var.
    The env var must hold a base64-encoded 32-byte value (44 chars with padding).
    Raises EnvironmentError if the variable is missing or the key is the wrong size.
    """
    raw = os.getenv("ENCRYPTION_KEY")
    if not raw:
        raise EnvironmentError(
            "ENCRYPTION_KEY environment variable is not set. "
            "Copy .env.example to .env and set a real key."
        )
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise EnvironmentError(
            f"ENCRYPTION_KEY must decode to exactly 32 bytes, got {len(key)}."
        )
    return key


def get_hmac_key() -> bytes:
    """
    Return the HMAC key from HMAC_KEY env var, or fall back to the encryption
    key with a fixed domain separator. Using a separate key is better practice.
    """
    raw = os.getenv("HMAC_KEY")
    if raw:
        return base64.b64decode(raw)
    # Fallback: derive from the encryption key with a fixed prefix so the two
    # keys are different even when only one env var is set.
    return b"hmac-phone-v1:" + get_encryption_key()

