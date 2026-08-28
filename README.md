# KalshiBot

Matt's small-account Kalshi campaign plus a research desk for **Crypto**, **Commodities**, and **Sports Bets**.

## Campaign playbook

One book. No $5 / $10 pots. 15-minute, hourly, and maker loops share it.

Tracker: `~/.kalshi/crypto-campaign.json` (override with `TRACKER_PATH`).

| Loop | Schedule | Universe |
| --- | --- | --- |
| 15m | every 3 minutes | BTC/ETH/SOL + gold/silver 15m |
| Hourly | every 5 minutes | hourly crypto/commodities ending in the next 75 minutes (no daily `…D` books) |
| Maker | minutes 12–14, 27–29, 42–44, 57–59 | extra limit-posting scan of the same books |

**Small-account rules**

- Risk about 3–5% of bankroll per idea. Cap 5–8%. Never more than 10% on one trade.
- Fractional Kelly (default 0.33×). On a $35 book that is about $1.50–$2.80 per idea.
- If the book is under $20, max risk is 3% and the bot demands a bigger edge.
- Prefer **post-only limit orders** inside the spread. No IOC take, no rest-99 after a take. Taker fees wipe a small edge.
- Filter first, size second. Sitting out is a valid trade. No revenge bet for 15 minutes after a loss.
- Hard filters: net edge after fees ≥ 4% (6% if the book is thin or the account is under $20); model must also clear fees + a 2–3% buffer; the spread cannot eat most of the edge; at least 3 minutes left.
- Price 15-minute crypto from live spot, time left, and recent 1-minute realized vol (not daily vol). Quiet starting points: BTC ~0.3–0.6%/hour, ETH jumpy-er.
- Exit: take profit (+2¢ or 99¢ bid), flatten if the statistical edge decays, or hold to settlement. You can lose the full amount on a contract.

**Raising the book as it grows**

Percents live in one place: `kalshibot/campaign/playbook.py` (and matching fields in `kalshibot/config.py` / env vars). You usually leave those alone and only raise the dollar book:

- GitHub: **Actions → Kalshi campaign → Run workflow** and type the new dollar amount in `bankroll` (for example `35`).
- Env: `CAMPAIGN_BANKROLL` for a fresh tracker, or the `bankroll` workflow input to update the saved book.

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
3. Open **Kalshi campaign → Run workflow** on the latest `main`. Type a `bankroll` only when you want to change the dollar book. Do not re-run an old red job.

It stays in practice mode (DRY) until Kalshi secrets are under **Settings → Secrets and variables → Actions**: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` (the full .pem text), and `KALSHI_LIVE` set to `1`.

Practice tickets from DRY runs are not real Kalshi fills; the bot drops those before placing live orders. The campaign book is saved on a `campaign-state` branch so it survives between runs.
