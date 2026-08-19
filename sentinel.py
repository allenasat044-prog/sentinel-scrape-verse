#!/usr/bin/env python3
"""
Sentinel — a self-healing watcher for a Bright Data Scraper Studio scraper.

What it does
------------
1. Runs your scraper via the `bdata` CLI.
2. Validates the structured output against a schema you define
   (which fields are required, and what type they should be).
3. If the output looks unhealthy (fields missing/empty/wrong type above
   a threshold), it diagnoses which fields broke and asks Bright Data's
   AI self-healing flow (`bdata scraper heal`) to fix the scraper.
4. Approves the fix, re-runs, and confirms health is restored.
5. Logs every step (run / diagnose / heal / approve / verify) to
   healing_log.json so you can show a live "it broke, it fixed itself"
   timeline in your demo.

This wraps the Bright Data CLI (`bdata` / `npx -p @brightdata/cli bdata`)
rather than reimplementing scraping or healing — the CLI already exposes
`scraper create`, `scraper run`, `scraper heal`, and `scraper approve`.
Sentinel's job is to notice *when* healing is needed and drive that loop
automatically instead of a human doing it by hand.

Usage
-----
    # one health check + heal-if-needed cycle
    python sentinel.py --collector-id c_xxx --url https://example.com/page \
        --schema schema.example.json --once

    # keep watching every 10 minutes
    python sentinel.py --collector-id c_xxx --url https://example.com/page \
        --schema schema.example.json --interval 600

Requires the Bright Data CLI to be available as `bdata` on PATH
(or set BDATA_CMD to something like "npx -p @brightdata/cli bdata").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BDATA_CMD = os.environ.get("BDATA_CMD", "bdata")
DEFAULT_LOG_PATH = Path("healing_log.json")
DEFAULT_HEALTH_THRESHOLD = 0.85  # below this fraction of "healthy" fields, trigger heal
CLI_TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class FieldSpec:
    name: str
    type: str = "string"          # "string" | "number" | "boolean"
    required: bool = True


@dataclass
class HealthReport:
    total_records: int
    healthy_fraction: float
    broken_fields: list[str]
    field_failure_rates: dict[str, float]

    @property
    def is_healthy(self) -> bool:
        return self.healthy_fraction >= DEFAULT_HEALTH_THRESHOLD


@dataclass
class Event:
    id: str
    timestamp: str
    kind: str                      # "run" | "diagnose" | "heal" | "approve" | "verify" | "error"
    success: bool
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# CLI wrapper
# --------------------------------------------------------------------------

def build_fake_broken_output(schema: list[FieldSpec], record_count: int = 5) -> list[dict[str, Any]]:
    """
    Produce fake records that fail validation on purpose — used by
    --simulate-break so a demo doesn't depend on the real site actually
    changing layout. Required fields are dropped/emptied; other fields
    are left populated so it still looks like a real (partial) response.
    """
    records = []
    for i in range(record_count):
        record: dict[str, Any] = {}
        for f in schema:
            if f.required:
                continue  # drop required fields to force a health failure
            record[f.name] = f"placeholder-{i}"
        records.append(record)
    return records


def run_cli(args: list[str], output_file: Path) -> dict[str, Any]:
    """
    Run a `bdata` subcommand that writes JSON to -o <output_file>,
    then read and return that JSON. Raises RuntimeError with a clear
    message on any failure (non-zero exit, timeout, bad JSON).
    """
    cmd = [BDATA_CMD, *args, "-o", str(output_file)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Could not find '{BDATA_CMD}'. Install the Bright Data CLI or "
            f"set BDATA_CMD=\"npx -p @brightdata/cli bdata\"."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {CLI_TIMEOUT_SECONDS}s: {' '.join(cmd)}") from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )

    if not output_file.exists():
        raise RuntimeError(f"Command reported success but {output_file} was not written: {' '.join(cmd)}")

    try:
        return json.loads(output_file.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Output file {output_file} was not valid JSON: {exc}") from exc


def scraper_run(collector_id: str, url: str, workdir: Path) -> dict[str, Any]:
    return run_cli(["scraper", "run", collector_id, "--urls", url], workdir / "run_output.json")


def scraper_heal(collector_id: str, diagnosis: str, url: str, workdir: Path) -> dict[str, Any]:
    return run_cli(
        ["scraper", "heal", collector_id, diagnosis, "--url", url],
        workdir / "heal_output.json",
    )


def scraper_approve(collector_id: str, workdir: Path) -> dict[str, Any]:
    return run_cli(["scraper", "approve", collector_id], workdir / "approve_output.json")


# --------------------------------------------------------------------------
# Validation / diagnosis
# --------------------------------------------------------------------------

def load_schema(schema_path: Path) -> list[FieldSpec]:
    raw = json.loads(schema_path.read_text())
    fields_raw = raw["fields"] if isinstance(raw, dict) else raw
    return [FieldSpec(**f) for f in fields_raw]


def _records_from_output(output: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Bright Data collector output shapes vary by scraper. Handle the common
    cases: a bare list, or an envelope with a "data"/"results" key.
    """
    if isinstance(output, list):
        return output
    for key in ("data", "results", "records"):
        if isinstance(output.get(key), list):
            return output[key]
    # Single-record object fallback
    return [output]


