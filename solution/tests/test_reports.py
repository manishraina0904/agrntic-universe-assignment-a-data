"""
test_reports.py — Tests for report generation:
  - Effective disposition (override wins, latest wins)
  - Lead bucket logic (all 4 buckets)
  - Exact report headers
  - Sorting
  - connectivity_rate 4 decimal places
  - success_leads calculation
  - ever_success, current_disposition, last_call_at
  - phone masking in reports
"""
from __future__ import annotations

import csv
import json
import textwrap
from pathlib import Path

import pytest

from solution.ingest_events import ingest_jsonl_drop, recover_orphans
from solution.ingest_leads import ingest_csv_batch
from solution.reports import (
    compute_bucket,
    generate_all_reports,
    get_effective_disposition,
    load_disposition_catalogue,
    utc_to_ist_date,
    utc_to_ist_iso,
)


# ── Fixtures & helpers ───────────────────────────────────────────────────────

@pytest.fixture
def catalogue():
    return load_disposition_catalogue()


def insert_full_lead(db_conn, tmp_path, phone="9876543210",
                     workflow="motor_renewal", batch="batch_001") -> int:
    """Insert a lead and return its id."""
    if workflow == "motor_renewal":
        content = f"customer_name,phone,policy_number\nTest User,{phone},POL001\n"
    else:
        content = f"customer_name,phone,order_id\nTest User,{phone},ORD001\n"
    p = tmp_path / f"{workflow}_{batch}.csv"
    p.write_text(content, encoding="utf-8")
    ingest_csv_batch(db_conn, workflow, batch, p)
    return db_conn.execute(
        "SELECT id FROM leads WHERE workflow_id=?", (workflow,)
    ).fetchone()["id"]


