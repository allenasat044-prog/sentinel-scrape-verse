#!/usr/bin/env python3
"""
Tracker — turns the raw scraped catalog into something real: a running
price/stock history for every product, and a log of what changed between
runs (price drops/rises, restocks, sell-outs).

This is the piece Sentinel doesn't do on its own — Sentinel just makes
sure the data is *healthy*. Tracker is what you actually build on top of
healthy data: it's what "products.html" reads to show a live catalog with
change badges.

What it does
------------
1. Runs the scraper via the same `bdata` CLI wrapper Sentinel uses.
2. Loads the last saved snapshot (products_snapshot.json), keyed by each
   product's page URL (the one stable identifier in this dataset).
3. Compares old vs new for every product currently returned:
     - price changed -> a "price_change" event (records old, new, delta)
     - availability changed -> a "stock_change" event (e.g. Sold out ->
       Add to cart, i.e. a restock)
     - a brand-new product URL not seen before -> a "new_product" event
4. Appends every change to product_changes.json (append-only history).
5. Overwrites products_snapshot.json with the latest full state, so the
   next run has something to diff against.

Usage
-----
    python3 tracker.py --collector-id c_xxx --url https://example.com/page

Requires the Bright Data CLI (`bdata`) on PATH, same as sentinel.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse Sentinel's CLI wrapper and output-shape handling instead of
# duplicating it.
from sentinel import scraper_run, _records_from_output

DEFAULT_SNAPSHOT_PATH = Path("products_snapshot.json")
DEFAULT_CHANGES_PATH = Path("product_changes.json")


def _price_value(record: dict[str, Any]) -> float | None:
    price = record.get("price")
    if isinstance(price, dict):
        return price.get("value")
    if isinstance(price, (int, float)):
        return price
    return None


def _key_for(record: dict[str, Any]) -> str | None:
    # product_page_url is the one field guaranteed unique per product in
    # this dataset — title alone isn't safe (could theoretically collide
    # or get truncated during a partial scrape).
    return record.get("product_page_url") or record.get("title")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def diff_and_log(records: list[dict[str, Any]], snapshot_path: Path,
                  changes_path: Path) -> list[dict[str, Any]]:
    previous: dict[str, Any] = load_json(snapshot_path, {})
    changes: list[dict[str, Any]] = load_json(changes_path, [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    current: dict[str, Any] = {}
    for record in records:
        key = _key_for(record)
        if not key:
            continue  # can't track a product with no stable identifier

        title = record.get("title", "(unknown)")
        price = _price_value(record)
        availability = record.get("availability")

        current[key] = {
            "title": title,
            "price": price,
            "availability": availability,
            "product_page_url": record.get("product_page_url"),
            "last_seen": now,
        }

        prev = previous.get(key)
        if prev is None:
            changes.append({
                "timestamp": now, "kind": "new_product", "product_page_url": key,
                "title": title, "price": price, "availability": availability,
            })
            continue

        if price is not None and prev.get("price") is not None and price != prev["price"]:
            changes.append({
                "timestamp": now, "kind": "price_change", "product_page_url": key,
                "title": title, "old_price": prev["price"], "new_price": price,
                "delta": round(price - prev["price"], 2),
            })

        if availability is not None and availability != prev.get("availability"):
            changes.append({
                "timestamp": now, "kind": "stock_change", "product_page_url": key,
                "title": title, "old_status": prev.get("availability"),
                "new_status": availability,
            })

    snapshot_path.write_text(json.dumps(current, indent=2))
    changes_path.write_text(json.dumps(changes, indent=2))
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description="Track price/stock changes across scraper runs.")
    parser.add_argument("--collector-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workdir", type=Path, default=Path("."))
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--changes-log", type=Path, default=DEFAULT_CHANGES_PATH)
    args = parser.parse_args()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running scraper {args.collector_id}...")
    try:
        output = scraper_run(args.collector_id, args.url, args.workdir)
    except RuntimeError as exc:
        print(f"  ✗ Run failed: {exc}", file=sys.stderr)
        sys.exit(1)

    records = _records_from_output(output)
    new_changes = diff_and_log(records, args.snapshot, args.changes_log)

    this_run_changes = [c for c in new_changes if c["timestamp"] == new_changes[-1]["timestamp"]] if new_changes else []
    print(f"  Tracked {len(records)} products.")
    if this_run_changes:
        print(f"  {len(this_run_changes)} change(s) detected this run:")
        for c in this_run_changes:
            if c["kind"] == "price_change":
                print(f"    price: {c['title']}: {c['old_price']} -> {c['new_price']}")
            elif c["kind"] == "stock_change":
                print(f"    stock: {c['title']}: {c['old_status']} -> {c['new_status']}")
            elif c["kind"] == "new_product":
                print(f"    new:   {c['title']}")
    else:
        print("  No changes since last run.")


if __name__ == "__main__":
    main()
