
# Assignment A – Interaction Ledger & Reporting Pipeline

This project processes lead CSV files and event JSONL files, stores the data in SQLite, and generates reports for each workflow.

It handles data validation, duplicate records, event matching, orphan events, and secure storage of personal information.

## 1. Requirements

- Python 3.11 or later
- Git

## 2. Setup

Clone the repository and open the project directory.

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it:

**Windows (PowerShell)**
```powershell
.venv\Scripts\Activate.ps1
```

**Linux/macOS**
```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create the environment file:

```bash
copy .env.example .env
```

On Linux/macOS, use:

```bash
cp .env.example .env
```

Add a valid 32-byte Base64 encryption key to `.env`. A new key can be generated with:

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

## 3. Running the Pipeline

Generate the sample data:

```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

Ingest the first drop:

```bash
python -m solution.main ingest data/drop_1
```

Ingest the second drop:

```bash
python -m solution.main ingest data/drop_2
```

Generate the reports:

```bash
python -m solution.main report --out reports/
```

The pipeline can also be run again without processing previously ingested batches or duplicate events again.

## 4. Project Structure

```text
solution/
├── main.py
├── config.py
├── db.py
├── schema_loader.py
├── validators.py
├── security.py
├── ingest_leads.py
├── ingest_events.py
├── reports.py
├── README.md
└── tests/
```

## 5. How It Works

The pipeline has three main parts:

1. **Lead ingestion:** Reads CSV files, validates fields, handles duplicate leads, and stores personal information in encrypted form.
2. **Event ingestion:** Reads JSONL files, validates events, matches them with leads, and keeps unmatched events for later recovery.
3. **Reporting:** Uses the stored data to calculate lead statuses, call statistics, and data quality information.

SQLite is used to store the data. Events are kept in an append-only ledger so that previous interactions are preserved.

## 6. Data Handling and Security

- Personal information is encrypted using AES-GCM-256.
- Phone numbers are matched using keyed HMAC-SHA256 hashes.
- Phone numbers are masked in generated reports.
- Rejected records are stored without exposing personal information.
- Timestamps are stored in UTC and reports use Indian Standard Time (IST).
- Currency values are handled using Python's `Decimal` to avoid floating-point precision issues.

## 7. Generated Reports

Running the reporting command generates five files:

| File | Description |
|---|---|
| `batch_summary.csv` | Lead counts grouped by batch and status |
| `daily_overview.csv` | Daily call and connectivity statistics |
| `lead_report.csv` | Lead details, call counts, and current status |
| `rejections.csv` | Records rejected during ingestion |
| `data_quality.json` | Data quality counters and ingestion statistics |

## 8. Running Tests

Run the test suite from the repository root:

```bash
pytest
```

The test suite covers validation, encryption, lead ingestion, event ingestion, reporting, and idempotency.

