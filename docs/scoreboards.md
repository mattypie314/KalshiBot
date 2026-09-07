# Termius scoreboards (Pi)

Classic PLAY/SIT + pot-graph boards. **Paper and live never share a board.** Combined views tag every row `15m` vs `hourly` and show each bot’s pot/PnL plus a total.

No trading gates, Pass/Sit filters, or journal writers change when you open a board. Boards are **read-only** (they do not settle tickets or rewrite pots). Run `./kb15 eval` / `./kb eval` if you want the bookkeeping dump that settles.

## Aliases

Install once (either checkout; same scripts):

```bash
# after git pull on both /home/KalshiBot15 and /home/KalshiBot
/home/KalshiBot15/scripts/install-pi-scoreboards.sh
# or
/home/KalshiBot/scripts/install-pi-scoreboards.sh
```

That drops leftover files in `~/.local/bin` and symlinks:

| Command | Tape | Bot |
| --- | --- | --- |
| `score` / `kbscore` | paper | 15m (`fifteen_paper_log.jsonl`) |
| `livescore` / `kbscore-live` | live cash | 15m (`fifteen_trade_log.jsonl`) |
| `score-hourly` / `hscore` / `kbscore-hourly` | paper | hourly (`paper_log.jsonl`) |
| `livescore-hourly` / `hlivescore` / `kbscore-hourly-live` | live cash | hourly (`trade_log.jsonl`) |
| `scoreall` / `score-all` | paper | 15m + hourly |
| `livescore-all` | live cash | 15m + hourly |

Same boards from a checkout:

```bash
cd /home/KalshiBot15
./kb15 score          # 15m paper
./kb15 livescore      # 15m live
./kb15 eval           # bookkeeping dump (paper + live sections)

cd /home/KalshiBot
./kb score            # hourly paper
./kb livescore        # hourly live
./kb eval             # bookkeeping dump

python3 -m src.scoreboard scoreall --no-color
python3 -m src.scoreboard livescore-all --no-color
```

Wrappers set `FORCE_COLOR=1` for Termius. Use `NO_COLOR=1` or `--no-color` to strip ANSI.

If Termius cannot find `score` / `livescore`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

## Checkouts

| Bot | Pi path | Journals | Pot |
| --- | --- | --- | --- |
| 15m | `/home/KalshiBot15` | `artifacts/fifteen_paper_log.jsonl`, `artifacts/fifteen_trade_log.jsonl` | start $5 / ask $10 (`fifteen_pot.json` is display-only) |
| hourly | `/home/KalshiBot` | `artifacts/paper_log.jsonl`, `artifacts/trade_log.jsonl` | start `BANKROLL` (default $40) |

Combined boards read **both** trees. Override if you move them:

```bash
export KALSHIBOT15_ROOT=/home/KalshiBot15
export KALSHIBOT_ROOT=/home/KalshiBot
livescore-all
```

`pi-shell.sh` still `cd`s to `/home/KalshiBot` on login. That is why the wrappers pin the 15m root to `/home/KalshiBot15` instead of `$PWD`.

## What you see

- PLAY + SIT timeline (chop sits count as process wins, $0)
- Pending tickets in their own section
- ASCII pot sparkline + meter from play-only PnL
- Real `HALTED` / `LIVE_TRADING` / `CONFIRM_LIVE` from that checkout’s `.env`
- Combined: per-bot pot, W/L, PnL, and a **TOTAL** line; every timeline row tagged `15m` or `hourly`
- `kind=backfill` recon rows stay in the jsonl and are skipped on the board

Do not retune Pass/Sit from paper. Do not treat paper assumed fills as live cash.
