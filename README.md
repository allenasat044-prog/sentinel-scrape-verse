# Sentinel — a self-healing wrapper around a Bright Data Scraper Studio scraper

Built for **Into the Scrape-Verse**. Sentinel watches a Scraper Studio
collector, checks whether its output still matches an expected schema, and —
when fields start coming back empty (a layout/selector drift) — automatically
triggers Bright Data's AI self-healing flow, approves the fix, and verifies
the scraper recovered. Every step is logged, so you can see the whole
"broke → diagnosed → healed → verified" cycle live.

On top of that, **Tracker** turns the validated data into something real: a
running price/stock history for every product, with a change feed showing
restocks and price drops — because healthy scraped data is only useful if
it powers something.

Open `index.html` for a landing page linking to both views, or go directly to:
- `dashboard.html` — Sentinel's health/heal timeline
- `products.html` — the live product catalog + change feed

## Target site

Custom collector built against **Amkette's EvoFox gaming-gear catalog**
(`https://www.amkette.com/pages/evofox`) — a public, unauthenticated product
listing page. No login, paywall, or private data involved (per rule 6).

## How it uses Bright Data Scraper Studio

- The scraper itself (selectors, pagination, fields) is a **custom collector
  built in Scraper Studio**, created via `bdata scraper create` from a
  natural-language description ("Extract title, price, and availability
  status for each product listing"). This is not a library scraper — it was
  generated specifically for this target page (rule 5).
- Sentinel drives it entirely through the **Bright Data CLI**:
  `scraper run` (execute), `scraper heal` (AI-proposed fix when drift is
  detected), `scraper approve` (commit the fix, or `--reject` to discard it).
- Sentinel does not reimplement scraping or healing — Bright Data's platform
  already does that. Sentinel's job is the layer the platform doesn't
  provide out of the box: **noticing when healing is needed and driving that
  loop automatically instead of a human running `heal` by hand.**

## Example structured output

One record from a real run against the live collector:

```json
{
  "title": "EvoFox Elite X2 Pro Tri Mode Wireless Gamepad",
  "price": { "value": 2599, "currency": "INR", "symbol": "₹" },
  "availability": "Add to cart",
  "product_page_url": "https://www.amkette.com/products/evofox-elite-x2-pro-tri-mode-wireless-gamepad"
}
```

`schema.amkette.json` validates against this exact shape, including the
nested `price.value` field (Sentinel's validator supports dot-path lookups
for nested fields, not just flat ones).

## Setup

1. Install/auth the Bright Data CLI:
   ```bash
   npx -p @brightdata/cli bdata login
   # or: npm i -g @brightdata/cli && bdata login
   ```

2. Create your scraper (one-time, already done for this project — the
   collector_id below is live):
   ```bash
   bdata scraper create "https://www.amkette.com/pages/evofox" \
     "Extract title, price, and availability status for each product listing"
   ```
   Collector ID: `c_msyndimi68dk1qu6l`

3. `schema.amkette.json` is already matched to this collector's real output
   shape — no editing needed unless you point Sentinel at a different site.

4. Run a single health check:
   ```bash
   python3 sentinel.py \
     --collector-id c_msyndimi68dk1qu6l \
     --url "https://www.amkette.com/pages/evofox" \
     --schema schema.amkette.json \
     --once --auto-approve
   ```

5. Or watch continuously (e.g. every 10 minutes):
   ```bash
   python3 sentinel.py \
     --collector-id c_msyndimi68dk1qu6l \
     --url "https://www.amkette.com/pages/evofox" \
     --schema schema.amkette.json \
     --interval 600 --auto-approve
   ```

6. Track price/stock changes on top of the same collector:
   ```bash
   python3 tracker.py \
     --collector-id c_msyndimi68dk1qu6l \
     --url "https://www.amkette.com/pages/evofox"
   ```
   Run this again later (or after the real catalog changes) to start seeing
   price-drop/restock entries in the change feed.

7. View everything:
   ```bash
   python3 -m http.server 8000
   # open http://localhost:8000
   ```
   `index.html` links to both `dashboard.html` (reads `healing_log.json`)
   and `products.html` (reads `products_snapshot.json` +
   `product_changes.json`). Both auto-refresh every 3 seconds.

## Demoing the self-heal live

Real layout drift is unpredictable, so Sentinel supports a safe, repeatable
demo mode:

```bash
python3 sentinel.py \
  --collector-id c_msyndimi68dk1qu6l \
  --url "https://www.amkette.com/pages/evofox" \
  --schema schema.amkette.json \
  --once --auto-approve --simulate-break
```

`--simulate-break` replaces only the **first** run with synthetic broken
records (required fields dropped), so the health check fails on demand.
Everything after that — `diagnose`, `scraper heal`, `scraper approve`, and
the verification re-run — still calls the **real** Bright Data CLI against
the real collector. Since the real site is healthy, the verify step
reliably comes back at 100%, giving a clean "broke → healed → verified"
arc for a demo recording without needing to actually damage the live
scraper or gamble on a real layout change happening on camera.

Alternative (non-simulated) approaches, if you want to demo against a truly
broken scraper: point `--url`/the collector at a modified/mirrored copy of
the page with a renamed CSS class, or manually edit the collector's
template in the Scraper Studio UI to break a selector.

## Files

| File | Purpose |
|---|---|
| `index.html` | Landing page linking to the dashboard and tracker |
| `sentinel.py` | Core watcher: run → validate → diagnose → heal → approve → verify → log |
| `tracker.py` | Diffs each run against the last snapshot; logs price/stock changes |
| `schema.amkette.json` | Expected fields/types for Sentinel's health validation (matches real Amkette output, including nested `price.value`) |
| `dashboard.html` | Reads `healing_log.json`, renders health + timeline |
| `products.html` | Reads `products_snapshot.json` + `product_changes.json`, renders the live catalog + change feed |
| `healing_log.json` | Generated at runtime by `sentinel.py` — the healing event history |
| `products_snapshot.json` | Generated at runtime by `tracker.py` — latest known state per product |
| `product_changes.json` | Generated at runtime by `tracker.py` — append-only price/stock change log |

## Notes on reliability (edge cases handled)

- Missing/empty/wrong-typed fields, and zero-record responses, are all
  treated as health signals, not crashes.
- Nested fields (like the real `price.value`) are validated via dot-path
  lookup, not just top-level keys.
- CLI failures (missing binary, timeout, bad JSON, non-zero exit) raise a
  clear error and get logged as a failed event rather than crashing
  silently — including real 409 "another refactor job is in progress"
  errors encountered during development, which are caught and logged
  instead of crashing Sentinel.
- `scraper heal` is human-in-the-loop by default in the Bright Data CLI;
  Sentinel only auto-commits fixes when you pass `--auto-approve` —
  otherwise it stops and tells you to review the proposed fix yourself.
  A stuck/awaiting-approval heal can be cleared with
  `bdata scraper approve <id> --reject`.
- Tracker only logs a change when a field's value actually differs from
  the last snapshot — a completely new product (first time its URL is
  seen) is logged separately as `new_product` rather than as a false
  price/stock change.

## AI-assistance disclosure

`sentinel.py`, `tracker.py`, `dashboard.html`, `products.html`,
`index.html`, and this README were built with Claude (Anthropic) based on
the Bright Data CLI's documented commands (`scraper create/run/heal/approve`).
Claude was used as a coding agent throughout: scaffolding the initial
watcher, fixing real bugs discovered while running against the live CLI
(a missing `--urls` flag on `scraper run`, and schema validation that
didn't support the real nested `price` object), adding the
`--simulate-break` demo mode, building the Tracker feature, and doing a
visual design pass on the dashboard/tracker pages. The scraper's target
site, schema, collector ID, health thresholds, and demo/debugging decisions
(including diagnosing and resolving a real 409 stuck-job error during
testing) were driven by the project author, who has read through
`sentinel.py` and can explain its validation/diagnosis logic.
