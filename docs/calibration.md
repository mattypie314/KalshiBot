# Fair-value calibration (offline)

Measures whether model **Yes** probabilities match official settlement — the CF Benchmarks **60-second average** (BRTI for BTC, ETHUSD_RTI for ETH). It is **not** a live hit rate and **not** paper assumed-maker-fill PnL.

Uses **every scanned strike** in the scan log (taken or not). Trading gates (6% edge, close-strike ban, Turbo, live size) are not read and not changed.

Rates (Yes frequency, majority-call hit) are printed only when a bucket has **n ≥ 20**. Thinner buckets are labeled `thin` and the rate is withheld — same discipline as `./kb eval`.

## What the scan log already has

Hourly `artifacts/scan_log.jsonl` stores `markets[]` (ticker, strike, book, close, plus spot/vol/fair/z when the tick had them), plus spots, vol, and which tickers were ideas. That is enough to rebuild `model_prob` / `z` / ask-or-join. Scan and live both append a line each run.

`artifacts/last_run.json` only lists ticker strings. It cannot expand strikes by itself.

Settlement is **joined**, not stored on every scan row. Sources, in order:

1. `artifacts/settlements.jsonl` or `settlement_prints.jsonl` if you keep them
2. Journal `settlement_print` / `settlement_result` on `trade_log.jsonl` / `paper_log.jsonl` (the official print or Yes/No — **never** `pnl`)
3. Optional `--fetch-prints` against Kalshi CF history for closed hours still missing a print

## 15m

Current ticks append `markets[]` (every scanned strike) on `artifacts/fifteen_scan_log.jsonl` and keep the Pass-idea / notes fields the boards already read. Older idea-only lines cannot expand; `./kb15 calibrate` will say so until new ticks land.

On the Pi, snapshot before `git pull` so a mid-pull crash does not strand a dirty checkout.

## On the Pi

Hourly checkout (`/home/KalshiBot`):

```bash
cd /home/KalshiBot
source .venv/bin/activate   # if you use the venv
./kb calibrate
# same thing:
python3 -m src.calibrate --artifacts artifacts
```

Writes `artifacts/calibration_rows.jsonl` (one row per strike/window) and prints bucket summaries.

15m checkout (`/home/KalshiBot15`):

```bash
cd /home/KalshiBot15
./kb15 calibrate
```

Useful flags:

```bash
./kb calibrate --artifacts artifacts
./kb calibrate --settlements /path/to/prints.jsonl
./kb calibrate --all-scans          # keep every snapshot (default: last scan per ticker/close)
./kb calibrate --include-proxy      # bucket Coinbase-priced rows too (default: exclude)
./kb calibrate --fetch-prints       # fill missing closed-hour prints via Kalshi CF history
./kb calibrate --out /tmp/rows.jsonl
```

A settlements jsonl row can be either a window print or a ticker result:

```json
{"asset": "BTC", "close_time": "2026-09-09T17:00:00-04:00", "settlement_print": 77300.0}
{"ticker": "KXETHD-26SEP0917-T2399", "settled_yes": false, "settlement_print": 2395.10}
```

Yes wins only if the official 60s average finishes **above** the strike (equal is No).

Do not retune edge / close-strike / Turbo / size from this tape.
