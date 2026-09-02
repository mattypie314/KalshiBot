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
- **Execution:** limit orders only (prefer rest inside the spread). It does not market-buy
- **Host:** a live Kalshi key needs `USE_DEMO=false` (edit `.env` or `./kb env --prod`). Demo host 401s that key.
- **Live:** `./kb live` then type `LIVE` (or `--confirm LIVE`). `.env` can stay dry. Unattended live still needs `LIVE_TRADING=true` and `CONFIRM_LIVE=YES`

A contract pays $1 if you are right and $0 if you are wrong. The price (like 0.42) is what you pay per contract. That 42¢ is also the market’s implied chance.

## Bankroll and size

| Rule | Number at $40 |
| --- | --- |
| Bankroll | $40 |
| Risk per idea (preferred) | $2.00 |
| Max % of bankroll | 5% = $2.00 |
| Hard dollar cap | $3.00 |
| Kelly fraction | 0.25× (quarter of full Kelly) |
| Typical ticket | about $2, never over $3 |

How size is picked:

1. Compute quarter-Kelly from model edge
2. Take the smallest of: that Kelly size, 5% of $40 ($2), preferred $2, hard cap $3
3. Contracts = floor(dollars ÷ entry price)
4. One contract is allowed only if that one contract is still ≤ $3

Anti-revenge: if it already lost this same UTC hour, it will not size bigger than last time. If last size was zero, it sits the rest of the hour.

## The model

1. Pull spot from Binance (Coinbase backup)
2. Pull hourly vol from recent 1-minute candles (~last 4 hours). Fallback if that fails: BTC 0.4%/hour, ETH 0.5%/hour
3. Measure how far the line is from spot, in typical remaining-hour moves (z-score)
4. Turn that into a fair probability that price finishes above the line
5. Compare fair odds to the real ask you would pay (never the mid)

Kalshi settles on CF Benchmarks RTI (a 60-second average), not Binance last tick. Spot is a **proxy**.

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
- Net edge ≥ 6% in normal books
- Net edge ≥ 4% only if the book is tight (spread ≤ 2¢) and depth ≥ 5
- If |z| > 2.5 (fat-tail / long-shot), need net edge ≥ 8%
- If the side needs a huge jump (|z| > 3.5), skip — treat as news-only, not a vol bet
- CPI window: sit 8:15–8:45 AM ET on CPI print days
- FOMC window: sit 1:45–2:45 PM ET on FOMC days

If nothing passes: print `NO_ACTIONABLE_EDGE` and do nothing. Sitting is a valid action.

## How it places the order

- Score both Yes and No; keep the higher net-edge side
- Prefer a maker limit: one tick inside the spread, or on the bid if the book is only 1¢ wide
- Fresh-quote before the POST. If Kalshi rejects `post only cross`, step one tick more passive and retry. Never lift / take
- Order type: GTC, `post_only` when resting inside
- Yes = bid on the Yes book at the limit
- No = ask on the Yes book at 1 − No limit (same book, other side)
- Before a new live order, cancel leftover hourly rests that are no longer the chosen ticker
- GitHub Actions is scan only (minute 3 of each hour). Live fills are meant for the Pi / a stable IP

## What it will not do

- No sports, no parlays, no 15-minute campaign loop (that code is parked)
- No taking the mid as a fill price
- No stacking many ideas in one hour (max 1)
- No sizing up after a loss in the same hour
- No trading through scheduled CPI/FOMC windows
- No live orders without typing `LIVE`, passing `--confirm LIVE`, or setting both env safety flags

## Operating checklist

- Bankroll in use: **$40** (`BANKROLL=40`)
- Risk unit: $2 typical, $3 absolute ceiling
- Need ~6%+ net edge after fees, or 4% only on a tight, deep book
- One limit per run, rest if you can, skip if you cannot
- Re-check after spot or time moves; short-hour edges die fast
