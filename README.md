# KalshiBot

Matt's small-account Kalshi campaign plus a research desk for **Crypto**, **Commodities**, and **Sports Bets**.

This is **not financial advice**. You can lose the full amount you put on a contract. Demo first. Live trading is off unless you set both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES`.

## Hourly BTC/ETH scanner

Standalone dry-run scanner for Kalshi **BTC and ETH hourly threshold** books (`KXBTCD`, `KXETHD` — above/below). Not sports. Not perps. Not SOL/XRP. Not 15-minute up/down unless those hourly books are missing this hour.

Bankroll default **$46.36**. Risk per idea **3–5%** ($1.40–$2.30), preferred **$2.00**, hard cap **$3.00**. Maker / limit orders only. Skip if the spread eats the edge. Net edge after estimated taker fees must be ≥ 6% (4% only if the spread is tight and the book can fill the tiny size).

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill keys locally — never commit them

python -m src.main scan
python -m src.main scan --asset BTC
python -m src.main once          # scan + print dry-run limit payloads
python -m src.main live          # refused unless LIVE_TRADING=true AND CONFIRM_LIVE=YES
```

Exit codes: `0` success or `NO_ACTIONABLE_EDGE`, `2` config/auth, `3` rate limited.

If nothing passes filters the process prints `NO_ACTIONABLE_EDGE` and exits 0. It never uses mid as a fill — Yes is bought at `yes_ask`, No at `no_ask`. Model vs market uses those executable prices.

### Demo first, then a VPS — not GitHub Actions for live orders

1. Create a Kalshi account and open the **demo** environment first (`USE_DEMO=true`).
2. Generate an API key + RSA keypair (do this on your machine, not in git):

```bash
mkdir -p ~/.kalshi
openssl genrsa -out ~/.kalshi/kalshi_private_key.pem 2048
openssl rsa -in ~/.kalshi/kalshi_private_key.pem -pubout -out ~/.kalshi/kalshi_public_key.pem
```

3. In Kalshi → Account Settings → API Keys, upload the **public** PEM. Save the key id.
4. Point the bot at the private PEM (never commit it):

```bash
export KALSHI_API_KEY_ID=your-key-id
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/kalshi_private_key.pem
```

On GitHub Actions you can store the PEM text as secret `KALSHI_PRIVATE_KEY` (the workflow writes it to a file at runtime). Still: **Actions is scan + notify only**. Kalshi often **403s cloud IPs**. That is expected. Do not retry-storm. For live limits use a cheap VPS with a stable IP and systemd/cron, not Actions.

```ini
# /etc/systemd/system/kalshi-hourly.service  (VPS)
# ExecStart=/usr/bin/python3 -m src.main once
# plus a timer at minute 3 if you want the same cadence
```

The workflow `.github/workflows/hourly.yml` runs `scan` at minute 3 of every hour, uploads `artifacts/last_run.json`, and can open a GitHub issue when something is actionable. It will not place live orders.

### Bankroll caps

| Knob | Default | Env |
| --- | --- | --- |
| Bankroll | $46.36 | `BANKROLL` |
| Min net edge | 6% | `MIN_NET_EDGE` |
| Soft edge (tight book) | 4% | `SOFT_NET_EDGE` |
| Risk % cap | 5% | `MAX_RISK_PCT` |
| Preferred risk | $2.00 | `PREFERRED_RISK_DOLLARS` |
| Hard dollar cap | $3.00 | `MAX_RISK_DOLLARS` |
| Fractional Kelly | 0.25× | `KELLY_MULT` |

Settlement is **not** Binance last tick. Official rules on these books use CF Benchmarks RTI (60-second average). The model still uses Coinbase/Binance spot as a **proxy** and says so in every report.

---


## Campaign playbook

One book. No $5 / $10 pots. 15-minute, hourly, and maker loops share it.

Tracker: `~/.kalshi/crypto-campaign.json` (override with `TRACKER_PATH`).

| Loop | Schedule | Universe |
| --- | --- | --- |
| 15m edge | first 2–4 minutes of each window (`:02–:04`, `:17–:19`, `:32–:34`, `:47–:49` ET) | BTC/ETH/SOL + gold/silver 15m. Pass/Fail vs mid. One post-only limit. 3–5% of Kalshi `total_value`. |
| Hourly | every 5 minutes | hourly crypto/commodities ending in the next 75 minutes (no daily `…D` books) |
| Maker | last 3 min of each 15m (`:12–:14`, `:27–:29`, `:42–:44`, `:57–:59`) | Rest post-only bids on **74–93¢** confirmed favorites. Edge is the spread (taker is at break-even). |

**15m edge (not a scan-all-day scalp)**

- Look only at the open of the window, or flatten/stay flat after a one-time half-sigma recheck. No new ticket at minute 0 (that is settlement of the window that just closed).
- Before any order: **Pass or Fail** (model-fair vs live mid). Fail skips. Do not scalp a Fail.
- Pass → one post-only limit. Yes joins the live Yes bid. No joins the live Yes ask. Never cross, never market, never IOC pay-through.
- Hard skips: under ~8 minutes left unless the strike is decided; spread wider than the edge; news candle (CPI/FOMC); fair and mid within 4¢; revenge (skip the next 15m window after a losing 15m ticket); three 15m losses in a row this ET day (stop 15m and tell Matt); book not live; 15m already stopped; room below 3% of bankroll.
- Manage: flatten if down ~$0.50 or ~10% from fill, held-side bid ≥ fill + 2¢, half-sigma says the edge died/flipped, or bid is 99¢. Never rest a sell under the bid. Flat is allowed.

