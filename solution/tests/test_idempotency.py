"""
test_idempotency.py — Tests for idempotent and sequential ingestion.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solution.db import get_quality
from solution.ingest_events import ingest_jsonl_drop
from solution.ingest_leads import ingest_csv_batch


def lead_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


def event_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


class TestIdempotency:
    def test_re_ingest_events_drop_no_change(self, db_conn, tmp_path):
        """Re-ingesting the exact same event drop produces no new events or counters."""
        drop = tmp_path / "drop_1"
        events_dir = drop / "events"
        events_dir.mkdir(parents=True)
        (events_dir / "events_0001.jsonl").write_text(
            '{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"}\n',
            encoding="utf-8",
        )
        ingest_jsonl_drop(db_conn, drop)
        assert event_count(db_conn) == 1
        assert get_quality(db_conn)["events_total"] == 1

        # Re-ingest
        ingest_jsonl_drop(db_conn, drop)
        assert event_count(db_conn) == 1
        assert get_quality(db_conn)["events_total"] == 1
    def test_re_ingest_same_drop_no_change(self, db_conn, tmp_path):
        """Running ingest twice on the same batch produces identical state."""
        content = textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,POL001
            Priya Nair,8888888888,POL002
        """)
        p = tmp_path / "batch_001.csv"
        p.write_text(content, encoding="utf-8")

        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", p)
        leads_after_first = lead_count(db_conn)
        total_after_first = get_quality(db_conn)["lead_rows_total"]

        # Second ingest of same batch
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", p)

        assert lead_count(db_conn) == leads_after_first
        assert get_quality(db_conn)["lead_rows_total"] == total_after_first

    def test_sequential_batches_accumulate(self, db_conn, tmp_path):
        """Different batches add to the total count correctly."""
        p1 = tmp_path / "batch_001.csv"
        p1.write_text(
            "customer_name,phone,policy_number\nAarav Sharma,9876543210,POL001\n",
            encoding="utf-8",
        )
        p2 = tmp_path / "batch_002.csv"
        p2.write_text(
            "customer_name,phone,policy_number\nPriya Nair,8888888888,POL002\n",
            encoding="utf-8",
        )

        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", p1)
        ingest_csv_batch(db_conn, "motor_renewal", "batch_002", p2)

        assert lead_count(db_conn) == 2
        assert get_quality(db_conn)["lead_rows_total"] == 2

