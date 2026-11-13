"""
reports.py — Generate all five report files.

Reports are computed by querying the SQLite database.
No PII is exposed: phones are masked, names are never included.

Five outputs:
  1. batch_summary.csv
  2. daily_overview.csv
  3. lead_report.csv
  4. rejections.csv
  5. data_quality.json
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from solution.config import SCHEMA_DIR
from solution.db import get_quality
from solution.security import decrypt, mask_phone

IST = timezone(timedelta(hours=5, minutes=30))

# ── Disposition catalogue ───────────────────────────────────────────────────

def load_disposition_catalogue() -> dict[str, dict[str, dict]]:
    """
    Load dispositions.csv and return a nested dict:
      { workflow_id: { code: {"is_terminal": bool, "is_success": bool} } }
    """
    path = SCHEMA_DIR / "dispositions.csv"
    catalogue: dict[str, dict[str, dict]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            wf = row["workflow_id"]
            code = row["code"]
            if wf not in catalogue:
                catalogue[wf] = {}
            catalogue[wf][code] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
                "label": row["label"],
            }
    return catalogue


# ── IST helpers ─────────────────────────────────────────────────────────────

def utc_to_ist(utc_str: str) -> datetime:
    """Parse a UTC ISO string and convert to IST datetime."""
    dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def utc_to_ist_date(utc_str: str) -> str:
    """Return the IST date string (YYYY-MM-DD) for a UTC ISO string."""
    return utc_to_ist(utc_str).strftime("%Y-%m-%d")


def utc_to_ist_iso(utc_str: str) -> str:
    """Return ISO-8601 string in IST, e.g. 2026-10-14T17:55:24+05:30."""
    return utc_to_ist(utc_str).strftime("%Y-%m-%dT%H:%M:%S+05:30")


# ── Effective disposition ───────────────────────────────────────────────────

def get_effective_disposition(
    conn: sqlite3.Connection,
    call_id: str,
    catalogue: dict[str, dict],
) -> str | None:
    """
    Return the effective disposition code for a call, or None.

    Rule (from spec):
      - Latest disposition.override by occurred_at wins.
      - Else latest disposition.set by occurred_at.
      - Else None.
    """
    # Latest override
    override = conn.execute(
        """
        SELECT payload_json, occurred_at_utc
        FROM events
        WHERE call_id=? AND event_type='disposition.override' AND lead_id IS NOT NULL
        ORDER BY occurred_at_utc DESC
        LIMIT 1
        """,
        (call_id,),
    ).fetchone()
    if override:
        code = json.loads(override["payload_json"]).get("disposition_code")
        if code and code in catalogue:
            return code

    # Latest disposition.set
    dset = conn.execute(
        """
        SELECT payload_json, occurred_at_utc
        FROM events
        WHERE call_id=? AND event_type='disposition.set' AND lead_id IS NOT NULL
        ORDER BY occurred_at_utc DESC
        LIMIT 1
        """,
        (call_id,),
    ).fetchone()
    if dset:
        code = json.loads(dset["payload_json"]).get("disposition_code")
        if code and code in catalogue:
            return code

    return None


# ── Lead bucket computation ─────────────────────────────────────────────────

def compute_bucket(
    conn: sqlite3.Connection,
    lead_id: int,
    workflow_id: str,
    catalogue: dict[str, dict],
) -> str:
    """
    Compute the bucket for a lead based on its linked calls.

    Priority (spec order):
      1. yet_to_reach_out      — no calls
      2. completed             — any call has terminal effective disposition
      3. in_progress_telephony_failed — every call has telephony_failed
      4. in_progress_dialed    — everything else
    """
    wf_catalogue = catalogue.get(workflow_id, {})

    # All calls for this lead = all distinct call_ids from linked call.initiated events
    call_rows = conn.execute(
        """
        SELECT DISTINCT call_id
        FROM events
        WHERE lead_id=? AND event_type='call.initiated'
        """,
        (lead_id,),
    ).fetchall()
    call_ids = [r["call_id"] for r in call_rows]

    if not call_ids:
        return "yet_to_reach_out"

    for call_id in call_ids:
        disp = get_effective_disposition(conn, call_id, wf_catalogue)
        if disp and wf_catalogue.get(disp, {}).get("is_terminal"):
            return "completed"

    # Check if every call has telephony_failed
    all_failed = all(
        conn.execute(
            "SELECT 1 FROM events WHERE call_id=? AND event_type='call.telephony_failed' AND lead_id IS NOT NULL",
            (call_id,),
        ).fetchone()
        for call_id in call_ids
    )
    if all_failed:
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


# ── Report generators ───────────────────────────────────────────────────────

def _write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    """Write a CSV file with the given headers and rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def report_batch_summary(conn: sqlite3.Connection, out_dir: Path, catalogue: dict) -> None:
    """
    batch_summary.csv — one row per (workflow_id, batch_id), sorted by both.
    Counts leads in each bucket per batch.
    """
    headers = [
        "workflow_id", "batch_id", "total_leads",
        "yet_to_reach_out", "in_progress_telephony_failed",
        "in_progress_dialed", "completed",
    ]

    # Get all (workflow_id, original_batch_id) combinations
    batch_rows = conn.execute(
        "SELECT DISTINCT workflow_id, original_batch_id FROM leads ORDER BY workflow_id, original_batch_id"
    ).fetchall()

    rows = []
    for br in batch_rows:
        workflow_id = br["workflow_id"]
        batch_id = br["original_batch_id"]

        leads = conn.execute(
            "SELECT id FROM leads WHERE workflow_id=? AND original_batch_id=?",
            (workflow_id, batch_id),
        ).fetchall()

        counts = {
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        }
        for lead in leads:
            bucket = compute_bucket(conn, lead["id"], workflow_id, catalogue)
            counts[bucket] += 1

        rows.append([
            workflow_id, batch_id, len(leads),
            counts["yet_to_reach_out"],
            counts["in_progress_telephony_failed"],
            counts["in_progress_dialed"],
            counts["completed"],
        ])

    _write_csv(out_dir / "batch_summary.csv", headers, rows)


