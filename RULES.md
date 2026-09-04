# KalshiBot rules (bankroll $40)

These are the live rules of KalshiBot: hourly BTC and ETH “above/below this dollar line” contracts only (`KXBTCD`, `KXETHD`). Not sports. Not 15-minute scalps.

This is **not financial advice**. A wrong contract can go to $0.

Set `BANKROLL=40` in `.env` if you override it. The code default is **$40**.

## What it is allowed to trade

- **Yes:** price finishes above the line this hour
- **No:** price finishes at or below the line this hour
- **Coins:** BTC and ETH only
- **Books:** hourly threshold markets, up to 12 per coin per scan
- **One idea per run.** Everything else is watch-only
- **Open tickets:** max 1 hourly crypto idea. A second is allowed only on the other coin and the opposite side. Same-direction BTC+ETH Nos sit (that was the 2026-09-02 stacked card).
- **Loseable risk:** $1.75 preferred, $2.00 hard cap on a $40 bankroll (also 5% of `BANKROLL`)
- **Execution:** maker / limit only. It does not lift the ask for a thin edge. It does not market-buy
- **Host:** a live Kalshi key needs `USE_DEMO=false` (edit `.env` or `./kb env --prod`). Demo host 401s that key.
- **Live (keyboard):** `./kb live --prod` then type `LIVE` (or `--confirm LIVE`). `.env` can stay dry.
- **Halted:** `HALTED=true` (the default) refuses live orders even with `--confirm LIVE`. Disable the Pi timer: `sudo systemctl disable --now kalshi-hourly.timer`.
- **Live (unattended, only after unhalt):** systemd has no TTY. It needs `HALTED=false` **and** both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES`. `--confirm LIVE` alone does not arm a timer or GitHub Action.

A contract pays $1 if you are right and $0 if you are wrong. The price (like 0.42) is what you pay per contract. That 42¢ is also the market’s implied chance.

## Bankroll and size

| Rule | Number at $40 |
| --- | --- |
| Bankroll | $40 |
| Risk per idea (preferred) | $1.75 |
| Max % of bankroll | 5% = $2.00 |
| Hard dollar cap | $2.00 |
| Kelly fraction | 0.25× (quarter of full Kelly) |
| Typical ticket | $1.50–$2.00, never over $2 |

How size is picked:

1. Compute quarter-Kelly from model edge
2. Take the smallest of: that Kelly size, 5% of $40 ($2), preferred $1.75, hard cap $2
3. Contracts = floor(dollars ÷ entry price)
4. One contract is allowed only if that one contract is still ≤ $2
5. Do not stack four $2 tickets in one morning. Max 1 open hourly idea (2 only if different coin and opposite side).

Anti-revenge: if the last live hourly ticket **filled** and settled against us (or a fill reports negative pnl), the next idea cannot size bigger than last time. If last size was zero, it sits. The ticket survives the Eastern `:00` hour roll so a just-settled loss still counts. Unfilled rests are not wins or losses. After 2 filled losses or $4 filled loss in one Eastern day, sit. Reports and settlements are America/New_York.

## The model

1. Pull spot from CF Benchmarks BRTI (BTC) / ETHUSD_RTI (ETH) via Kalshi — the 60-second average Kalshi settles on. The ETH id is `ETHUSD_RTI`, not `ERTI`. Coinbase/Binance are **display proxies only**, not settlement truth. Without the official index the coin sits (`REQUIRE_SETTLEMENT_INDEX=true`).
2. Pull hourly vol from recent 1-minute candles (~last 4 hours). Fallback if that fails: BTC 0.4%/hour, ETH 0.5%/hour. If realized vol is 2× typical, sit (news tape)
3. Measure how far the line is from spot, in typical remaining-hour moves (z-score)
4. Turn that into a fair probability that price finishes above the line
5. Compare fair odds to the real ask you would pay (never the mid)

Kalshi settles on the 60-second average of CF Benchmarks’ real-time index (BRTI / ETHUSD_RTI) in the minute before the clock time. The ETH request id is `ETHUSD_RTI`, not `ERTI`. The model prices off that index when the Kalshi passthrough works. Coinbase last tick is a fallback, not the print.

- **Vol** = how jumpy price has been
- **z-score** = how many normal hourly moves the line is away. |z| of 2.5+ is a long shot
- **Zero drift** = the model does not assume BTC should keep going up this hour. It only uses distance + noise

## Edge math (must clear this)

- Gross edge = model chance × payout if win − (1 − model chance) × cost if lose (fair − price)
- Taker fee ≈ 0.07 × price × (1 − price) per contract, rounded up. Worst around 50¢ (~1.75¢ per contract)
- Net edge = gross edge − that fee
- The filter always uses taker fee + the ask, even if it later posts a maker limit. Maker is a bonus, not the reason to bet

## Hard filters (must all pass)

Skip unless every box is checked:

- Market is open
- At least 3 minutes left
- Ask is between 5¢ and 95¢ (no 99¢ favorites, no 1¢ lottery tickets)
- Spread (ask − bid) ≤ 6¢, unless net edge is already ≥ 10%
- If it would have to lift the ask, visible size must be ≥ 5 contracts
- Net edge ≥ 6% after fees. No 4% tight-book exception. No edge = sit
- Ban close strikes: skip if the line is inside 0.50% of spot, or inside 1.5× a normal 1-hour move. A fat model edge on a tight strike is not a reason to fade it.
- If realized vol is 2× typical, sit the coin (headline / war-tape days). Operator hook: `NEWS_PAUSE=true` sits everything without scraping headlines.
- If |z| > 2.5 (fat-tail / long-shot), need net edge ≥ 8%
- If the side needs a huge jump (|z| > 3.5), skip — treat as news-only, not a vol bet
- CPI window: sit 8:15–8:45 AM ET on CPI print days
- FOMC window: sit 1:45–2:45 PM ET on FOMC days
- If the close-strike / buy-No bucket in `artifacts/trade_log.jsonl` is underwater (3+ **filled** settled, net red), that rule turns off
- Daily sit: 2 filled losses or $4 filled loss (ET day). Caps, not a fitted edge.
- Paper tape (`./kb scan` / `./kb once` → `artifacts/paper_log.jsonl`) is a counterfactual. Default assumed-maker-fill at the printed limit. PROXY / missing BRTI/ERTI = sit/unscored. Settle vs official 60s BRTI/ERTI average. Do not retune these rules from paper PnL.

If nothing passes: print `NO_ACTIONABLE_EDGE` and do nothing. Sitting is a valid action.

## How it places the order

- Score both Yes and No; keep the higher net-edge side
- Prefer a maker limit: one tick inside the spread, or on the bid if the book is only 1¢ wide
- Do not cross for a 6% edge. If a maker rest is impossible, sit (`REQUIRE_MAKER=true`)
- Fresh-quote before the POST. If Kalshi rejects `post only cross`, step one tick more passive and retry. Never lift / take
- Order type: GTC, always `post_only`
- Yes = bid on the Yes book at the limit
- No = ask on the Yes book at 1 − No limit (same book, other side)
- Before a new live order, cancel leftover hourly rests that are no longer the chosen ticker
- GitHub Actions hourly scan is halted (no cron). The Pi timer must stay disabled while `HALTED=true`. Cloud IPs often 403.

## What it will not do

- No sports, no parlays, no 15-minute campaign loop (that code is parked)
- No taking the mid as a fill price
- No stacking many ideas in one morning (max 1 open hourly ticket; 2 only if different coin and opposite side)
- No sizing up after a loss to “win it back”
- No flipping always-No to always-Yes on tight strikes — both are the same mistake
- No firing every hour just because the timer ran. Sitting is a valid action
- No trading through scheduled CPI/FOMC windows or 2×-vol news tape
- No live orders without typing `LIVE` on a keyboard, or setting both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES` (and `HALTED=false`)

## Operating checklist

- Bankroll in use: **$40** (`BANKROLL=40`)
- Risk unit: $1.75 typical, $2 absolute ceiling
- Need 6%+ net edge after fees. Far strikes only. One open hourly idea
- One limit per run, rest if you can, skip if you cannot. Log strike distance, time left, fair %, Kalshi price, result
- Re-check after spot or time moves; short-hour edges die fast
- Paper: `./kb scan` then `./kb eval` / `./kb paper`. Assumed maker fill, not live. Live stays off until you type LIVE.
