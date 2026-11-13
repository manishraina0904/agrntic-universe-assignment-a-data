# Submission

## Candidate
- **Name:** Manish Raina
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 6h 30m

## How to run
```bash
# 1. Setup virtual environment from a clean clone
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure encryption key (.env)
cp .env.example .env
# Or generate an explicit key:
python -c "import os, base64; print(f'ENCRYPTION_KEY={base64.b64encode(os.urandom(32)).decode()}')" > .env

# 4. Run automated test suite
pytest

# 5. Generate sample data (seed 42, small scale)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 6. Execute pipeline
python -m solution.main ingest data/drop_1
python -m solution.main ingest data/drop_2
python -m solution.main report --out reports/
```

## Design

### Architecture
The pipeline follows an ETL architecture built for incremental, append-only processing:
1. **Schema-driven Normalisation & Ingestion:**
   Incoming CSV headers are normalised (lowercased, trimmed, whitespace/underscore collapsed) and mapped against field definitions and aliases. Valid rows are cleansed, validated, and inserted or merged.
2. **Append-Only Event Ledger:**
   Event JSONL streams are validated and appended without updates to historical records. Events link to leads via indexed phone HMACs or call identifiers. Overrides and state transitions are stored as distinct event records.
3. **Derived Metrics & Reporting:**
   Reports are computed using deterministic SQL queries over the ledger, calculating effective dispositions, call counts, connectivity rates, and lead buckets.

### Database Schema (SQLite)
SQLite was chosen for simplicity, zero configuration, and embedded file portability:
- **`batches`**: Stores `(workflow_id, batch_id, ingested_at)` as primary key. Provides idempotency for lead CSV batches.
- **`leads`**: Stores one row per unique `(workflow_id, phone_hmac)`.
  - PII fields (`customer_name`, `phone`) are stored encrypted using AES-GCM-256 (`customer_name_enc`, `phone_enc`).
  - Canonical phone is never stored in plaintext. `phone_hmac` (HMAC-SHA256) enables indexed $O(1)$ lookups.
  - `original_batch_id` is permanently fixed on initial insertion.
  - Non-PII fields (`external_ref`, dates, amounts) are stored as text to avoid float precision loss.
- **`events`**: Append-only event store with a unique constraint on `event_id`. Overrides never edit prior rows. `lead_id` references `leads(id)` and is `NULL` for orphan events.
- **`rejections`**: Stores rejected lead rows with 1-based non-blank row numbers and sanitised reason codes. Contains zero PII.
- **`data_quality`**: Key-value store maintaining running counters for `data_quality.json`.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| AES-GCM-256 via `cryptography` library | Industry standard authenticated encryption (detects tampering/bitflips) with fresh random 12-byte nonces. | Fernet (less explicit control) or raw unauthenticated AES-CBC (insecure). |
| Deterministic HMAC-SHA256 for phone matching | Allows $O(1)$ database index lookups and event-to-lead matching without decrypting stored phone numbers. | Decrypting every lead phone in memory during event ingestion (poor scalability). |
| Storing decimal amounts as strings in SQLite | SQLite has no native Decimal type. Storing formatted strings preserves precision and prevents float roundoff errors. | Converting to `REAL` (floats introduce IEEE 754 precision inaccuracies). |
| Linking `disposition.override` via `call_id` | Overrides contain `call_id` but no `phone`. Finding the lead through existing events sharing that `call_id` correctly resolves lead identity. | Requiring phone in override payloads (violates assignment spec). |
| Live calculation of `orphan_events` in `data_quality.json` | The spec requires orphan count to reflect the final state after drop recovery. Running `SELECT COUNT(*) FROM events WHERE lead_id IS NULL` guarantees exact final state. | Decrementing a running counter upon recovery (susceptible to skew). |
| `success_leads` anchored to call initiation date | Spec states "distinct leads with at least one call on that date whose effective disposition has is_success=true". Date of call is defined as the IST date of `call.initiated`. | Anchoring to disposition timestamp (leads called late in day might resolve on next date). |
| Tracking event files in `batches` table for drop idempotency | Running `ingest data/drop_1` twice should change nothing in `data_quality.json`. Tracking processed files in `batches` (`workflow_id='_events'`) prevents double-counting `events_total`. | Creating a separate table or relying solely on `event_id` unique constraint (would inflate duplicate event drop counts on re-runs). |
 
## Failure modes considered

- **Dirty / Malformed Inputs:**
  Invalid JSON or lines missing `event_id` are caught, increment `malformed_events`, and are skipped without crashing the batch.
- **Re-runs & Idempotency:**
  Re-ingesting an already-ingested drop checks `batches` (for both lead and event files), skipping re-processing and keeping counters intact.
- **Missing Required Fields vs. Optional Invalids:**
  Required field failures reject the row and record to `rejections.csv`. Optional field errors set the value to `NULL` and increment `field_warnings`.
- **Out-of-Order Delivery (Orphans):**
  Events arriving before their lead are safely quarantined with `lead_id = NULL`. When the lead batch is ingested, `recover_orphans()` retroactively links them.
- **Disposition Overrides:**
  Multiple overrides and out-of-order dispositions are resolved by picking the latest `disposition.override` by `occurred_at_utc`. If none, latest `disposition.set`.
- **Database Crashes:**
  Transactions (`conn.commit()`) ensure atomic batch updates; SQLite WAL mode provides durability.

## Testing

- **What was tested:**
  - Phone validation: all 6 valid Indian formats, 6 invalid formats (landline, US, 9-digit, bad prefix).
  - Date validation: all 4 accepted formats, leap years, impossible dates (31/02).
  - Currency validation: ₹, Rs, Rs., INR prefixes, comma formatting, negative rejection.
  - Schema alias mapping: varied casing, spaces, underscores, differing column orders, unknown columns.
  - Rejections & Warnings: row numbering, first-failure precedence, rejection reason formatting.
  - Duplicate merging: last-write-wins on non-null fields, permanent `original_batch_id`, default application.
  - Event ingestion: deduplication across drops, orphan recovery, override linking via `call_id`, unknown disposition handling.
  - Reporting: exact column headers, IST date/time formatting, connectivity rate (4 decimal places), bucket precedence, sorting.
  - Security: AES-GCM round-trip, nonce randomness, HMAC determinism, phone masking (`+91******XXXX`).
  - Idempotency: repeated drop ingestion, sequential drop ingestion.
- **What was not tested:**
  - Multi-process concurrent writers (SQLite single-writer architecture is sufficient for sequential batch ingest).

## Results

- **Small Scale (1,000 leads/workflow, ~9,000 event lines):**
  - Ingestion (Drop 1 + Drop 2): ~4.7s
  - Report Generation: ~0.8s
  - Total Runtime: ~5.5s
- **Observations in the data:**
  - Cross-drop duplicates occur in ~3% of generated events as expected.
  - Overrides occur up to 72 hours after call completion, requiring timeline-based disposition resolution.
  - Lead batches from later drops (`batch_003`) test alias column headers in altered column orders.

## If I had another day

1. **SQL-based Report Aggregation (Stretch Goal):**
   Push bucket and daily overview aggregations entirely into SQLite window functions and CTEs for increased throughput.
2. **Chunked Streaming Ingestion:**
   Stream JSONL lines in batches of 1,000 within a single transaction using `executemany` to scale to millions of events per drop.
3. **Database Migration Pipeline:**
   Provide a PostgreSQL backend switch via SQLAlchemy/psycopg3 for enterprise multi-tenant deployments.
