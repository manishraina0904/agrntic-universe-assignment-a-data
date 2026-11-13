"""
ingest_leads.py — CSV lead ingestion.

Reads one CSV batch file, validates and normalises every row against the
workflow schema, inserts valid leads into the database, and records rejections.
Duplicate leads are merged (last-write-wins on non-null fields).
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from solution.db import increment_quality
from solution.schema_loader import build_alias_map, load_schema, normalise_key
from solution.security import encrypt, hmac_phone
from solution.validators import (
    validate_date,
    validate_decimal,
    validate_enum,
    validate_phone,
)


def _is_blank_row(raw_row: dict) -> bool:
    """Blank rows (every cell empty or whitespace) are skipped and not counted."""
    for v in raw_row.values():
        if v is None:
            continue
        if isinstance(v, list):
            if any(item and str(item).strip() for item in v):
                return False
        elif str(v).strip():
            return False
    return True


def ingest_csv_batch(
    conn: sqlite3.Connection,
    workflow_id: str,
    batch_id: str,
    csv_path: Path,
) -> None:
    """
    Ingest one CSV file into the database.

    - Skips the file entirely if (workflow_id, batch_id) already exists
      in the batches table (idempotency).
    - Processes rows in file order.
    - Rejected rows go to the rejections table, not to leads.
    - Duplicate valid rows merge into the existing lead.
    - Defaults are applied after all rows in the file are processed.
    """
    # Idempotency check
    existing = conn.execute(
        "SELECT 1 FROM batches WHERE workflow_id=? AND batch_id=?",
        (workflow_id, batch_id),
    ).fetchone()
    if existing:
        return  # already ingested; skip

    schema = load_schema(workflow_id)
    alias_map = build_alias_map(schema)
    fields = schema["fields"]  # in schema order

    now = datetime.now(timezone.utc).isoformat()

    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        # Build column→field mapping from the actual CSV headers
        # Columns that match nothing in the schema are silently ignored.
        col_to_field: dict[str, str] = {}
        for col in (reader.fieldnames or []):
            field_name = alias_map.get(normalise_key(col))
            if field_name:
                col_to_field[col] = field_name

        row_number = 0  # counts non-blank data rows

        for raw_row in reader:
            # Skip blank rows (every cell empty or whitespace)
            if _is_blank_row(raw_row):
                continue

            # Map CSV columns to field names
            row: dict[str, str | None] = {}
            for col, val in raw_row.items():
                if col is None:
                    continue
                field_name = col_to_field.get(col)
                if field_name:
                    trimmed = (val or "").strip() if isinstance(val, str) else str(val).strip()
                    row[field_name] = trimmed if trimmed else None

            row_number += 1
            increment_quality(conn, "lead_rows_total")

            # Validate row; collect the first rejection reason
            rejection_reason = _validate_row(row, fields)

            if rejection_reason:
                increment_quality(conn, "lead_rows_rejected")
                conn.execute(
                    "INSERT INTO rejections (workflow_id, batch_id, row_number, reason) "
                    "VALUES (?, ?, ?, ?)",
                    (workflow_id, batch_id, row_number, rejection_reason),
                )
                continue

            # Row is valid — normalise fields and count optional warnings
            normalised, warnings = _normalise_row(row, fields)
            increment_quality(conn, "field_warnings", by=warnings)

            # Store the lead
            _upsert_lead(conn, workflow_id, batch_id, normalised, now)

    # Apply defaults to leads in this batch that still have null values
    _apply_defaults(conn, workflow_id, fields)

    # Record the batch as ingested
    conn.execute(
        "INSERT INTO batches (workflow_id, batch_id, ingested_at) VALUES (?, ?, ?)",
        (workflow_id, batch_id, now),
    )
    conn.commit()


def _validate_row(
    row: dict[str, str | None],
    fields: list[dict],
) -> str | None:
    """
    Walk fields in schema order. Return the first rejection reason or None.

    Rejection reasons:
      missing_required:<field_name>  — required field is missing
      invalid_phone                  — phone is present but invalid
    """
    for field in fields:
        name = field["name"]
        value = row.get(name)
        required = field.get("required", False)

        if required and not value:
            return f"missing_required:{name}"

        if name == "phone" and value:
            try:
                validate_phone(value)
            except ValueError:
                return "invalid_phone"

    return None


def _normalise_row(
    row: dict[str, str | None],
    fields: list[dict],
) -> tuple[dict[str, Any], int]:
    """
    Normalise every field value according to its type.

    For optional fields that are present but invalid:
      - store None (the warning is counted by the caller)

    Returns (normalised_dict, warning_count).
    Phone is returned as the canonical +91XXXXXXXXXX form.
    """
    normalised: dict[str, Any] = {}
    warnings = 0

    for field in fields:
        name = field["name"]
        value = row.get(name)
        ftype = field["type"]
        required = field.get("required", False)

        if not value:
            normalised[name] = None
            continue

        try:
            if ftype == "phone":
                normalised[name] = validate_phone(value)
            elif ftype == "date":
                normalised[name] = validate_date(value)
            elif ftype == "decimal":
                normalised[name] = validate_decimal(value)
            elif ftype == "enum":
                normalised[name] = validate_enum(value, field.get("values", []))
            else:  # string
                normalised[name] = value
        except ValueError:
            if required:
                # Should have been caught in _validate_row; shouldn't reach here
                normalised[name] = None
            else:
                # Optional field present but invalid → store null, count warning
                normalised[name] = None
                warnings += 1

    return normalised, warnings


def _upsert_lead(
    conn: sqlite3.Connection,
    workflow_id: str,
    batch_id: str,
    normalised: dict[str, Any],
    now: str,
) -> None:
    """
    Insert a new lead or merge into an existing one.

    First occurrence: INSERT with original_batch_id = batch_id.
    Later occurrence (same workflow+phone): UPDATE non-null fields only
    (last-write-wins). original_batch_id is never changed.
    """
    canonical_phone: str = normalised["phone"]   # +91XXXXXXXXXX
    phone_h = hmac_phone(canonical_phone)
    phone_e = encrypt(canonical_phone)

    name = normalised.get("customer_name")
    name_e = encrypt(name) if name else None

    external_ref = (
        normalised.get("policy_number")
        or normalised.get("order_id")
        or ""
    )

    existing = conn.execute(
        "SELECT id FROM leads WHERE workflow_id=? AND phone_hmac=?",
        (workflow_id, phone_h),
    ).fetchone()

    if existing is None:
        # First occurrence — INSERT
        conn.execute(
            """
            INSERT INTO leads (
                workflow_id, phone_hmac, phone_enc, customer_name_enc,
                external_ref, original_batch_id,
                policy_expiry, premium_amount, product, order_date,
                preferred_language, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                phone_h,
                phone_e,
                name_e,
                external_ref,
                batch_id,
                normalised.get("policy_expiry"),
                str(normalised["premium_amount"]) if normalised.get("premium_amount") is not None else None,
                normalised.get("product"),
                normalised.get("order_date"),
                normalised.get("preferred_language"),
                now,
                now,
            ),
        )
    else:
        # Duplicate — merge (last-write-wins on non-null values)
        increment_quality(conn, "duplicate_lead_rows_merged")

        # Build the SET clause dynamically for non-null incoming values
        updates: list[tuple[str, Any]] = []

        if name_e is not None:
            updates.append(("customer_name_enc", name_e))
        if external_ref:
            updates.append(("external_ref", external_ref))
        if normalised.get("policy_expiry") is not None:
            updates.append(("policy_expiry", normalised["policy_expiry"]))
        if normalised.get("premium_amount") is not None:
            updates.append(("premium_amount", str(normalised["premium_amount"])))
        if normalised.get("product") is not None:
            updates.append(("product", normalised["product"]))
        if normalised.get("order_date") is not None:
            updates.append(("order_date", normalised["order_date"]))
        if normalised.get("preferred_language") is not None:
            updates.append(("preferred_language", normalised["preferred_language"]))

        updates.append(("updated_at", now))

        if updates:
            set_clause = ", ".join(f"{col}=?" for col, _ in updates)
            values = [v for _, v in updates]
            values.append(existing["id"])
            conn.execute(
                f"UPDATE leads SET {set_clause} WHERE id=?",
                values,
            )


def _apply_defaults(conn: sqlite3.Connection, workflow_id: str, fields: list[dict]) -> None:
    """
    Apply schema defaults to leads that still have null values after merging.
    Currently only 'preferred_language' has a default ('en').
    """
    for field in fields:
        default = field.get("default")
        if default is not None:
            col = field["name"]
            conn.execute(
                f"UPDATE leads SET {col}=? WHERE workflow_id=? AND {col} IS NULL",
                (default, workflow_id),
            )

