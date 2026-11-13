"""
ingest_events.py — JSONL event ingestion and orphan recovery.

Processes each line in order:
  1. Malformed (invalid JSON or no event_id) → count + skip
  2. Duplicate event_id                      → count + skip
  3. Orphan (can't link to a lead)           → store with lead_id=NULL
  4. Unknown disposition code                → count + skip INSERT

After each call to ingest_jsonl_drop(), orphan recovery is attempted
so events that arrived before their lead get linked retroactively.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solution.db import increment_quality
from solution.validators import validate_phone

IST = timezone(timedelta(hours=5, minutes=30))

# The six supported event types
SUPPORTED_EVENT_TYPES = {
    "call.initiated",
    "call.telephony_failed",
    "call.connected",
    "call.ended",
    "disposition.set",
    "disposition.override",
}

# Disposition event types that need catalogue validation
DISPOSITION_EVENT_TYPES = {"disposition.set", "disposition.override"}


def parse_timestamp(occurred_at: str) -> str:
    """
    Parse a timestamp in one of three forms and return a UTC ISO-8601 string.

    Forms:
      "2026-10-05T13:20:21Z"          — UTC
      "2026-10-05T13:20:21+05:30"     — with offset
      "2026-10-05 13:20:21"           — naive → treated as IST
    """
    s = occurred_at.strip()

    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    elif "T" in s and ("+" in s[10:] or "-" in s[10:]):
        # fromisoformat handles +05:30 style in Python 3.11+
        dt = datetime.fromisoformat(s)
    else:
        # Naive — treat as IST
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_dispositions(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """
    Load all valid disposition codes keyed by workflow_id.
    We store disposition info in a separate catalogue loaded from the CSV.
    Since this is loaded at runtime from the CSV via the reports module,
    we query it here from a helper.
    """
    # Import here to avoid circular imports
    from solution.reports import load_disposition_catalogue
    cat = load_disposition_catalogue()
    return {wf: set(codes.keys()) for wf, codes in cat.items()}


def _get_phone_hmac(raw_phone: str) -> str | None:
    """
    Normalise and fingerprint a raw phone string.
    Returns the HMAC hex string, or None if the phone is invalid.
    """
    from solution.security import hmac_phone
    try:
        canonical = validate_phone(raw_phone)
        return hmac_phone(canonical)
    except ValueError:
        return None


def _find_lead_by_phone(
    conn: sqlite3.Connection, workflow_id: str, phone_hmac: str
) -> int | None:
    """Return lead.id for (workflow_id, phone_hmac), or None."""
    row = conn.execute(
        "SELECT id FROM leads WHERE workflow_id=? AND phone_hmac=?",
        (workflow_id, phone_hmac),
    ).fetchone()
    return row["id"] if row else None


def _find_lead_by_call_id(conn: sqlite3.Connection, call_id: str) -> int | None:
    """
    For disposition.override (which has no phone), find the lead via
    an existing event that shares the same call_id.
    """
    row = conn.execute(
        "SELECT lead_id FROM events WHERE call_id=? AND lead_id IS NOT NULL LIMIT 1",
        (call_id,),
    ).fetchone()
    return row["lead_id"] if row else None


def ingest_jsonl_drop(
    conn: sqlite3.Connection,
    drop_path: Path,
) -> None:
    """
    Ingest all JSONL event files in <drop_path>/events/, then attempt
    orphan recovery.

    Event files are sorted by filename so processing order is deterministic.
    """
    events_dir = drop_path / "events"
    if not events_dir.exists():
        return

    dispositions = _load_dispositions(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for jsonl_file in sorted(events_dir.iterdir()):
        if jsonl_file.suffix != ".jsonl":
            continue
        _ingest_one_jsonl(conn, jsonl_file, dispositions, now)

    # After processing all event files in this drop, try to link orphans
    recover_orphans(conn)
    conn.commit()


def _ingest_one_jsonl(
    conn: sqlite3.Connection,
    jsonl_path: Path,
    dispositions: dict[str, set[str]],
    ingested_at: str,
) -> None:
    """Process every line of one JSONL file."""
    # Use drop directory name + filename as key, e.g. "drop_1/events_0001.jsonl"
    file_key = f"{jsonl_path.parent.parent.name}/{jsonl_path.name}"
    already = conn.execute(
        "SELECT 1 FROM batches WHERE workflow_id='_events' AND batch_id=?", (file_key,)
    ).fetchone()
    if already:
        return  # already ingested; skip for idempotency

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue  # truly empty line — not counted

            increment_quality(conn, "events_total")

            # Step 1: Malformed?
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                increment_quality(conn, "malformed_events")
                continue

            if not isinstance(event, dict) or not event.get("event_id"):
                increment_quality(conn, "malformed_events")
                continue

            event_id = event["event_id"]
            event_type = event.get("event_type", "")
            workflow_id = event.get("workflow_id", "")
            call_id = event.get("call_id", "")
            occurred_at_raw = event.get("occurred_at", "")
            payload = event.get("payload", {})

            # Step 2: Duplicate?
            already = conn.execute(
                "SELECT 1 FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if already:
                increment_quality(conn, "duplicate_events_dropped")
                continue

            # Parse timestamp (best-effort; skip if unparseable)
            try:
                occurred_at_utc = parse_timestamp(occurred_at_raw)
            except (ValueError, KeyError):
                increment_quality(conn, "malformed_events")
                continue

            # Step 4: Unknown disposition code?
            if event_type in DISPOSITION_EVENT_TYPES:
                code = payload.get("disposition_code", "")
                valid_codes = dispositions.get(workflow_id, set())
                if code not in valid_codes:
                    increment_quality(conn, "unknown_disposition_events")
                    continue  # do not store

            # Determine lead_id (Step 3: Orphan check)
            phone_hmac: str | None = None
            lead_id: int | None = None

            if event_type == "disposition.override":
                # Links via call_id only — no phone in payload
                lead_id = _find_lead_by_call_id(conn, call_id)
            else:
                raw_phone = event.get("phone", "")
                if raw_phone:
                    phone_hmac = _get_phone_hmac(raw_phone)
                    if phone_hmac:
                        lead_id = _find_lead_by_phone(conn, workflow_id, phone_hmac)

            # Step 3 outcome: orphan if lead_id is still None
            if lead_id is None:
                # Orphan — stored with lead_id=NULL; recovered later
                pass  # lead_id remains None

            conn.execute(
                """
                INSERT INTO events (
                    event_id, event_type, workflow_id, call_id,
                    occurred_at_utc, phone_hmac, payload_json,
                    lead_id, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    workflow_id,
                    call_id,
                    occurred_at_utc,
                    phone_hmac,
                    json.dumps(payload),
                    lead_id,
                    ingested_at,
                ),
            )

    conn.execute(
        "INSERT INTO batches (workflow_id, batch_id, ingested_at) VALUES ('_events', ?, ?)",
        (file_key, ingested_at),
    )


