"""
test_event_ingest.py — Tests for JSONL event ingestion:
  - Malformed event handling
  - Event deduplication across drops
  - Orphan detection and recovery
  - disposition.override linking via call_id
  - Unknown disposition code handling
  - IST timestamp conversion
  - events_total counting
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from solution.db import get_quality
from solution.ingest_events import ingest_jsonl_drop, parse_timestamp, recover_orphans
from solution.ingest_leads import ingest_csv_batch


# ── Helpers ─────────────────────────────────────────────────────────────────

def event_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def orphan_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM events WHERE lead_id IS NULL").fetchone()[0]


def linked_count(db_conn) -> int:
    return db_conn.execute("SELECT COUNT(*) FROM events WHERE lead_id IS NOT NULL").fetchone()[0]


def make_drop(tmp_path: Path, events: list[dict | str], drop_name: str = "drop_test") -> Path:
    """Create a drop directory with one JSONL events file."""
    drop = tmp_path / drop_name
    events_dir = drop / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in events:
        lines.append(e if isinstance(e, str) else json.dumps(e))
    (events_dir / "events_0001.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return drop


def make_lead_csv(tmp_path: Path, phone: str, workflow: str = "motor_renewal") -> Path:
    """Write a minimal valid lead CSV."""
    if workflow == "motor_renewal":
        content = f"customer_name,phone,policy_number\nTest User,{phone},POL001\n"
    else:
        content = f"customer_name,phone,order_id\nTest User,{phone},ORD001\n"
    p = tmp_path / f"{workflow}_leads.csv"
    p.write_text(content, encoding="utf-8")
    return p


def insert_lead(db_conn, tmp_path, phone="9876543210", workflow="motor_renewal") -> None:
    csv_path = make_lead_csv(tmp_path, phone, workflow)
    ingest_csv_batch(db_conn, workflow, "batch_001", csv_path)


# ══════════════════════════════════════════════════════════
# Malformed events
# ══════════════════════════════════════════════════════════

class TestMalformedEvents:
    def test_invalid_json_counted(self, db_conn, tmp_path):
        drop = make_drop(tmp_path, ['{"broken json"'])
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["malformed_events"] == 1
        assert event_count(db_conn) == 0

    def test_missing_event_id_counted(self, db_conn, tmp_path):
        drop = make_drop(tmp_path, [{"event_type": "call.initiated", "workflow_id": "motor_renewal"}])
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["malformed_events"] == 1
        assert event_count(db_conn) == 0

    def test_empty_lines_not_counted(self, db_conn, tmp_path):
        """Truly empty lines are not counted in events_total."""
        drop = tmp_path / "drop_e"
        (drop / "events").mkdir(parents=True)
        (drop / "events" / "events_0001.jsonl").write_text("\n\n\n", encoding="utf-8")
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["events_total"] == 0

    def test_events_total_counts_malformed(self, db_conn, tmp_path):
        """events_total includes malformed lines (non-empty)."""
        drop = make_drop(tmp_path, ['not json at all'])
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["events_total"] == 1
        assert get_quality(db_conn)["malformed_events"] == 1


# ══════════════════════════════════════════════════════════
# Deduplication
# ══════════════════════════════════════════════════════════

class TestEventDedup:
    def _make_event(self, phone="9876543210"):
        return {
            "event_id": "evt_00000001",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_00000001",
            "occurred_at": "2026-10-05T10:00:00Z",
            "phone": phone,
            "payload": {},
        }

    def test_duplicate_in_same_drop(self, db_conn, tmp_path):
        e = self._make_event()
        drop = make_drop(tmp_path, [e, e])
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["duplicate_events_dropped"] == 1
        assert event_count(db_conn) == 1

    def test_duplicate_across_drops(self, db_conn, tmp_path):
        e = self._make_event()
        drop1 = make_drop(tmp_path, [e], drop_name="drop_1")
        drop2 = make_drop(tmp_path, [e], drop_name="drop_2")
        ingest_jsonl_drop(db_conn, drop1)
        ingest_jsonl_drop(db_conn, drop2)
        assert get_quality(db_conn)["duplicate_events_dropped"] == 1
        assert event_count(db_conn) == 1


# ══════════════════════════════════════════════════════════
# Orphan handling and recovery
# ══════════════════════════════════════════════════════════

class TestOrphans:
    def _make_event(self, phone="9876543210", eid="evt_00000001"):
        return {
            "event_id": eid,
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_00000001",
            "occurred_at": "2026-10-05T10:00:00Z",
            "phone": phone,
            "payload": {},
        }

    def test_event_for_unknown_phone_is_orphan(self, db_conn, tmp_path):
        """Event for a phone not in leads → orphan (lead_id=NULL)."""
        drop = make_drop(tmp_path, [self._make_event("9999999999")])
        ingest_jsonl_drop(db_conn, drop)
        assert orphan_count(db_conn) == 1
        assert linked_count(db_conn) == 0

    def test_event_linked_when_lead_exists(self, db_conn, tmp_path):
        """Event for a known phone → linked immediately."""
        insert_lead(db_conn, tmp_path, phone="9876543210")
        drop = make_drop(tmp_path, [self._make_event("9876543210")])
        ingest_jsonl_drop(db_conn, drop)
        assert orphan_count(db_conn) == 0
        assert linked_count(db_conn) == 1

    def test_orphan_recovered_when_lead_arrives_later(self, db_conn, tmp_path):
        """
        Event arrives in drop_1 for an unknown phone.
        Lead arrives in drop_2.
        After drop_2 ingest, the orphan must be linked.
        """
        # drop_1: event only, no lead yet
        drop1 = make_drop(tmp_path, [self._make_event("9876543210")], drop_name="drop_1")
        ingest_jsonl_drop(db_conn, drop1)
        assert orphan_count(db_conn) == 1

        # drop_2: lead arrives → orphan recovery triggered
        csv_path = make_lead_csv(tmp_path, "9876543210")
        ingest_csv_batch(db_conn, "motor_renewal", "batch_002", csv_path)
        recover_orphans(db_conn)
        db_conn.commit()

        assert orphan_count(db_conn) == 0
        assert linked_count(db_conn) == 1


# ══════════════════════════════════════════════════════════
# disposition.override linking via call_id
# ══════════════════════════════════════════════════════════

class TestOverrideLinking:
    def test_override_links_via_call_id(self, db_conn, tmp_path):
        """
        disposition.override has no phone. It must link via call_id
        to the lead that owns the other events in that call.
        """
        insert_lead(db_conn, tmp_path, phone="9876543210")

        events = [
            {
                "event_id": "evt_00000001",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_00000001",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "9876543210",
                "payload": {},
            },
            {
                "event_id": "evt_00000002",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_00000001",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "renewed"},
            },
            {
                # No phone — must link via call_id
                "event_id": "evt_00000003",
                "event_type": "disposition.override",
                "workflow_id": "motor_renewal",
                "call_id": "call_00000001",
                "occurred_at": "2026-10-05T12:00:00Z",
                "payload": {
                    "disposition_code": "not_interested",
                    "overridden_by": "supervisor_1",
                    "reason": "qa_review",
                },
            },
        ]
        drop = make_drop(tmp_path, events)
        ingest_jsonl_drop(db_conn, drop)

        # All 3 events should be linked (not orphans)
        assert orphan_count(db_conn) == 0
        assert linked_count(db_conn) == 3


# ══════════════════════════════════════════════════════════
# Unknown disposition
# ══════════════════════════════════════════════════════════

class TestUnknownDisposition:
    def test_unknown_disposition_code_not_stored(self, db_conn, tmp_path):
        """Events with unknown disposition codes are counted but NOT stored."""
        insert_lead(db_conn, tmp_path, phone="9876543210")
        events = [
            {
                "event_id": "evt_00000001",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_00000001",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "RENEWD"},  # typo — not in catalogue
            }
        ]
        drop = make_drop(tmp_path, events)
        ingest_jsonl_drop(db_conn, drop)
        assert get_quality(db_conn)["unknown_disposition_events"] == 1
        assert event_count(db_conn) == 0


# ══════════════════════════════════════════════════════════
# Timestamp / IST conversion
# ══════════════════════════════════════════════════════════

class TestTimestamps:
    def test_utc_z_format(self):
        result = parse_timestamp("2026-10-05T13:20:21Z")
        assert result == "2026-10-05T13:20:21Z"

    def test_ist_offset_format(self):
        result = parse_timestamp("2026-10-05T18:50:21+05:30")
        # 18:50:21+05:30 = 13:20:21Z
        assert result == "2026-10-05T13:20:21Z"

    def test_naive_treated_as_ist(self):
        result = parse_timestamp("2026-10-05 18:50:21")
        # Naive IST 18:50:21 = UTC 13:20:21
        assert result == "2026-10-05T13:20:21Z"

