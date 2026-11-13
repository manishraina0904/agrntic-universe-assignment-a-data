"""
conftest.py — shared pytest fixtures.

Provides:
  - A fresh in-memory SQLite database for each test.
  - A test encryption key set in the environment.
  - Helper functions to build minimal schemas and CSV data.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from solution.db import get_connection, init_db

# ── Encryption key fixture ──────────────────────────────────────────────────

TEST_KEY_32 = b"test-key-32-bytes-padded-to-fit!"  # exactly 32 bytes
TEST_KEY_B64 = base64.b64encode(TEST_KEY_32).decode()


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    """Set required environment variables for every test."""
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY_B64)
    # Clear HMAC_KEY so it derives from encryption key (consistent behaviour)
    monkeypatch.delenv("HMAC_KEY", raising=False)


# ── Database fixture ────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """
    Return an initialised in-memory SQLite connection.
    Automatically closed after each test.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    yield conn
    conn.close()


# ── Temporary file helpers ──────────────────────────────────────────────────

@pytest.fixture
def tmp_csv(tmp_path):
    """
    Return a helper that writes a CSV string to a temp file and
    returns its Path. Usage: path = tmp_csv("header\nrow1\nrow2")
    """
    def _write(content: str, filename: str = "batch_001.csv") -> Path:
        p = tmp_path / filename
        p.write_text(content, encoding="utf-8")
        return p
    return _write


@pytest.fixture
def tmp_jsonl(tmp_path):
    """
    Return a helper that writes a JSONL string to a temp events file.
    """
    def _write(content: str, filename: str = "events_0001.jsonl") -> Path:
        events_dir = tmp_path / "drop_test" / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        p = events_dir / filename
        p.write_text(content, encoding="utf-8")
        return tmp_path / "drop_test"  # return the drop directory
    return _write

