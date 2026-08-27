# KalshiBot

Matt's Kalshi campaign loops (from GrokBot) plus a research desk for **Crypto**, **Commodities**, and **Sports Bets**.

## Campaign loops

Tracker: `~/.kalshi/crypto-campaign.json` (override with `TRACKER_PATH`).

| Loop | Schedule | Universe | Pot |
| --- | --- | --- | --- |
| 15m | every 3 minutes | BTC/ETH/SOL (shard 2) + gold/silver 15m (shard 0) | $5, stop -$0.50, skip new if room < $0.50 |
| Hourly | every 5 minutes | live hourly crypto/commodities | $10, stop -$0.50, skip new if room < $1 |
| Maker | minutes 12–14, 27–29, 42–44, 57–59 | 15m books; hourlies at :57–:59 | same two pots, never share room |

Shared ticket rules (all three loops):

- Flatten IOC if down ~$0.50 or ~10% from fill (18% if filled before 3:00 AM ET 2026-08-27)
- Take profit if held-side bid ≥ fill + 2¢, or sell immediately at 99¢
- Never rest a sell under the bid
- 15m/hourly: IOC pay-through on a real favorite, then rest 99¢ post-only
- Maker: post-only join 74–93¢, never IOC-pay-through

**Live trading is off by default.** Dry-run logs intended orders. To enable live:

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

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Campaign pots sit above the Crypto / Commodities / Sports Bets tabs.

```bash
python -m kalshibot scan --section crypto
pytest
```

Not financial advice. Wins stay in their own pot.
