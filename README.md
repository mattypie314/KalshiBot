# KalshiBot

Matt's small-account Kalshi campaign plus a research desk for **Crypto**, **Commodities**, and **Sports Bets**.

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

After this is merged it **runs every 15-minute window by itself** (GitHub schedule). To stop it: **Actions → Kalshi campaign → Run workflow** and set `maker_auto` to **no**. To start again, set `maker_auto` to **yes**. Leave `keep` if you are only changing bankroll.

**Raising the book as it grows**

You do not have to edit code. Size is a **percent of the book**, so bigger cash means bigger tickets.

On your iPhone: **Actions → Kalshi campaign → Run workflow**

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The campaign book sits above the Crypto / Commodities / Sports Bets tabs.

```bash
python -m kalshibot scan --section crypto
pytest
```

## From an iPhone (no computer)

GitHub runs the campaign every 5 minutes after this pull request is merged.

1. Merge this pull request.
2. Open the repo **Actions** tab. If GitHub asks to enable workflows, tap Enable.
3. Open **Kalshi campaign → Run workflow** on the latest `main`. Maker auto is already on. Set `maker_auto` to **no** when you want it to stop. Do not re-run an old red job.

It stays in practice mode (DRY) until Kalshi secrets are under **Settings → Secrets and variables → Actions**: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` (the full .pem text), and `KALSHI_LIVE` set to `1`.

Practice tickets from DRY runs are not real Kalshi fills; the bot drops those before placing live orders. The campaign book is saved on a `campaign-state` branch so it survives between runs.
