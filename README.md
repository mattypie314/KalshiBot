# KalshiBot

Hourly BTC and ETH threshold scanner for Kalshi (`KXBTCD`, `KXETHD`).

Not financial advice. You can lose the full amount you put on a contract. Demo first. `.env` can stay dry. Live is a one-run confirm: type `LIVE` at the prompt, or pass `--confirm LIVE`.

The 15-minute campaign, maker loop, dashboard, and research desk are parked on the `archive/campaign-desk` branch for later.

## What this does

Scans the live hourly above/below books for Bitcoin and Ethereum. Fair probability comes from spot plus recent realized vol. It only prints or places a **limit** order when net edge after estimated fees clears the filter.

Bankroll default **$40**. Risk per idea **3–5%** ($1.20–$2.00), preferred **$2.00**, hard cap **$3.00**. Maker / limit only. Skip if the spread eats the edge. Net edge after estimated taker fees must be ≥ 6% (4% only if the spread is tight and the book can fill the tiny size). Full rules: `RULES.md`.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill keys locally — never commit them
chmod +x kb
```

Pick a mode from a menu, or pass it on the command line:

```bash
./kb                 # menu: 1 scan / 2 once / 3 auth / 4 live / 5 env
./kb scan            # also: s  or  1
./kb scan --asset BTC
./kb once            # also: o  or  2   dry-run limit payloads
./kb auth            # also: a  or  3   test key + PEM (tries demo then prod)
./kb env             # also: e  or  5   show DEMO vs PROD
./kb env --prod      # write USE_DEMO=false to .env (live Kalshi)
./kb env --demo      # write USE_DEMO=true
./kb auth --prod
./kb live --prod
```

`python -m src.main …` and (after `pip install -e .`) `kalshibot` / `kb` do the same thing. No args on a TTY opens the menu; no args in a script defaults to `scan`.

A **live Kalshi key** (created on kalshi.com, not demo) returns 401 on demo. Use `--prod` for that key, or `USE_DEMO=false ./kb auth`. Live does **not** require editing `LIVE_TRADING` in `.env`. On a terminal, `./kb live` asks you to type `LIVE`. Scripts can use `--confirm LIVE`. GitHub Actions stays dry.

Exit codes: `0` success or `NO_ACTIONABLE_EDGE`, `2` config/auth, `3` rate limited.

If nothing passes filters the process prints `NO_ACTIONABLE_EDGE` and exits 0. It never uses mid as a fill — Yes is bought at `yes_ask`, No at `no_ask`. Live limits are `post_only`. If the book moved and that would take, it requotes one tick more passive instead of crossing.

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

GitHub Actions (`.github/workflows/hourly.yml`) runs `scan` at minute 3 of every hour. It does **not** place live orders. Kalshi often 403s cloud IPs. For live limits use a VPS or the Pi with a stable IP.

## On the Pi (`/home/KalshiBot`)

The repo root **is** the project. Clone straight into that folder so you do not get a nested `KalshiBot/KalshiBot`:

```bash
git clone https://github.com/mattypie314/KalshiBot.git /home/KalshiBot
```

If you already have `/home/mkubit/KalshiBot/KalshiBot` (or `/home/mkubit/KalshiBot`), move it up:

```bash
# stop anything using the old path first
sudo mv /home/mkubit/KalshiBot/KalshiBot /home/KalshiBot
# or, if it was already flattened under your home:
# sudo mv /home/mkubit/KalshiBot /home/KalshiBot
sudo chown -R mkubit:mkubit /home/KalshiBot
```

Then:

```bash
cd /home/KalshiBot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x kb
cp -n .env.example .env   # edit keys; never commit .env
nano .env                 # Ctrl+O then Enter to save, Ctrl+X to quit
./kb auth
```

A **401** on `./kb auth` or `./kb live` with `LIVE order failed auth` means a **live** Kalshi key was sent to the **demo** host. The PEM is fine. Point `.env` at prod, then type `LIVE` only if the prompt says `on PROD` (real money):

```bash
cd /home/KalshiBot
nano .env
# change USE_DEMO=true  →  USE_DEMO=false
# save: Ctrl+O, Enter, then Ctrl+X

# same thing without nano:
#   echo USE_DEMO=false >> .env

USE_DEMO=false ./kb auth    # want AUTH OK on external-api.kalshi.com
USE_DEMO=false ./kb live    # prompt must say on PROD, then type LIVE
```

After this branch is on the Pi, `./kb env --prod` writes that line for you.

`./kb` uses `/home/KalshiBot/.venv/bin/python` when that venv exists.

systemd units in `scripts/` use the venv and `WorkingDirectory=/home/KalshiBot`. The timer is **scan/dry-run only** (`once`). It does not place live orders:

```bash
sudo cp scripts/kalshi-hourly.service scripts/kalshi-hourly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-hourly.timer
```

## Bankroll caps

| Knob | Default | Env |
| --- | --- | --- |
| Bankroll | $40 | `BANKROLL` |
| Min net edge | 6% | `MIN_NET_EDGE` |
| Soft edge (tight book) | 4% | `SOFT_NET_EDGE` |
| Risk % cap | 5% | `MAX_RISK_PCT` |
| Preferred risk | $2.00 | `PREFERRED_RISK_DOLLARS` |
| Hard dollar cap | $3.00 | `MAX_RISK_DOLLARS` |
| Fractional Kelly | 0.25× | `KELLY_MULT` |

Settlement on these books uses CF Benchmarks RTI (60-second average), not a single exchange last tick. The model uses Coinbase/Binance spot as a **proxy** and says so in every report.

```bash
pytest
```

## Later

Full campaign desk (15m / maker / dashboard / sports research):

https://github.com/mattypie314/KalshiBot/tree/archive/campaign-desk
