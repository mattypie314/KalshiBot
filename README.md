# KalshiBot

Hourly BTC and ETH threshold scanner for Kalshi (`KXBTCD`, `KXETHD`).

Not financial advice. You can lose the full amount you put on a contract. Demo first. Live trading stays off unless both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES` are set.

The 15-minute campaign, maker loop, dashboard, and research desk are parked on the `archive/campaign-desk` branch for later.

## What this does

Scans the live hourly above/below books for Bitcoin and Ethereum. Fair probability comes from spot plus recent realized vol. It only prints or places a **limit** order when net edge after estimated fees clears the filter.

Bankroll default **$46.36**. Risk per idea **3–5%** ($1.40–$2.30), preferred **$2.00**, hard cap **$3.00**. Maker / limit only. Skip if the spread eats the edge. Net edge after estimated taker fees must be ≥ 6% (4% only if the spread is tight and the book can fill the tiny size).

```bash
pip install -r requirements.txt
cp .env.example .env   # fill keys locally — never commit them

python -m src.main scan
python -m src.main scan --asset BTC
python -m src.main once          # scan + print dry-run limit payloads
python -m src.main auth          # test key + PEM (no orders)
python -m src.main live          # refused unless LIVE_TRADING=true AND CONFIRM_LIVE=YES
```

Exit codes: `0` success or `NO_ACTIONABLE_EDGE`, `2` config/auth, `3` rate limited.

If nothing passes filters the process prints `NO_ACTIONABLE_EDGE` and exits 0. It never uses mid as a fill — Yes is bought at `yes_ask`, No at `no_ask`.

## Keys

1. Create a Kalshi account and open the **demo** environment first (`USE_DEMO=true`).
2. Generate an API key + RSA keypair on your machine, not in git:

```bash
mkdir -p ~/.kalshi
openssl genrsa -out ~/.kalshi/kalshi_private_key.pem 2048
openssl rsa -in ~/.kalshi/kalshi_private_key.pem -pubout -out ~/.kalshi/kalshi_public_key.pem
```

3. In Kalshi → Account Settings → API Keys, upload the **public** PEM. Save the key id.
4. Point the bot at the private PEM:

```bash
export KALSHI_API_KEY_ID=your-key-id
export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/kalshi_private_key.pem
```

GitHub Actions (`.github/workflows/hourly.yml`) runs `scan` at minute 3 of every hour. It does **not** place live orders. Kalshi often 403s cloud IPs. For live limits use a VPS with a stable IP.

## Bankroll caps

| Knob | Default | Env |
| --- | --- | --- |
| Bankroll | $46.36 | `BANKROLL` |
| Min net edge | 6% | `MIN_NET_EDGE` |
| Soft edge (tight book) | 4% | `SOFT_NET_EDGE` |
| Risk % cap | 5% | `MAX_RISK_PCT` |
| Preferred risk | $2.00 | `PREFERRED_RISK_DOLLARS` |
| Hard dollar cap | $3.00 | `MAX_RISK_DOLLARS` |
| Fractional Kelly | 0.25× | `KELLY_MULT` |

Settlement on these books uses CF Benchmarks RTI (60-second average), not a single exchange last tick. The model uses Coinbase/Binance spot as a **proxy** and says so in every report.

## Later

Full campaign desk (15m / maker / dashboard / sports research):

https://github.com/mattypie314/KalshiBot/tree/archive/campaign-desk