def _get_nested(record: dict[str, Any], dotted_name: str) -> Any:
    """
    Look up a possibly-nested field, e.g. "price.value" -> record["price"]["value"].
    Returns None if any part of the path is missing or not a dict.
    """
    value: Any = record
    for part in dotted_name.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _value_matches_type(value: Any, expected_type: str) -> bool:
    if value is None:
        return False
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    # default: string-like, but reject empty strings
    return isinstance(value, str) and value.strip() != ""


def validate(output: dict[str, Any], schema: list[FieldSpec]) -> HealthReport:
    records = _records_from_output(output)
    total = len(records)

    if total == 0:
        # Zero records is itself a failure signal — every required field "fails".
        broken = [f.name for f in schema if f.required]
        rates = {f.name: 1.0 for f in schema}
        return HealthReport(total_records=0, healthy_fraction=0.0,
                             broken_fields=broken, field_failure_rates=rates)

    failure_counts: dict[str, int] = {f.name: 0 for f in schema}
    for record in records:
        for f in schema:
            value = _get_nested(record, f.name) if isinstance(record, dict) else None
            if f.required and not _value_matches_type(value, f.type):
                failure_counts[f.name] += 1

    field_failure_rates = {name: count / total for name, count in failure_counts.items()}
    broken_fields = [name for name, rate in field_failure_rates.items() if rate > (1 - DEFAULT_HEALTH_THRESHOLD)]

    required_fields = [f.name for f in schema if f.required] or [f.name for f in schema]
    avg_failure_rate = (
        sum(field_failure_rates[name] for name in required_fields) / len(required_fields)
        if required_fields else 0.0
    )
    healthy_fraction = 1.0 - avg_failure_rate

    return HealthReport(
        total_records=total,
        healthy_fraction=round(healthy_fraction, 4),
        broken_fields=broken_fields,
        field_failure_rates={k: round(v, 4) for k, v in field_failure_rates.items()},
    )


def build_diagnosis_prompt(report: HealthReport) -> str:
    """
    Turn a HealthReport into the natural-language prompt `bdata scraper heal`
    expects — it wants a description of what looks broken, not raw numbers.
    """
    if report.total_records == 0:
        return (
            "The scraper is returning zero records. The page structure has "
            "likely changed enough that the top-level item selector no "
            "longer matches anything. Please re-inspect the page and fix "
            "the extraction template."
        )

    field_list = ", ".join(
        f"'{name}' (missing/empty in {rate:.0%} of records)"
        for name, rate in sorted(report.field_failure_rates.items(), key=lambda kv: -kv[1])
        if name in report.broken_fields
    )
    return (
        f"After a recent run, these fields are failing to extract: {field_list}. "
        f"This is likely caused by a layout/selector change on the target page. "
        f"Please locate the current elements holding this data and update the "
        f"extraction template accordingly."
    )


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------

def append_event(log_path: Path, kind: str, success: bool, detail: dict[str, Any]) -> Event:
    event = Event(
        id=str(uuid.uuid4())[:8],
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        kind=kind,
        success=success,
        detail=detail,
    )
    history = []
    if log_path.exists():
        try:
            history = json.loads(log_path.read_text())
        except json.JSONDecodeError:
            history = []
    history.append(asdict(event))
    log_path.write_text(json.dumps(history, indent=2))
    return event


# --------------------------------------------------------------------------
# Core cycle
# --------------------------------------------------------------------------