def report_daily_overview(conn: sqlite3.Connection, out_dir: Path, catalogue: dict) -> None:
    """
    daily_overview.csv — one row per workflow per IST date with ≥1 call.
    Sorted by workflow_id, date.
    """
    headers = [
        "workflow_id", "date", "total_calls",
        "connected_calls", "connectivity_rate", "success_leads",
    ]

    # Get all linked call.initiated events (each unique call counted once)
    initiated_rows = conn.execute(
        """
        SELECT workflow_id, call_id, MIN(occurred_at_utc) as occurred_at_utc, lead_id
        FROM events
        WHERE event_type='call.initiated' AND lead_id IS NOT NULL
        GROUP BY workflow_id, call_id
        ORDER BY workflow_id, MIN(occurred_at_utc)
        """
    ).fetchall()

    # Group by (workflow_id, IST date)
    from collections import defaultdict
    # day_calls[(workflow_id, date)] = list of (call_id, lead_id)
    day_calls: dict[tuple, list] = defaultdict(list)
    for row in initiated_rows:
        ist_date = utc_to_ist_date(row["occurred_at_utc"])
        day_calls[(row["workflow_id"], ist_date)].append(
            (row["call_id"], row["lead_id"])
        )

    rows = []
    for (workflow_id, date) in sorted(day_calls.keys()):
        calls = day_calls[(workflow_id, date)]
        wf_catalogue = catalogue.get(workflow_id, {})
        total_calls = len(calls)

        connected_calls = sum(
            1 for call_id, _ in calls
            if conn.execute(
                "SELECT 1 FROM events WHERE call_id=? AND event_type='call.connected' AND lead_id IS NOT NULL",
                (call_id,),
            ).fetchone()
        )

        connectivity_rate = connected_calls / total_calls if total_calls else 0
        rate_str = f"{connectivity_rate:.4f}"

        # success_leads: distinct leads with ≥1 call on this date with is_success disposition
        success_leads_set: set[int] = set()
        for call_id, lead_id in calls:
            disp = get_effective_disposition(conn, call_id, wf_catalogue)
            if disp and wf_catalogue.get(disp, {}).get("is_success"):
                success_leads_set.add(lead_id)

        rows.append([workflow_id, date, total_calls, connected_calls, rate_str, len(success_leads_set)])

    _write_csv(out_dir / "daily_overview.csv", headers, rows)