def insert_event(db_conn, event_id, event_type, call_id, lead_id,
                 occurred_at_utc="2026-10-05T10:00:00Z",
                 workflow_id="motor_renewal", payload=None):
    import json
    db_conn.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id,
            occurred_at_utc, phone_hmac, payload_json, lead_id, ingested_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?, '2026-10-05T10:00:00Z')
        """,
        (event_id, event_type, workflow_id, call_id, occurred_at_utc,
         json.dumps(payload or {}), lead_id),
    )


# ══════════════════════════════════════════════════════════
# IST conversion
# ══════════════════════════════════════════════════════════

class TestIST:
    def test_utc_to_ist_date(self):
        # UTC midnight → previous IST day would be wrong; ensure correct
        assert utc_to_ist_date("2026-10-05T00:00:00Z") == "2026-10-05"
        # 18:30 UTC = midnight IST next day
        assert utc_to_ist_date("2026-10-05T18:30:00Z") == "2026-10-06"

    def test_utc_to_ist_iso(self):
        result = utc_to_ist_iso("2026-10-05T10:00:00Z")
        assert result == "2026-10-05T15:30:00+05:30"


# ══════════════════════════════════════════════════════════
# Effective disposition
# ══════════════════════════════════════════════════════════

class TestEffectiveDisposition:
    def test_no_disposition_returns_none(self, db_conn, tmp_path, catalogue):
        # call with only call.initiated — no disposition
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id=lead_id)
        wf_cat = catalogue["motor_renewal"]
        assert get_effective_disposition(db_conn, "call_A", wf_cat) is None

    def test_disposition_set_returned(self, db_conn, tmp_path, catalogue):
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id)
        insert_event(db_conn, "e2", "disposition.set", "call_A", lead_id,
                     payload={"disposition_code": "renewed"})
        wf_cat = catalogue["motor_renewal"]
        assert get_effective_disposition(db_conn, "call_A", wf_cat) == "renewed"

    def test_override_wins_over_set(self, db_conn, tmp_path, catalogue):
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:00Z")
        insert_event(db_conn, "e2", "disposition.set", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:05:00Z",
                     payload={"disposition_code": "renewed"})
        insert_event(db_conn, "e3", "disposition.override", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T12:00:00Z",
                     payload={"disposition_code": "not_interested",
                               "overridden_by": "sup1", "reason": "qa"})
        wf_cat = catalogue["motor_renewal"]
        assert get_effective_disposition(db_conn, "call_A", wf_cat) == "not_interested"

    def test_latest_override_wins(self, db_conn, tmp_path, catalogue):
        """When there are two overrides, the later one wins."""
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "disposition.override", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:00Z",
                     payload={"disposition_code": "not_interested",
                               "overridden_by": "sup1", "reason": "r1"})
        insert_event(db_conn, "e2", "disposition.override", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T14:00:00Z",
                     payload={"disposition_code": "renewed",
                               "overridden_by": "sup2", "reason": "r2"})
        wf_cat = catalogue["motor_renewal"]
        assert get_effective_disposition(db_conn, "call_A", wf_cat) == "renewed"


# ══════════════════════════════════════════════════════════
# Bucket logic
# ══════════════════════════════════════════════════════════

class TestBucket:
    def test_yet_to_reach_out(self, db_conn, tmp_path, catalogue):
        """Lead with no linked call.initiated → yet_to_reach_out."""
        lead_id = insert_full_lead(db_conn, tmp_path)
        assert compute_bucket(db_conn, lead_id, "motor_renewal", catalogue) == "yet_to_reach_out"

    def test_completed_terminal_disposition(self, db_conn, tmp_path, catalogue):
        """Lead with terminal effective disposition → completed."""
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id)
        insert_event(db_conn, "e2", "disposition.set", "call_A", lead_id,
                     payload={"disposition_code": "renewed"})  # is_terminal=True
        assert compute_bucket(db_conn, lead_id, "motor_renewal", catalogue) == "completed"

    def test_in_progress_telephony_failed(self, db_conn, tmp_path, catalogue):
        """Every call has telephony_failed → in_progress_telephony_failed."""
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:00Z")
        insert_event(db_conn, "e2", "call.telephony_failed", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:03Z")
        assert compute_bucket(db_conn, lead_id, "motor_renewal", catalogue) == "in_progress_telephony_failed"

    def test_in_progress_dialed(self, db_conn, tmp_path, catalogue):
        """Call exists, no terminal disposition, not all telephony failed → in_progress_dialed."""
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id)
        insert_event(db_conn, "e2", "call.connected", "call_A", lead_id)
        # No disposition set
        assert compute_bucket(db_conn, lead_id, "motor_renewal", catalogue) == "in_progress_dialed"

    def test_completed_takes_priority_over_telephony_failed(self, db_conn, tmp_path, catalogue):
        """
        Two calls: call_A telephony_failed, call_B has terminal disposition.
        Result: completed (priority 2 > priority 3).
        """
        lead_id = insert_full_lead(db_conn, tmp_path)
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:00Z")
        insert_event(db_conn, "e2", "call.telephony_failed", "call_A", lead_id,
                     occurred_at_utc="2026-10-05T10:00:03Z")
        insert_event(db_conn, "e3", "call.initiated", "call_B", lead_id,
                     occurred_at_utc="2026-10-05T12:00:00Z")
        insert_event(db_conn, "e4", "disposition.set", "call_B", lead_id,
                     occurred_at_utc="2026-10-05T12:05:00Z",
                     payload={"disposition_code": "renewed"})
        assert compute_bucket(db_conn, lead_id, "motor_renewal", catalogue) == "completed"


# ══════════════════════════════════════════════════════════
# Report headers and sorting
# ══════════════════════════════════════════════════════════

class TestReportHeaders:
    def test_batch_summary_headers(self, db_conn, tmp_path):
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "batch_summary.csv") as f:
            headers = next(csv.reader(f))
        assert headers == [
            "workflow_id", "batch_id", "total_leads",
            "yet_to_reach_out", "in_progress_telephony_failed",
            "in_progress_dialed", "completed",
        ]

    def test_daily_overview_headers(self, db_conn, tmp_path):
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "daily_overview.csv") as f:
            headers = next(csv.reader(f))
        assert headers == [
            "workflow_id", "date", "total_calls",
            "connected_calls", "connectivity_rate", "success_leads",
        ]

    def test_lead_report_headers(self, db_conn, tmp_path):
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "lead_report.csv") as f:
            headers = next(csv.reader(f))
        assert headers == [
            "workflow_id", "external_ref", "batch_id", "phone_masked",
            "bucket", "total_calls", "connected_calls",
            "last_call_at", "current_disposition", "ever_success",
        ]

    def test_rejections_headers(self, db_conn, tmp_path):
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "rejections.csv") as f:
            headers = next(csv.reader(f))
        assert headers == ["workflow_id", "batch_id", "row_number", "reason"]

    def test_data_quality_keys(self, db_conn, tmp_path):
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "data_quality.json") as f:
            dq = json.load(f)
        expected_keys = {
            "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
            "duplicate_lead_rows_merged", "field_warnings", "events_total",
            "malformed_events", "duplicate_events_dropped",
            "orphan_events", "unknown_disposition_events",
        }
        assert set(dq.keys()) == expected_keys


# ══════════════════════════════════════════════════════════
# Calculations
# ══════════════════════════════════════════════════════════

class TestReportCalculations:
    def test_connectivity_rate_4_decimals(self, db_conn, tmp_path):
        """connectivity_rate must have exactly 4 decimal places."""
        insert_full_lead(db_conn, tmp_path, phone="9876543210")
        # 1 total call, 1 connected → rate = 1.0000
        insert_event(db_conn, "e1", "call.initiated", "call_A",
                     lead_id=1, occurred_at_utc="2026-10-05T10:00:00Z")
        insert_event(db_conn, "e2", "call.connected", "call_A",
                     lead_id=1, occurred_at_utc="2026-10-05T10:00:10Z")

        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "daily_overview.csv") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        rate = rows[0]["connectivity_rate"]
        # Must have exactly 4 decimal places
        parts = rate.split(".")
        assert len(parts) == 2 and len(parts[1]) == 4

    def test_ever_success_true(self, db_conn, tmp_path):
        """ever_success=true when any call has is_success disposition."""
        insert_full_lead(db_conn, tmp_path, phone="9876543210")
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id=1)
        insert_event(db_conn, "e2", "disposition.set", "call_A", lead_id=1,
                     payload={"disposition_code": "renewed"})  # is_success=True

        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "lead_report.csv") as f:
            row = list(csv.DictReader(f))[0]
        assert row["ever_success"] == "true"

    def test_ever_success_false(self, db_conn, tmp_path):
        """ever_success=false when no call has is_success disposition."""
        insert_full_lead(db_conn, tmp_path, phone="9876543210")
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id=1)
        insert_event(db_conn, "e2", "disposition.set", "call_A", lead_id=1,
                     payload={"disposition_code": "not_interested"})  # is_success=False

        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "lead_report.csv") as f:
            row = list(csv.DictReader(f))[0]
        assert row["ever_success"] == "false"

    def test_phone_masked_in_lead_report(self, db_conn, tmp_path):
        """Phone must be masked in lead_report.csv."""
        insert_full_lead(db_conn, tmp_path, phone="9876543210")
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "lead_report.csv") as f:
            row = list(csv.DictReader(f))[0]
        assert row["phone_masked"] == "+91******3210"
        assert "9876" not in row["phone_masked"]  # digits 1-6 hidden

    def test_last_call_at_ist_format(self, db_conn, tmp_path):
        """last_call_at must be ISO-8601 in IST (+05:30)."""
        insert_full_lead(db_conn, tmp_path, phone="9876543210")
        insert_event(db_conn, "e1", "call.initiated", "call_A", lead_id=1,
                     occurred_at_utc="2026-10-05T10:00:00Z")
        p = tmp_path / "reports"
        generate_all_reports(db_conn, p)
        with open(p / "lead_report.csv") as f:
            row = list(csv.DictReader(f))[0]
        # 10:00:00Z = 15:30:00+05:30
        assert row["last_call_at"] == "2026-10-05T15:30:00+05:30"

    def test_no_pii_in_rejections(self, db_conn, tmp_path):
        """rejections.csv must not contain phone numbers or names."""
        content = "customer_name,phone,policy_number\nAarav Sharma,N/A,POL001\n"
        p = tmp_path / "batch_001.csv"
        p.write_text(content, encoding="utf-8")
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", p)

        out = tmp_path / "reports"
        generate_all_reports(db_conn, out)
        text = (out / "rejections.csv").read_text()
        assert "Aarav" not in text
        assert "N/A" not in text

