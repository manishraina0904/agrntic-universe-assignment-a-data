"""
main.py — CLI entry point for the Interaction Ledger pipeline.

Usage:
  python -m solution.main ingest <drop_path>
  python -m solution.main report --out <output_dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import DB_PATH
from solution.db import get_connection, init_db
from solution.ingest_events import ingest_jsonl_drop, recover_orphans
from solution.ingest_leads import ingest_csv_batch
from solution.reports import generate_all_reports


def cmd_ingest(args: argparse.Namespace) -> None:
    """
    Ingest a drop directory:
      1. Ingest all lead CSV files (sorted by filename, per workflow).
      2. Ingest all event JSONL files.
      3. Attempt orphan recovery.
    """
    drop_path = Path(args.drop_path)
    if not drop_path.exists():
        print(f"Error: drop path does not exist: {drop_path}", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    init_db(conn)

    # ── Ingest lead CSVs ──────────────────────────────────────────────────
    leads_dir = drop_path / "leads"
    if leads_dir.exists():
        # Process workflow directories in sorted order
        for wf_dir in sorted(leads_dir.iterdir()):
            if not wf_dir.is_dir():
                continue
            workflow_id = wf_dir.name

            # Process CSV files in filename order
            for csv_file in sorted(wf_dir.glob("*.csv")):
                batch_id = csv_file.stem  # e.g. "batch_001"
                print(f"  Ingesting leads: {workflow_id}/{batch_id}")
                ingest_csv_batch(conn, workflow_id, batch_id, csv_file)

    # ── Ingest event JSONLs ───────────────────────────────────────────────
    events_dir = drop_path / "events"
    if events_dir.exists():
        print(f"  Ingesting events from {events_dir}")
        ingest_jsonl_drop(conn, drop_path)

    # Always run orphan recovery to link any orphans against newly ingested leads
    recover_orphans(conn)
    conn.commit()

    conn.close()
    print(f"Ingest complete: {drop_path}")


def cmd_report(args: argparse.Namespace) -> None:
    """Generate all five report files into the specified output directory."""
    out_dir = Path(args.out)

    conn = get_connection()
    init_db(conn)

    print(f"Generating reports → {out_dir}")
    generate_all_reports(conn, out_dir)

    conn.close()
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m solution.main",
        description="Interaction Ledger & Reporting Pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest subcommand
    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest a drop directory (leads + events)"
    )
    ingest_parser.add_argument("drop_path", help="Path to the drop directory, e.g. data/drop_1")

    # report subcommand
    report_parser = subparsers.add_parser(
        "report", help="Generate all five report files"
    )
    report_parser.add_argument(
        "--out", required=True, help="Output directory, e.g. reports/"
    )

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()

