"""
db.py — SQLite database setup and connection.

Five tables (minimal):
  batches       — tracks ingested CSV batches (idempotency)
  leads         — one row per unique (workflow_id, phone_hmac); PII encrypted
  events        — append-only interaction ledger; lead_id=NULL means orphan
  rejections    — rejected lead rows for rejections.csv
  data_quality  — running key/value counters for data_quality.json
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from solution.config import DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """
    Open (or create) the SQLite database and return a connection.
    Uses WAL mode for better concurrency and sets a helpful row factory.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """
    Create all tables and indexes if they don't already exist.
    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS.
    """
    conn.executescript("""
        -- Tracks ingested CSV batches. Prevents re-ingesting the same file.
        CREATE TABLE IF NOT EXISTS batches (
            workflow_id TEXT NOT NULL,
            batch_id    TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            PRIMARY KEY (workflow_id, batch_id)
        );

        -- One row per unique lead identified by (workflow_id, phone_hmac).
        -- PII fields (customer_name, phone) are encrypted at rest.
        -- Non-PII fields are stored in plaintext.
        CREATE TABLE IF NOT EXISTS leads (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id         TEXT NOT NULL,
            phone_hmac          TEXT NOT NULL,     -- HMAC-SHA256 for matching
            phone_enc           TEXT NOT NULL,     -- AES-GCM encrypted canonical phone
            customer_name_enc   TEXT,              -- AES-GCM encrypted
            external_ref        TEXT NOT NULL,     -- policy_number or order_id (not PII)
            original_batch_id   TEXT NOT NULL,     -- set once; never updated
            -- Optional workflow-specific fields (not PII)
            policy_expiry       TEXT,
            premium_amount      TEXT,              -- stored as string to preserve Decimal precision
            product             TEXT,
            order_date          TEXT,
            preferred_language  TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE (workflow_id, phone_hmac)
        );

        -- Append-only interaction ledger. Events are NEVER updated.
        -- lead_id=NULL means the event is an orphan (lead not yet ingested).
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL UNIQUE,  -- natural key; deduplication
            event_type      TEXT NOT NULL,
            workflow_id     TEXT NOT NULL,
            call_id         TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,          -- stored as UTC ISO-8601 string
            phone_hmac      TEXT,                  -- NULL for disposition.override
            payload_json    TEXT NOT NULL,
            lead_id         INTEGER REFERENCES leads(id),  -- NULL = orphan
            ingested_at     TEXT NOT NULL
        );

        -- Rejected lead rows. No PII stored.
        CREATE TABLE IF NOT EXISTS rejections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            batch_id    TEXT NOT NULL,
            row_number  INTEGER NOT NULL,
            reason      TEXT NOT NULL
        );

        -- Running counters for data_quality.json.
        -- Keys match the JSON output keys exactly.
        CREATE TABLE IF NOT EXISTS data_quality (
            key   TEXT NOT NULL PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );

        -- Indexes for common query patterns
        CREATE INDEX IF NOT EXISTS idx_events_lead_id
            ON events (lead_id);
        CREATE INDEX IF NOT EXISTS idx_events_call_id
            ON events (call_id);
        CREATE INDEX IF NOT EXISTS idx_events_phone_hmac
            ON events (phone_hmac);
        CREATE INDEX IF NOT EXISTS idx_leads_workflow_phone
            ON leads (workflow_id, phone_hmac);
    """)
    _ensure_quality_keys(conn)
    conn.commit()


# The exact keys required in data_quality.json
_QUALITY_KEYS = [
    "lead_rows_total",
    "lead_rows_rejected",
    "duplicate_lead_rows_merged",
    "field_warnings",
    "events_total",
    "malformed_events",
    "duplicate_events_dropped",
    "unknown_disposition_events",
]


def _ensure_quality_keys(conn: sqlite3.Connection) -> None:
    """Insert any missing counter rows with value 0."""
    for key in _QUALITY_KEYS:
        conn.execute(
            "INSERT OR IGNORE INTO data_quality (key, value) VALUES (?, 0)",
            (key,),
        )


def increment_quality(conn: sqlite3.Connection, key: str, by: int = 1) -> None:
    """Atomically increment a data_quality counter."""
    conn.execute(
        "UPDATE data_quality SET value = value + ? WHERE key = ?",
        (by, key),
    )


def get_quality(conn: sqlite3.Connection) -> dict:
    """Return all data_quality counters as a plain dict."""
    rows = conn.execute("SELECT key, value FROM data_quality").fetchall()
    return {row["key"]: row["value"] for row in rows}

