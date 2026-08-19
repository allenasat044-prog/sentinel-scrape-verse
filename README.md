# 🕷️ Sentinel

**A self-healing wrapper around a Bright Data Scraper Studio collector — built for [Into the Scrape-Verse](https://www.wemakedevs.org).**

Sentinel doesn't scrape. It watches something that does, notices when the target site's layout has drifted enough to break extraction, and drives Bright Data's AI self-healing flow to fix it — without a human running `bdata scraper heal` by hand. On top of that, **Tracker** turns the validated output into something real: a running price/stock history for a live product catalog.

---

## Table of contents

- [The problem](#the-problem)
- [Architecture](#architecture)
- [Component deep-dive](#component-deep-dive)
  - [1. The Scraper Studio collector](#1-the-scraper-studio-collector)
  - [2. Sentinel — health validation](#2-sentinel--health-validation)
  - [3. Sentinel — diagnosis](#3-sentinel--diagnosis)
  - [4. Sentinel — the heal cycle](#4-sentinel--the-heal-cycle)
  - [5. Tracker — turning data into something real](#5-tracker--turning-data-into-something-real)
  - [6. Console — the UI layer](#6-console--the-ui-layer)
- [The self-healing loop, step by step](#the-self-healing-loop-step-by-step)
- [Demo mode: `--simulate-break`](#demo-mode---simulate-break)
- [Real edge cases hit during development](#real-edge-cases-hit-during-development)
- [File structure](#file-structure)
- [Setup & usage](#setup--usage)
- [Example structured output](#example-structured-output)
- [How this maps to the judging tracks](#how-this-maps-to-the-judging-tracks)
- [Known limitations / what's next](#known-limitations--whats-next)
- [AI-assistance disclosure](#ai-assistance-disclosure)

---

## The problem

A scraper is a bet that a page's structure won't change. That bet always eventually loses — a site redesign, a renamed CSS class, an A/B test — and the scraper doesn't crash, it just quietly starts returning `null` where a price used to be. Nobody notices until the downstream data is already wrong.

Bright Data Scraper Studio solves the *repair* half of this problem: `bdata scraper heal` can look at a collector and propose a fix when you tell it what broke. But it's a manual command — a human has to notice something's wrong first, then run it, then approve it, then confirm it worked.

Sentinel is the layer Scraper Studio doesn't provide out of the box: **the noticing, the diagnosing, and the driving of that loop, automatically.**

---

## Architecture

```
                    ┌─────────────────────────┐
                    │   Bright Data Platform    │
                    │   Scraper Studio Collector │
                    │   (c_msyndimi68dk1qu6l)    │
                    └────────────┬────────────┘
                                 │  bdata CLI
                                 │  (run / heal / approve)
                 ┌───────────────┴────────────────┐
                 │                                  │
        ┌────────▼─────────┐              ┌────────▼─────────┐
        │    sentinel.py     │              │    tracker.py      │
        │  ─────────────────  │              │  ──────────────────  │
        │  run → validate      │              │  run → diff vs.       │
        │  → diagnose → heal    │              │  last snapshot →      │
        │  → approve → verify    │              │  log price/stock       │
        │  → log                  │              │  changes                 │
        └────────┬───────────┘              └────────┬───────────┘
                 │ writes                              │ writes
                 ▼                                      ▼
        healing_log.json                    products_snapshot.json
                                             product_changes.json
                 │                                      │
                 └──────────────────┬───────────────────┘
                                    ▼
                        ┌───────────────────────┐
                        │      console.html        │
                        │  (static, reads JSON       │
                        │   over fetch, no backend)    │
                        └───────────────────────┘
```

Two independent Python scripts, both thin wrappers around the same `bdata` CLI primitives, writing separate JSON files that a single static frontend reads. No server, no database — the JSON files on disk *are* the state.

---

## Component deep-dive

### 1. The Scraper Studio collector

Built once, via:
```bash
bdata scraper create "https://www.amkette.com/pages/evofox" \
  "Extract title, price, and availability status for each product listing"
```
Bright Data's AI reads the page and generates the extraction template (selectors, pagination, field mapping) — that template lives on Bright Data's platform under the returned `collector_id` (`c_msyndimi68dk1qu6l`). Every `bdata scraper run` call re-executes that template against the live page and returns structured JSON. Neither `sentinel.py` nor `tracker.py` know or care what the selectors actually are — they only ever see the resulting JSON.

Target: Amkette's EvoFox gaming-peripherals catalog — a regional e-commerce page, not one of Bright Data's 800+ pre-built scrapers, and fully public (no login, no paywall).

### 2. Sentinel — health validation

`sentinel.py` defines an expected shape via `schema.amkette.json`:
```json
{ "fields": [
  { "name": "title", "type": "string", "required": true },
  { "name": "price.value", "type": "number", "required": true },
  { "name": "availability", "type": "string", "required": true },
  { "name": "product_page_url", "type": "string", "required": false }
]}
```
Note `price.value` — the real scraper output nests price as `{ "value": 2599, "currency": "INR", "symbol": "₹" }`, not a flat number. `_get_nested()` walks dotted field names through nested dicts, so validation can check inside that object without the schema (or Sentinel) needing to know the object's other keys.

For every record, every required field is checked against its expected type (`_value_matches_type`) — an empty string, a `null`, a wrong-typed value, or a missing key all count as a failure for that field on that record. Failures are aggregated per-field across all records into `field_failure_rates`, and the overall `healthy_fraction` is `1 - average(failure_rate across required fields)`. A run with **zero records** is treated as a total failure (every required field scored as 100% failed) rather than a crash or a silent pass — an empty page is itself a health signal.

`DEFAULT_HEALTH_THRESHOLD = 0.85` — below 85% healthy, Sentinel considers the scraper broken and moves to diagnosis.

### 3. Sentinel — diagnosis

`build_diagnosis_prompt()` turns a `HealthReport` into the natural-language sentence `bdata scraper heal` actually expects (it wants a description of what looks broken, not a JSON blob of failure rates). Two cases:
- **Zero records** → a generic "the top-level item selector likely no longer matches anything" prompt.
- **Specific fields broken** → names each broken field and its failure rate, e.g. *"'price.value' (missing/empty in 100% of records)"*, sorted worst-first, so the AI healer gets a targeted description instead of a vague one.

### 4. Sentinel — the heal cycle

`run_cycle()` is the actual state machine:

1. **Run** → `bdata scraper run <id> --urls <url>`, parse output, validate.
2. If healthy → log and stop.
3. If unhealthy → **diagnose** (build the prompt above), log it.
4. **Heal** → `bdata scraper heal <id> "<diagnosis>" --url <url>`. This is a *real* AI call to Bright Data's platform.
5. If `--auto-approve` wasn't passed → stop here and tell the human to review (heal is human-in-the-loop by default in the CLI itself; Sentinel just respects that).
6. **Approve** → `bdata scraper approve <id>` commits the fix.
7. **Re-run and re-validate** to confirm the fix actually worked — this is the "verify" step, and it's what actually closes the loop. A heal that gets approved but doesn't fix anything still gets caught here and logged as `verify (failed)`.

Every one of these seven steps writes an `Event` to `healing_log.json` (`run` / `diagnose` / `heal` / `approve` / `verify`, each with a `success` flag and a `detail` payload) — regardless of whether it succeeded or failed. That log is append-only and is the entire source of truth for the dashboard's timeline.

### 5. Tracker — turning data into something real

`tracker.py` reuses Sentinel's own `scraper_run()` (same CLI wrapper, no duplicated logic) but does something different with the output: instead of validating shape, it **diffs** each run against the last saved snapshot, keyed by `product_page_url` (the one field guaranteed unique per product in this dataset — title alone isn't safe).

For every product in the new run:
- Not in the previous snapshot → `new_product` event
- `price.value` differs from last time → `price_change` event, with the signed delta
- `availability` differs from last time (`"Sold out"` ↔ `"Add to cart"`) → `stock_change` event

Every change is appended to `product_changes.json` (append-only history); the full current state overwrites `products_snapshot.json` (latest-known-state, used as the diff baseline for the *next* run). This is the piece that answers "what did the data go on to power" — a live restock/price-drop feed, not just a validated blob.

### 6. Console — the UI layer

`console.html` is a single static file — sidebar nav (Overview / Healing Log / Products / Settings), no backend, no build step. It `fetch()`es `healing_log.json`, `products_snapshot.json`, and `product_changes.json` directly off disk (served via `python3 -m http.server`) and re-renders every 3 seconds. Every number on screen — the health ring, the sparkline, the terminal log lines, the product cards — is computed from those real JSON files at render time; nothing is hardcoded or simulated. The "Re-check now" button doesn't fake a scraping animation — it force-refetches the same JSON with a real loading state.

---

## The self-healing loop, step by step

This is the sequence a demo (or `--simulate-break`, see below) actually walks through, end to end, with real output from a test run:

```
[11:12:25] Running scraper c_msyndimi68dk1qu6l...
  Health: 0% (5 records, broken fields: ['title', 'price.value', 'availability'])
  ⚠ Unhealthy. Diagnosis: After a recent run, these fields are failing to
    extract: 'title' (missing/empty in 100% of records), 'price.value'
    (missing/empty in 100% of records), 'availability' (missing/empty in
    100% of records). This is likely caused by a layout/selector change on
    the target page. Please locate the current elements holding this data
    and update the extraction template accordingly.
  → Heal triggered.
  ✓ Fix approved.
  Re-running to verify the fix...
  ✓ Healed. Health restored to 100%.
```

Every line above is a real event, written to `healing_log.json`, and visible in the console's Healing Log tab and Overview sparkline.

---

## Demo mode: `--simulate-break`

Real layout drift doesn't happen on a schedule convenient for a demo recording. `--simulate-break` solves that honestly, not by faking the whole pipeline:

- **Only the first `run` is synthetic** — `build_fake_broken_output()` generates records with required fields stripped out, so the health check fails on demand.
- **Everything after that is real** — `diagnose`, `bdata scraper heal`, `bdata scraper approve`, and the verification re-run all hit the actual Bright Data API against the actual collector. Since the real site is healthy, verify reliably comes back at 100%, giving a clean, repeatable "broke → healed → verified" arc without ever risking the live scraper.

---

## Real edge cases hit during development

These aren't hypothetical — they happened while building this, and Sentinel's error handling is shaped by them:

- **`scraper run` requires `--urls` explicitly** — the CLI doesn't infer it from the collector's config. Missing this caused every early run to fail with a clear CLI error, which Sentinel now passes correctly and would otherwise have caught and logged rather than crashing.
- **Nested `price` object** — the real scraper output didn't match the flat schema originally assumed. Fixed with dot-path field lookup rather than hardcoding a `price` special case, so the same mechanism works for any future nested field.
- **`409: Another refactor job is still in progress`** — an interrupted (Ctrl+C'd) `heal` call left a fix "awaiting approval" server-side, which then blocked all subsequent heal attempts on that collector. Diagnosed via `bdata scraper --help` (found the `approve --reject` option) and resolved with `bdata scraper approve <id> --reject`. Sentinel's `run_cli()` wrapper catches CLI failures like this and logs them as failed events (with the CLI's actual stderr) instead of crashing silently — this incident is direct proof that behavior works, not just a theoretical safeguard.

---

## File structure

| File | Purpose |
|---|---|
| `sentinel.py` | Core watcher: run → validate → diagnose → heal → approve → verify → log |
| `tracker.py` | Diffs each run against the last snapshot; logs price/stock changes |
| `schema.amkette.json` | Expected fields/types for validation, including nested `price.value` |
| `console.html` | Unified dashboard — health ring, sparkline, live healing log, product tracker |
| `dashboard.html` / `products.html` | Earlier standalone views (kept for reference) |
| `index.html` | Landing page linking to all views |
| `healing_log.json` | Runtime — append-only healing event history (written by `sentinel.py`) |
| `products_snapshot.json` | Runtime — latest known state per product (written by `tracker.py`) |
| `product_changes.json` | Runtime — append-only price/stock change log (written by `tracker.py`) |

---

## Setup & usage

```bash
# 1. Install/auth the Bright Data CLI
npx -p @brightdata/cli bdata login

# 2. (Already done for this project — collector_id is live)
bdata scraper create "https://www.amkette.com/pages/evofox" \
  "Extract title, price, and availability status for each product listing"

# 3. Run a health check + heal-if-needed cycle
python3 sentinel.py \
  --collector-id c_msyndimi68dk1qu6l \
  --url "https://www.amkette.com/pages/evofox" \
  --schema schema.amkette.json \
  --once --auto-approve

# 4. Track price/stock changes on the same collector
python3 tracker.py \
  --collector-id c_msyndimi68dk1qu6l \
  --url "https://www.amkette.com/pages/evofox"

# 5. View it
python3 -m http.server 8000
# open http://localhost:8000
```

Continuous watching (instead of `--once`):
```bash
python3 sentinel.py --collector-id c_msyndimi68dk1qu6l \
  --url "https://www.amkette.com/pages/evofox" \
  --schema schema.amkette.json --interval 600 --auto-approve
```

Demo mode:
```bash
python3 sentinel.py --collector-id c_msyndimi68dk1qu6l \
  --url "https://www.amkette.com/pages/evofox" \
  --schema schema.amkette.json --once --auto-approve --simulate-break
```

---

## Example structured output

One real record from a live run:
```json
{
  "title": "EvoFox Elite X2 Pro Tri Mode Wireless Gamepad",
  "price": { "value": 2599, "currency": "INR", "symbol": "₹" },
  "availability": "Add to cart",
  "product_page_url": "https://www.amkette.com/products/evofox-elite-x2-pro-tri-mode-wireless-gamepad"
}
```

---

## How this maps to the judging tracks

- **Web-Slinger (Best Use of Bright Data)** — custom collector built in Scraper Studio, driven entirely from a coding agent via the CLI, real `heal`/`approve` calls (not mocked), and structured output feeding a second real feature (Tracker).
- **Suit-Up (Best UI)** — `console.html`: real charts, real terminal-style logs, glassmorphism, all backed by live data, no hardcoded/simulated content.
- **Spider-Sense (Best Clean Code)** — dataclasses for structured state (`FieldSpec`, `HealthReport`, `Event`), explicit edge-case handling (zero records, CLI timeouts/failures, nested fields), and every failure mode logged rather than swallowed.

---

## Known limitations / what's next

- Sentinel and Tracker are triggered manually or via `--interval`, but haven't been wired into a scheduler (cron / GitHub Actions) for this submission — `--interval` makes that straightforward as a next step.
- `--simulate-break` proves the heal loop reliably, but the project hasn't (yet) captured a genuine, organic site-layout change in the wild — by nature, hard to schedule around a submission deadline.

---

## AI-assistance disclosure

`sentinel.py`, `tracker.py`, `console.html`, `dashboard.html`, `products.html`, `index.html`, and this README were built with Claude (Anthropic) acting as a coding agent, based on the Bright Data CLI's documented commands (`scraper create/run/heal/approve`). Claude scaffolded the initial watcher, fixed real bugs found while running against the live CLI (a missing `--urls` flag, schema validation that didn't support the real nested `price` object), added the `--simulate-break` demo mode, built the Tracker feature, and did the visual design pass. The scraper's target site, schema, collector ID, and all debugging decisions — including diagnosing and resolving a real 409 stuck-job error during testing — were driven by the project author, who has read through `sentinel.py` and can explain its validation and diagnosis logic.
