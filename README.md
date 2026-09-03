# KalshiBot

**Halted until further notice.** Live orders are refused (`HALTED=true` by default). The GitHub hourly scan cron is off. On the Pi, stop the timer so a stale unit cannot fire:

```bash
sudo systemctl disable --now kalshi-hourly.timer
```

To resume later: set `HALTED=false`, restore the systemd `ExecStart` live line, then `sudo systemctl enable --now kalshi-hourly.timer`.

Hourly BTC and ETH threshold scanner for Kalshi (`KXBTCD`, `KXETHD`).

Pi operating manual (PDF): `docs/KalshiBot-operating-manual.pdf`. Rebuild with `python3 scripts/build_operating_manual.py`.

Not financial advice. You can lose the full amount you put on a contract. Demo first. `.env` can stay dry. Live is a one-run confirm: type `LIVE` at the prompt, or pass `--confirm LIVE` on a keyboard. Unattended live still needs both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES`.

The 15-minute campaign, maker loop, dashboard, and research desk are parked on the `archive/campaign-desk` branch for later.

## What this does

Scans the live hourly above/below books for Bitcoin and Ethereum. Fair probability comes from spot plus recent realized vol. It only prints or places a **limit** order when net edge after estimated fees clears the filter.

Bankroll default **$40**. Risk per idea **$1.50–$2.00**, preferred **$1.75**, hard cap **$2.00**. Maker / limit only. Sit unless net edge after fees is ≥ 6%. Ban close strikes (coin-flip fades inside ~0.5–0.75% of spot). Max 1 open hourly ticket. Full rules: `RULES.md`.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill keys locally — never commit them
chmod +x kb
```

Pick a mode from a menu, or pass it on the command line:

```bash
./kb                 # menu: 1 scan / 2 once / 3 auth / 4 live / 5 env / 6 eval
./kb scan            # also: s  or  1
./kb scan --asset BTC
./kb once            # also: o  or  2   dry-run limit payloads
./kb auth            # also: a  or  3   test key + PEM (tries demo then prod)
./kb env             # also: e  or  5   show DEMO vs PROD
./kb env --prod      # write USE_DEMO=false to .env (live Kalshi)
./kb env --demo      # write USE_DEMO=true
./kb auth --prod
./kb live --prod
./kb eval            # also: v  or  6   local journal / scan-log report
```

`python -m src.main …` and (after `pip install -e .`) `kalshibot` / `kb` do the same thing. No args on a TTY opens the menu; no args in a script defaults to `scan`.

A **live Kalshi key** (created on kalshi.com, not demo) returns 401 on demo. Use `--prod` for that key, or `USE_DEMO=false ./kb auth`. On a terminal, `./kb live` asks you to type `LIVE` (`.env` can stay dry). Unattended systemd / CI has no TTY: both `LIVE_TRADING=true` and `CONFIRM_LIVE=YES` are required, and `HALTED` still wins. GitHub Actions stays dry.

Exit codes: `0` success or `NO_ACTIONABLE_EDGE`, `2` config/auth, `3` rate limited.

If nothing passes filters the process prints `NO_ACTIONABLE_EDGE` and exits 0. It never uses mid as a fill — Yes is bought at `yes_ask`, No at `no_ask`. Live limits are `post_only`. If the book moved and that would take, it requotes one tick more passive instead of crossing. Report timestamps and settlements are **America/New_York** (EDT/EST).

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

GitHub Actions (`.github/workflows/hourly.yml`) is **halted** (no schedule). When re-enabled it only runs `scan` — it does **not** place live orders. Kalshi often 403s cloud IPs. For live limits use a VPS or the Pi with a stable IP.

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

To land in the repo (and activate the venv) when you SSH in, add one line to `~/.bashrc` on the Pi:

```bash
echo '. /home/KalshiBot/scripts/pi-shell.sh' >> ~/.bashrc
```

Then `source ~/.bashrc` or open a new SSH session. It only `cd`s if you started in your home directory, so it will not fight a directory you already chose.

systemd units in `scripts/` use the venv and `WorkingDirectory=/home/KalshiBot`. The timer is **halted**. Keep it off:

```bash
sudo systemctl disable --now kalshi-hourly.timer
```

When you resume: copy the units, set `HALTED=false`, restore `ExecStart` to `... -m src.main live --prod --confirm LIVE`, then `daemon-reload` and `enable --now`.

Logs: `journalctl -u kalshi-hourly.service -n 80 --no-pager`  
`~/.kalshi/env` may use bash `export KEY=value`. systemd cannot read that; the bot loads it in Python.

## Bankroll caps

| Knob | Default | Env |
| --- | --- | --- |
| Bankroll | $40 | `BANKROLL` |
| Min net edge | 6% | `MIN_NET_EDGE` |
| Soft edge (no longer a discount) | 6% | `SOFT_NET_EDGE` |
| Risk % cap | 5% | `MAX_RISK_PCT` |
| Preferred risk | $1.75 | `PREFERRED_RISK_DOLLARS` |
| Hard dollar cap | $2.00 | `MAX_RISK_DOLLARS` |
| Min fade distance | 0.50% | `MIN_STRIKE_DISTANCE_PCT` |
| Fractional Kelly | 0.25× | `KELLY_MULT` |
| Daily filled-loss sit | $4 or 2 losses | `MAX_DAILY_LOSS_DOLLARS` / `MAX_DAILY_LOSSES` |

Settlement is the 60-second average of CF Benchmarks BRTI (BTC) / ERTI (ETH). The bot prices off that index via Kalshi when the key can; Coinbase/Binance are fallbacks. Vol is still exchange-realized. A 2× typical vol day sits.

Each scan appends one line to `artifacts/scan_log.jsonl` (quotes, spots, ideas — no keys). Live tickets in `artifacts/trade_log.jsonl` stay pending until a fill is confirmed; unfilled rests are not scored as wins or losses.

```bash
pytest
./kb eval    # local journal + historical GitHub-scan replay; does not claim edge
```

## Later

Full campaign desk (15m / maker / dashboard / sports research):

https://github.com/mattypie314/KalshiBot/tree/archive/campaign-desk