def report_lead_report(conn: sqlite3.Connection, out_dir: Path, catalogue: dict) -> None:
    """
    lead_report.csv — one row per lead, sorted by workflow_id, external_ref.
    """
    headers = [
        "workflow_id", "external_ref", "batch_id", "phone_masked",
        "bucket", "total_calls", "connected_calls",
        "last_call_at", "current_disposition", "ever_success",
    ]

    leads = conn.execute(
        "SELECT * FROM leads ORDER BY workflow_id, external_ref"
    ).fetchall()

    rows = []
    for lead in leads:
        lead_id = lead["id"]
        workflow_id = lead["workflow_id"]
        wf_catalogue = catalogue.get(workflow_id, {})

        # Decrypt phone for masking
        canonical_phone = decrypt(lead["phone_enc"])
        phone_masked = mask_phone(canonical_phone)

        bucket = compute_bucket(conn, lead_id, workflow_id, catalogue)

        # All linked calls (call.initiated), sorted by occurred_at
        call_rows = conn.execute(
            """
            SELECT call_id, MIN(occurred_at_utc) as occurred_at_utc
            FROM events
            WHERE lead_id=? AND event_type='call.initiated'
            GROUP BY call_id
            ORDER BY MIN(occurred_at_utc)
            """,
            (lead_id,),
        ).fetchall()
        call_ids_ordered = [(r["call_id"], r["occurred_at_utc"]) for r in call_rows]

        total_calls = len(call_ids_ordered)
        connected_calls = sum(
            1 for call_id, _ in call_ids_ordered
            if conn.execute(
                "SELECT 1 FROM events WHERE call_id=? AND event_type='call.connected' AND lead_id IS NOT NULL",
                (call_id,),
            ).fetchone()
        )

        last_call_at = ""
        if call_ids_ordered:
            last_utc = call_ids_ordered[-1][1]
            last_call_at = utc_to_ist_iso(last_utc)

        # current_disposition: effective disposition of the most recent call that has one
        current_disposition = ""
        for call_id, _ in reversed(call_ids_ordered):
            disp = get_effective_disposition(conn, call_id, wf_catalogue)
            if disp:
                current_disposition = disp
                break

        # ever_success: any call has is_success effective disposition
        ever_success = False
        for call_id, _ in call_ids_ordered:
            disp = get_effective_disposition(conn, call_id, wf_catalogue)
            if disp and wf_catalogue.get(disp, {}).get("is_success"):
                ever_success = True
                break

        rows.append([
            workflow_id,
            lead["external_ref"],
            lead["original_batch_id"],
            phone_masked,
            bucket,
            total_calls,
            connected_calls,
            last_call_at,
            current_disposition,
            str(ever_success).lower(),  # 'true' or 'false'
        ])

    _write_csv(out_dir / "lead_report.csv", headers, rows)


def report_rejections(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    rejections.csv — sorted by workflow_id, batch_id, row_number. No PII.
    """
    headers = ["workflow_id", "batch_id", "row_number", "reason"]

    rows_db = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number
        """
    ).fetchall()

    rows = [[r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]] for r in rows_db]
    _write_csv(out_dir / "rejections.csv", headers, rows)


def report_data_quality(conn: sqlite3.Connection, out_dir: Path) -> None:
    """
    data_quality.json — exact top-level keys as specified.
    orphan_events = current count of events WHERE lead_id IS NULL (final state).
    rejections_by_reason = grouped from rejections table.
    """
    counters = get_quality(conn)

    # orphan_events is computed live (final state after all recovery)
    orphan_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE lead_id IS NULL"
    ).fetchone()["cnt"]

    # rejections_by_reason grouped from the rejections table
    reason_rows = conn.execute(
        "SELECT reason, COUNT(*) as cnt FROM rejections GROUP BY reason"
    ).fetchall()
    rejections_by_reason = {r["reason"]: r["cnt"] for r in reason_rows}

    output = {
        "lead_rows_total": counters.get("lead_rows_total", 0),
        "lead_rows_rejected": counters.get("lead_rows_rejected", 0),
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": counters.get("duplicate_lead_rows_merged", 0),
        "field_warnings": counters.get("field_warnings", 0),
        "events_total": counters.get("events_total", 0),
        "malformed_events": counters.get("malformed_events", 0),
        "duplicate_events_dropped": counters.get("duplicate_events_dropped", 0),
        "orphan_events": orphan_count,
        "unknown_disposition_events": counters.get("unknown_disposition_events", 0),
    }

    out_path = out_dir / "data_quality.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)


def generate_all_reports(conn: sqlite3.Connection, out_dir: Path) -> None:
    """Generate all five report files into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    catalogue = load_disposition_catalogue()

    report_batch_summary(conn, out_dir, catalogue)
    report_daily_overview(conn, out_dir, catalogue)
    report_lead_report(conn, out_dir, catalogue)
    report_rejections(conn, out_dir)
    report_data_quality(conn, out_dir)