def run_cycle(collector_id: str, url: str, schema: list[FieldSpec],
              workdir: Path, log_path: Path, auto_approve: bool,
              simulate_break: bool = False) -> HealthReport:
    """
    One full check: run -> validate -> (if unhealthy) diagnose -> heal ->
    approve -> re-run -> re-validate. Returns the final HealthReport.

    If simulate_break is True, the initial run is replaced with synthetic
    broken data (see build_fake_broken_output) so you can demo the full
    diagnose/heal/approve/verify pipeline on demand, without needing the
    real target site to actually break during a recording.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running scraper {collector_id}...")
    if simulate_break:
        print("  (simulate-break mode: using synthetic broken data, not a real scraper run)")
        output = build_fake_broken_output(schema)
    else:
        try:
            output = scraper_run(collector_id, url, workdir)
        except RuntimeError as exc:
            append_event(log_path, "run", success=False, detail={"error": str(exc)})
            print(f"  ✗ Run failed: {exc}", file=sys.stderr)
            raise

    report = validate(output, schema)
    append_event(log_path, "run", success=True, detail={
        "total_records": report.total_records,
        "healthy_fraction": report.healthy_fraction,
        "broken_fields": report.broken_fields,
    })
    print(f"  Health: {report.healthy_fraction:.0%} "
          f"({report.total_records} records, broken fields: {report.broken_fields or 'none'})")

    if report.is_healthy:
        print("  ✓ Healthy. No action needed.")
        return report

    # --- Unhealthy: diagnose and attempt to heal ---
    diagnosis = build_diagnosis_prompt(report)
    append_event(log_path, "diagnose", success=True, detail={"prompt": diagnosis})
    print(f"  ⚠ Unhealthy. Diagnosis: {diagnosis}")

    try:
        heal_result = scraper_heal(collector_id, diagnosis, url, workdir)
        append_event(log_path, "heal", success=True, detail={"result_summary": str(heal_result)[:500]})
        print("  → Heal triggered.")
    except RuntimeError as exc:
        append_event(log_path, "heal", success=False, detail={"error": str(exc)})
        print(f"  ✗ Heal failed: {exc}", file=sys.stderr)
        return report

    if not auto_approve:
        print("  Heal produced a proposed fix. Run with --auto-approve to "
              "commit it automatically, or review it in the Bright Data "
              "dashboard and approve manually.")
        return report

    try:
        scraper_approve(collector_id, workdir)
        append_event(log_path, "approve", success=True, detail={})
        print("  ✓ Fix approved.")
    except RuntimeError as exc:
        append_event(log_path, "approve", success=False, detail={"error": str(exc)})
        print(f"  ✗ Approve failed: {exc}", file=sys.stderr)
        return report

    # --- Verify the fix worked ---
    print("  Re-running to verify the fix...")
    try:
        output_after = scraper_run(collector_id, url, workdir)
    except RuntimeError as exc:
        append_event(log_path, "verify", success=False, detail={"error": str(exc)})
        print(f"  ✗ Verification run failed: {exc}", file=sys.stderr)
        return report

    report_after = validate(output_after, schema)
    append_event(log_path, "verify", success=report_after.is_healthy, detail={
        "total_records": report_after.total_records,
        "healthy_fraction": report_after.healthy_fraction,
        "broken_fields": report_after.broken_fields,
    })
    if report_after.is_healthy:
        print(f"  ✓ Healed. Health restored to {report_after.healthy_fraction:.0%}.")
    else:
        print(f"  ✗ Still unhealthy after heal ({report_after.healthy_fraction:.0%}). "
              f"May need a manual look in Scraper Studio.")

    return report_after


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Self-healing watcher for a Bright Data scraper.")
    parser.add_argument("--collector-id", required=True, help="Scraper Studio collector_id")
    parser.add_argument("--url", required=True, help="Target page URL (used for heal context)")
    parser.add_argument("--schema", required=True, type=Path, help="Path to schema JSON (see schema.example.json)")
    parser.add_argument("--workdir", type=Path, default=Path("."), help="Where run/heal output files are written")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, help="Path to the healing event log")
    parser.add_argument("--interval", type=int, default=0,
                         help="Seconds between checks. Omit or 0 with --once for a single run.")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit")
    parser.add_argument("--auto-approve", action="store_true",
                         help="Automatically approve AI-proposed fixes (heal is human-in-the-loop by default)")
    parser.add_argument("--simulate-break", action="store_true",
                         help="Use synthetic broken data instead of a real scraper run, to safely "
                              "demo the diagnose/heal/approve/verify pipeline on demand")
    args = parser.parse_args()

    args.workdir.mkdir(parents=True, exist_ok=True)
    schema = load_schema(args.schema)

    if args.once or args.interval <= 0:
        run_cycle(args.collector_id, args.url, schema, args.workdir, args.log, args.auto_approve, args.simulate_break)
        return

    print(f"Watching every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            run_cycle(args.collector_id, args.url, schema, args.workdir, args.log, args.auto_approve, args.simulate_break)
        except RuntimeError:
            pass  # already logged; keep watching
        time.sleep(args.interval)


if __name__ == "__main__":
    main()