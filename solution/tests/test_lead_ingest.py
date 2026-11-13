"""
test_lead_ingest.py — Tests for CSV lead ingestion:
  - Header aliases and different column orders
  - Blank row skipping
  - Required field rejection
  - Invalid phone rejection
  - Optional field warnings
  - Duplicate lead merging (last-write-wins, permanent batch_id)
  - Defaults applied after merge
  - Idempotency (re-ingesting same batch)
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solution.db import get_quality
from solution.ingest_leads import ingest_csv_batch
from solution.security import decrypt


# ── Helpers ─────────────────────────────────────────────────────────────────

def lead_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


def rejection_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]


def get_lead(db_conn, workflow_id="motor_renewal"):
    return db_conn.execute(
        "SELECT * FROM leads WHERE workflow_id=?", (workflow_id,)
    ).fetchone()


# ══════════════════════════════════════════════════════════
# Header aliases
# ══════════════════════════════════════════════════════════

class TestHeaderAliases:
    def test_canonical_headers(self, db_conn, tmp_csv):
        """Standard motor_renewal headers work."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language
            Aarav Sharma,9876543210,MOT/2026/100000,2026-11-01,12000,en
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1

    def test_alias_headers_motor_renewal(self, db_conn, tmp_csv):
        """batch_003-style alias headers for motor_renewal."""
        csv_path = tmp_csv(textwrap.dedent("""\
            Insured Name,Mobile No,Policy No,Expiry Date,Premium,Language
            Aarav Sharma,9876543210,MOT/2026/100000,2026-11-01,12000,en
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1

    def test_different_column_order(self, db_conn, tmp_csv):
        """Columns in different order still parse correctly."""
        csv_path = tmp_csv(textwrap.dedent("""\
            Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium
            hi,Aarav Sharma,MOT/2026/100000,9876543210,2026-11-01,₹12000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1
        lead = get_lead(db_conn)
        assert lead["preferred_language"] == "hi"

    def test_unknown_columns_ignored(self, db_conn, tmp_csv):
        """Extra columns like 'agent_notes' are silently ignored."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,agent_notes,something_random
            Aarav Sharma,9876543210,MOT/2026/100000,VIP,extra
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1

    def test_alias_headers_policy_feedback(self, db_conn, tmp_csv):
        """Alias headers for policy_feedback workflow."""
        csv_path = tmp_csv(textwrap.dedent("""\
            Order ID,Name,Contact,Product,Order Date,Language
            ORD-700000,Aarav Sharma,9876543210,Phone,2026-09-01,en
        """))
        ingest_csv_batch(db_conn, "policy_feedback", "batch_001", csv_path)
        assert lead_count(db_conn) == 1


# ══════════════════════════════════════════════════════════
# Blank rows
# ══════════════════════════════════════════════════════════

class TestBlankRows:
    def test_blank_rows_skipped_not_counted(self, db_conn, tmp_csv):
        """Blank rows are silently skipped; row numbers are unaffected."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
            ,,,
            Priya Nair,8888888888,MOT/2026/100001
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 2
        total = get_quality(db_conn)["lead_rows_total"]
        assert total == 2  # blank row not counted

    def test_all_blank_rows(self, db_conn, tmp_csv):
        """File with only blank rows produces 0 leads."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            ,,,
            ,,,
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 0


# ══════════════════════════════════════════════════════════
# Rejections
# ══════════════════════════════════════════════════════════

class TestRejections:
    def test_missing_required_name(self, db_conn, tmp_csv):
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            ,9876543210,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 0
        assert rejection_count(db_conn) == 1
        r = db_conn.execute("SELECT reason FROM rejections").fetchone()
        assert r["reason"] == "missing_required:customer_name"

    def test_missing_required_phone(self, db_conn, tmp_csv):
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 0
        r = db_conn.execute("SELECT reason FROM rejections").fetchone()
        assert r["reason"] == "missing_required:phone"

    def test_invalid_phone_rejection(self, db_conn, tmp_csv):
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,N/A,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 0
        r = db_conn.execute("SELECT reason FROM rejections").fetchone()
        assert r["reason"] == "invalid_phone"

    def test_first_failure_wins(self, db_conn, tmp_csv):
        """Missing name is first schema field → that is the rejection reason."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            ,N/A,
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        r = db_conn.execute("SELECT reason FROM rejections").fetchone()
        # customer_name is field 1, phone is field 2 → first failure is missing_required:customer_name
        assert r["reason"] == "missing_required:customer_name"

    def test_rejection_has_row_number(self, db_conn, tmp_csv):
        """Row numbers are 1-based, counting only non-blank rows."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
            ,N/A,
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1
        r = db_conn.execute("SELECT row_number FROM rejections").fetchone()
        assert r["row_number"] == 2  # second data row

    def test_no_pii_in_rejection(self, db_conn, tmp_csv):
        """Rejection reason must not contain phone or name."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,N/A,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        r = db_conn.execute("SELECT reason FROM rejections").fetchone()
        assert "Aarav" not in r["reason"]
        assert "N/A" not in r["reason"]


# ══════════════════════════════════════════════════════════
# Optional field warnings
# ══════════════════════════════════════════════════════════

class TestOptionalWarnings:
    def test_invalid_optional_date_generates_warning(self, db_conn, tmp_csv):
        """Invalid optional field → null stored + field_warnings incremented."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,policy_expiry
            Aarav Sharma,9876543210,MOT/2026/100000,31/02/2026
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1  # not rejected
        assert get_quality(db_conn)["field_warnings"] == 1
        lead = get_lead(db_conn)
        assert lead["policy_expiry"] is None  # stored as null

    def test_invalid_enum_generates_warning(self, db_conn, tmp_csv):
        """Invalid enum value → null stored + warning."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,preferred_language
            Aarav Sharma,9876543210,MOT/2026/100000,Hindi
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1
        assert get_quality(db_conn)["field_warnings"] == 1

    def test_no_warning_for_rejected_rows(self, db_conn, tmp_csv):
        """Warnings are only counted for valid (non-rejected) rows."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,policy_expiry
            ,9876543210,MOT/2026/100000,31/02/2026
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert get_quality(db_conn)["field_warnings"] == 0  # rejected → no warning


# ══════════════════════════════════════════════════════════
# Duplicate merging
# ══════════════════════════════════════════════════════════

class TestDuplicateMerging:
    def test_duplicate_counted(self, db_conn, tmp_csv):
        """Second row with same phone → counted as merged duplicate."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,premium_amount
            Aarav Sharma,9876543210,MOT/2026/100000,12000
            Aarav Sharma,9876543210,MOT/2026/100000,15000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        assert lead_count(db_conn) == 1
        assert get_quality(db_conn)["duplicate_lead_rows_merged"] == 1

    def test_last_write_wins_on_non_null(self, db_conn, tmp_csv):
        """Later row's non-null values overwrite earlier values."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,premium_amount
            Aarav Sharma,9876543210,MOT/2026/100000,12000.00
            Aarav Sharma Updated,9876543210,MOT/2026/100000,15000.00
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        lead = get_lead(db_conn)
        assert lead["premium_amount"] == "15000.00"

    def test_null_does_not_overwrite(self, db_conn, tmp_csv):
        """Later row with null optional field does not erase existing value."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,premium_amount
            Aarav Sharma,9876543210,MOT/2026/100000,12000.00
            Aarav Sharma,9876543210,MOT/2026/100000,
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        lead = get_lead(db_conn)
        assert lead["premium_amount"] == "12000.00"  # original preserved

    def test_original_batch_id_permanent(self, db_conn, tmp_csv, tmp_path):
        """batch_id stays as the first batch even after a merge from a later batch."""
        csv1 = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
        """), filename="batch_001.csv")
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv1)

        csv2 = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma Updated,9876543210,MOT/2026/100000
        """), filename="batch_002.csv")
        ingest_csv_batch(db_conn, "motor_renewal", "batch_002", csv2)

        lead = get_lead(db_conn)
        assert lead["original_batch_id"] == "batch_001"  # never changed

    def test_cross_workflow_no_merge(self, db_conn, tmp_csv, tmp_path):
        """Same phone in different workflows → two separate leads."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)

        csv_path2 = tmp_path / "pf.csv"
        csv_path2.write_text(textwrap.dedent("""\
            customer_name,phone,order_id
            Aarav Sharma,9876543210,ORD-700000
        """), encoding="utf-8")
        ingest_csv_batch(db_conn, "policy_feedback", "batch_001", csv_path2)

        assert lead_count(db_conn) == 2


# ══════════════════════════════════════════════════════════
# Defaults
# ══════════════════════════════════════════════════════════

class TestDefaults:
    def test_default_language_applied(self, db_conn, tmp_csv):
        """preferred_language defaults to 'en' when not provided."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        lead = get_lead(db_conn)
        assert lead["preferred_language"] == "en"

    def test_default_not_applied_if_value_exists(self, db_conn, tmp_csv):
        """Existing preferred_language is not overwritten by default."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,preferred_language
            Aarav Sharma,9876543210,MOT/2026/100000,hi
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        lead = get_lead(db_conn)
        assert lead["preferred_language"] == "hi"

    def test_default_applied_after_invalid_enum(self, db_conn, tmp_csv):
        """Invalid enum → null → default 'en' applied."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number,preferred_language
            Aarav Sharma,9876543210,MOT/2026/100000,Hindi
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        lead = get_lead(db_conn)
        assert lead["preferred_language"] == "en"


# ══════════════════════════════════════════════════════════
# Idempotency
# ══════════════════════════════════════════════════════════

class TestIdempotency:
    def test_re_ingest_same_batch_no_change(self, db_conn, tmp_csv):
        """Re-ingesting the same batch produces no duplicates or extra rows."""
        csv_path = tmp_csv(textwrap.dedent("""\
            customer_name,phone,policy_number
            Aarav Sharma,9876543210,MOT/2026/100000
        """))
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)
        ingest_csv_batch(db_conn, "motor_renewal", "batch_001", csv_path)  # again

        assert lead_count(db_conn) == 1
        assert get_quality(db_conn)["lead_rows_total"] == 1  # counted only once

