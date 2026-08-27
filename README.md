# KalshiBot

Automated Kalshi predictions for **Crypto**, **Commodities**, and **Sports Bets**.

The bot scans liquid Kalshi series in those three categories, estimates a fair probability, and ranks contracts by executable edge versus the live bid/ask. It is a research desk: **it does not place orders**.

## What it does

| Section | Model |
| --- | --- |
| Crypto | Spot price (Coinbase, Yahoo fallback) vs contract strike, lognormal digital probability |
| Commodities | Futures spot (Yahoo: gold, silver, copper, WTI, Brent, nat gas) vs strike |
| Sports Bets | De-vig mutually exclusive moneylines so outcomes sum to 100% |

Each row shows Kalshi implied probability, the bot’s probability, which side is cheap (YES/NO), confidence, and a one-line rationale.

## Run the dashboard

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m kalshibot serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Use the three section tabs, filter box, and “Edges ≥ 2¢”.

## One-shot scan

```bash
python -m kalshibot scan
python -m kalshibot scan --section crypto
python -m kalshibot scan --json --section sports
```

No Kalshi API key is required for market data.

## Tests

```bash
pytest
```

## Notes

- Forecasts are mechanical, not financial advice.
- Live order placement is intentionally out of scope for this version.
- Tune `SERIES_PER_SECTION` and `CACHE_TTL_SECONDS` via environment variables if you want a wider or fresher scan.