**Small-account rules (hourly / shared book)**

- Risk about 3–5% of bankroll per idea. Cap 5–8%. Never more than 10% on one trade.
- Fractional Kelly (default 0.33×). On a $35 book that is about $1.50–$2.80 per idea.
- If the book is under $20, max risk is 3% and the bot demands a bigger edge.
- Prefer **post-only limit orders** inside the spread. No IOC take, no rest-99 after a take. Taker fees wipe a small edge.
- Filter first, size second. Sitting out is a valid trade. No revenge bet for 15 minutes after a loss.
- Hard filters: net edge after fees ≥ 4% (6% if the book is thin or the account is under $20); model must also clear fees + a 2–3% buffer; the spread cannot eat most of the edge; at least 3 minutes left.
- Price 15-minute crypto from live spot, time left, and recent 1-minute realized vol (not daily vol). Quiet starting points: BTC ~0.3–0.6%/hour, ETH jumpy-er.
- Exit: take profit (+2¢ or 99¢ bid), flatten if the statistical edge decays, or hold to settlement. You can lose the full amount on a contract.

**Last-3-minute maker (spread capture)**

This is a separate routine from the 4–6% playbook. In the final 3 minutes, a confirmed favorite often sits at **taker break-even**. Taking the ask has no edge after fees. Resting a bid at 74–93¢ earns the spread (cents per fill, many fills). Never IOC. Size is still small-account (about 3% cap).

After this is merged the **Pi** runs the loops (dashboard Fire buttons, or `python -m kalshibot serve` plus a local scheduler). GitHub Actions does **not** auto-trade. To run one GitHub fire by hand: **Actions → Kalshi campaign → Run workflow**. Set `halted` to **yes** only on that GitHub job (it cancels resting orders GitHub still sees). To stop only maker: set `maker_auto` to **no**. Leave `keep` if you are only changing bankroll.

**Raising the book as it grows**

You do not have to edit code. Size is a **percent of the book**, so bigger cash means bigger tickets.

On your iPhone: open the Pi dashboard (Halt, Fire, DRY / LIVE). GitHub **Run workflow** is only a one-off Actions fire — do not use it while the Pi is live.

| Field | What to type | Leave blank / keep |
| --- | --- | --- |
| `bankroll` | Max dollars the bot may use, like `50` | Follows Kalshi cash (grows when you deposit) |
| `follow_kalshi_cash` | `yes` or `no` | `keep` |
| `maker_auto` | `yes` to run every 15-minute window, `no` to stop | `keep` |

Usual path: deposit on Kalshi, leave the fields blank. Next run sizes off your live cash. Type a `bankroll` only if you want a cap so the bot does not use the whole Kalshi account.

The same knobs are saved on the `campaign-state` book so they stick until you change them.

| Knob | Default | Env |
| --- | --- | --- |
| Kelly fraction | 0.33 | `KELLY_FRACTION` |
| Risk cap | 8% | `RISK_CAP` |
| Hard max | 10% | `RISK_HARD_MAX` |
| Under-$20 risk | 3% | `SMALL_BANKROLL_RISK` |
| Min net edge | 4% | `MIN_NET_EDGE` |
| Thin-book / small-account edge | 6% | `TARGET_NET_EDGE` |

Ignore daily books (`KXETHD`, `KXBTCD`, `KXDOGED`, other tickers ending in `D`).

**Live trading is off by default.** Dry-run logs intended limits and does not fake fills. To enable live:

```bash
export KALSHI_API_KEY_ID=...
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/kalshi_private_key.pem
export KALSHI_LIVE=1
```

```bash
python -m kalshibot campaign status
python -m kalshibot campaign fire fifteen
python -m kalshibot campaign fire hourly
python -m kalshibot campaign fire maker
python -m kalshibot campaign run
```

## Research desk

```bash
pip install -r requirements.txt
python -m kalshibot serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The campaign book sits above the Crypto / Commodities / Sports Bets tabs. **Serve also runs the loops by itself** while that process is up: 15m at `:02–:04` ET each window, maker in the last 3 minutes, hourly about every 5 minutes. Halt / DRY-LIVE on the phone still apply. The small 15m / Hourly / Maker buttons are “run now,” not the schedule.

```bash
python -m kalshibot scan --section crypto
pytest
```

## From an iPhone (no computer)

GitHub does **not** run the campaign on a schedule. The Pi is the live host.

1. Merge this pull request.
2. Open the repo **Actions** tab. If the **Kalshi campaign** workflow is still enabled, open it → **…** → **Disable workflow** so no leftover cron fires.
3. Optional: **Kalshi campaign → Run workflow** is a one-off GitHub fire. Do not use it while the Pi is live.

It stays in practice mode (DRY) until Kalshi secrets are under **Settings → Secrets and variables → Actions**: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` (the full .pem text), and `KALSHI_LIVE` set to `1`.

Practice tickets from DRY runs are not real Kalshi fills; the bot drops those before placing live orders. The campaign book is saved on a `campaign-state` branch so it survives between runs.
