# KalshiBot

One repo. One Kalshi key setup. Two tools on top of it.

This is **not financial advice**. You can lose the full amount you put on a contract. Demo first.

| Tool | Command | Live switch |
| --- | --- | --- |
| Research dashboard + 15m / maker / KXBTC15M campaign | `python -m kalshibot serve` | `KALSHI_LIVE=1` (DRY/LIVE pill on the Pi) |
| Hourly BTC/ETH threshold scanner (`KXBTCD`, `KXETHD`) | `python -m kalshibot hourly scan` | `LIVE_TRADING=true` **and** `CONFIRM_LIVE=YES` |

Same keys, same demo host. The hourly scanner is the stricter one: it will not place an order unless both live flags are set.

## One-time setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill keys locally — never commit them

mkdir -p ~/.kalshi
openssl genrsa -out ~/.kalshi/kalshi_private_key.pem 2048
openssl rsa -in ~/.kalshi/kalshi_private_key.pem -pubout -out ~/.kalshi/kalshi_public_key.pem
```

In Kalshi → Account Settings → API Keys, upload the **public** PEM. Save the key id.

```bash
export KALSHI_API_KEY_ID=your-key-id
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/kalshi_private_key.pem
export USE_DEMO=true
```

You can also drop `api_key_id` and the PEM under `~/.kalshi/` — both CLIs read those files.

```bash
python -m kalshibot hourly auth   # signed balance check, no orders
```

## Research desk and campaign

```bash
python -m kalshibot serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The campaign book sits above the Crypto / Commodities / Sports Bets tabs. While `serve` is up it also runs the loops: 15m at `:02–:04` ET each window, maker in the last 3 minutes, hourly **KXBTC15M** tape once an hour. Halt / DRY-LIVE on the phone still apply. The small Fire buttons are “run now,” not the schedule.

```bash
python -m kalshibot scan --section crypto
python -m kalshibot campaign status
python -m kalshibot campaign fire fifteen
python -m kalshibot campaign run
```

Tracker: `~/.kalshi/crypto-campaign.json` (override with `TRACKER_PATH`).

| Loop | Schedule | Universe |
| --- | --- | --- |
| 15m edge | first 2–4 minutes of each window (`:02–:04`, `:17–:19`, `:32–:34`, `:47–:49` ET) | BTC/ETH/SOL + gold/silver 15m. Pass/Fail vs mid. One post-only limit. 3–5% of Kalshi `total_value`. |
| Hourly tape | once an hour (notify) | live ATM **KXBTC15M** only. 6%+ net after taker fee or sit out. |
| Maker | last 3 min of each 15m | Rest post-only bids on **74–93¢** confirmed favorites. |

The **Pi** is the live host. GitHub Actions does not auto-trade the campaign. **Actions → Kalshi campaign → Run workflow** is a one-off fire — do not use it while the Pi is live.

**15m edge**

- Look only at the open of the window, or flatten/stay flat after a one-time half-sigma recheck.
- Pass or Fail vs live mid before any order. Fail skips.
- Pass → one post-only limit. Yes joins the live Yes bid. No joins the live Yes ask.
- Hard skips: under ~8 minutes left unless the strike is decided; spread wider than the edge; news candle; revenge after a losing 15m ticket.

**Hourly KXBTC15M tape**

Not the 1-hour `KXBTCD` / `KXETHD` books. Those are the other scanner below.

**Small-account rules**

- Risk about 3–5% of bankroll per idea. Cap 5–8%. Never more than 10% on one trade.
- Prefer post-only limits. Sitting out is a valid trade.
- Size is a percent of the book. Deposit on Kalshi and the next run sizes off live cash unless you set a `bankroll` cap.

## Hourly BTC/ETH scanner

Standalone dry-run scanner for **BTC and ETH hourly threshold** books (`KXBTCD`, `KXETHD`). Not sports. Not the 15-minute up/down tape unless those hourly books are missing this hour.

Bankroll default **$46.36**. Risk per idea **3–5%**, preferred **$2.00**, hard cap **$3.00**. Limit orders only. Net edge after estimated taker fees must be ≥ 6% (4% only if the spread is tight).

```bash
python -m kalshibot hourly scan
python -m kalshibot hourly scan --asset BTC
python -m kalshibot hourly once          # scan + print dry-run limit payloads
python -m kalshibot hourly live          # refused unless LIVE_TRADING=true AND CONFIRM_LIVE=YES
```

`python -m src.main scan` still works (GitHub Actions uses it).

Exit codes: `0` success or `NO_ACTIONABLE_EDGE`, `2` config/auth, `3` rate limited. It never uses mid as a fill — Yes is bought at `yes_ask`, No at `no_ask`.

`.github/workflows/hourly.yml` runs `scan` at minute 3 of every hour and can open a GitHub issue when something is actionable. It will not place live orders. Kalshi often 403s cloud IPs. For live limits use a VPS with a stable IP.

| Knob | Default | Env |
| --- | --- | --- |
| Bankroll | $46.36 | `BANKROLL` |
| Min net edge | 6% | `MIN_NET_EDGE` |
| Soft edge (tight book) | 4% | `SOFT_NET_EDGE` |
| Risk % cap | 5% | `MAX_RISK_PCT` |
| Preferred risk | $2.00 | `PREFERRED_RISK_DOLLARS` |
| Hard dollar cap | $3.00 | `MAX_RISK_DOLLARS` |
| Fractional Kelly | 0.25× | `KELLY_MULT` |

Settlement on these books uses CF Benchmarks RTI (60-second average). The model uses Coinbase/Binance spot as a **proxy** and says so in every report.

## From an iPhone

The Pi runs `kalshibot serve`. Open the dashboard for Halt, Fire, and DRY / LIVE.

Practice tickets from DRY runs are not real Kalshi fills. The campaign book is saved on the `campaign-state` branch so it survives between runs.

## Tests

```bash
pytest
```