def recover_orphans(conn: sqlite3.Connection) -> None:
    """
    Try to link orphan events (lead_id IS NULL) to leads that may have
    been ingested since the orphan was stored.

    Two strategies:
      1. Phone-based: match events.phone_hmac against leads.phone_hmac
      2. Call-id-based: for disposition.override, find a sibling event
         that is now linked.
    """
    # Strategy 1: phone-based recovery
    conn.execute(
        """
        UPDATE events
        SET lead_id = (
            SELECT l.id
            FROM leads l
            WHERE l.workflow_id = events.workflow_id
              AND l.phone_hmac  = events.phone_hmac
            LIMIT 1
        )
        WHERE lead_id IS NULL
          AND phone_hmac IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM leads l
            WHERE l.workflow_id = events.workflow_id
              AND l.phone_hmac  = events.phone_hmac
          )
        """
    )

    # Strategy 2: call_id-based recovery (e.g. for disposition.override which has no phone)
    conn.execute(
        """
        UPDATE events
        SET lead_id = (
            SELECT e2.lead_id
            FROM events e2
            WHERE e2.call_id = events.call_id
              AND e2.lead_id IS NOT NULL
            LIMIT 1
        )
        WHERE lead_id IS NULL
          AND EXISTS (
            SELECT 1 FROM events e2
            WHERE e2.call_id = events.call_id
              AND e2.lead_id IS NOT NULL
          )
        """
    )

