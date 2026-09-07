"""Combined LIVE Termius scoreboard for the 15m and hourly Pi bots.

Read-only. Never writes journals, pots, or .env. Never places orders.
Paper tapes (`paper_log.jsonl`, `fifteen_paper_log.jsonl`) are not opened.

Default Pi roots (override with env or flags):

- 15m:    /home/KalshiBot15  · fifteen_trade_log.jsonl + fifteen_pot.json
- hourly: /home/KalshiBot    · trade_log.jsonl + hourly_pot.json (optional)

Hourly has no in-repo pot writer. If `hourly_pot.json` is missing, the board
reconstructs cash as BANKROLL (default $40) plus filled live PnL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.clock import ET, format_et, parse_ts, same_et_day, to_et
from src.journal import counts_as_filled, load_trades

WIDTH = 76
TIMELINE_LIMIT = 24
SPARK_CHARS = "▁▂▃▄▅▆▇█"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

DEFAULT_HOURLY_ROOT = "/home/KalshiBot"
DEFAULT_FIFTEEN_ROOT = "/home/KalshiBot15"

FIFTEEN_JOURNAL = "fifteen_trade_log.jsonl"
FIFTEEN_POT = "fifteen_pot.json"
HOURLY_JOURNAL = "trade_log.jsonl"
HOURLY_POT_CANDIDATES = ("hourly_pot.json", "pot.json")

FIFTEEN_START_DEFAULT = 5.0
FIFTEEN_ASK_DEFAULT = 10.0
HOURLY_START_DEFAULT = 40.0

POT_BALANCE_KEYS = ("balance_usd", "balance", "bankroll", "equity_usd", "equity")
POT_START_KEYS = ("start_usd", "start", "starting_balance", "bankroll_start")
POT_ASK_KEYS = ("double_at", "ask_usd", "ask", "target")
POT_PNL_KEYS = ("realized_pnl", "realized_pnl_usd", "pnl")

BOT_15M = "15M"
BOT_1H = "1H"


@dataclass(frozen=True)
class PlayEvent:
    ts: datetime | None
    bot: str
    asset: str
    ticker: str
    side: str
    detail: str
    result: str
    pnl: float | None
    pending: bool


@dataclass
class BotSnapshot:
    label: str
    title: str
    journal_path: Path
    pot_path: Path | None
    pot_source: str
    pot: float
    start: float
    ask: float
    realized_pnl: float
    wins: int
    losses: int
    pending_n: int
    unfilled: int
    day_pnl: float
    streak: str
    equity: list[float]
    plays: list[PlayEvent] = field(default_factory=list)
    journal_missing: bool = False
    pot_file_missing: bool = False
    gates: dict[str, Any] = field(default_factory=dict)
    skipped_paper: int = 0
    skipped_other: int = 0


def use_color(*, stream: Any | None = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    out = stream if stream is not None else sys.stdout
    return bool(getattr(out, "isatty", lambda: False)())


def paint(text: str, code: str, enabled: bool) -> str:
    if not enabled or not code:
        return text
    return f"{code}{text}{RESET}"


def sparkline(values: Iterable[float], width: int = 28) -> str:
    series = [float(v) for v in values]
    if not series:
        return SPARK_CHARS[0]
    if len(series) > width:
        step = len(series) / width
        series = [series[min(len(series) - 1, int(i * step))] for i in range(width)]
    lo = min(series)
    hi = max(series)
    n = len(SPARK_CHARS) - 1
    if hi - lo < 1e-12:
        return SPARK_CHARS[0] * len(series)
    out: list[str] = []
    for value in series:
        idx = int(round((value - lo) / (hi - lo) * n))
        out.append(SPARK_CHARS[max(0, min(n, idx))])
    return "".join(out)


def meter(value: float, *, lo: float = 0.0, hi: float = 10.0, width: int = 22) -> str:
    span = hi - lo
    if span <= 0:
        filled = 0
    else:
        filled = int(round((float(value) - lo) / span * width))
    filled = max(0, min(width, filled))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def pot_curve(pnls: Iterable[float], *, start: float) -> list[float]:
    equity = [round(float(start), 4)]
    running = float(start)
    for pnl in pnls:
        running = round(running + float(pnl), 4)
        equity.append(running)
    return equity


def money(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if not signed:
        return f"${amount:.2f}"
    if amount > 0:
        return f"+${amount:.2f}"
    if amount < 0:
        return f"-${abs(amount):.2f}"
    return "$0.00"


def short_time(stamp: datetime | None, fallback: str = "—") -> str:
    if stamp is None:
        return fallback
    local = to_et(stamp)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M %p')}"


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _hr(title: str = "") -> str:
    if not title:
        return "-" * WIDTH
    body = f" {title} "
    pad = max(0, WIDTH - len(body) - 2)
    left = pad // 2
    right = pad - left
    return "-" * left + body + "-" * right


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env without mutating os.environ (each bot has its own file)."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
    return loaded


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def gate_status(env: dict[str, str]) -> dict[str, Any]:
    halted = _truthy(env.get("HALTED", "true"))
    live_on = _truthy(env.get("LIVE_TRADING", "false"))
    confirm = str(env.get("CONFIRM_LIVE") or "NO").strip().upper() or "NO"
    armed = (not halted) and live_on and confirm == "YES"
    if halted:
        label = "HALTED"
    elif armed:
        label = "LIVE"
    elif live_on:
        label = "CONFIRM"
    else:
        label = "OFF"
    return {
        "halted": halted,
        "live_trading": live_on,
        "confirm_live": confirm,
        "armed": armed,
        "label": label,
    }


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def load_pot_payload(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def find_hourly_pot(artifacts: Path) -> Path | None:
    for name in HOURLY_POT_CANDIDATES:
        candidate = artifacts / name
        if candidate.is_file():
            return candidate
    return None


def is_journal_backfill(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "").strip().lower()
    if kind == "backfill":
        return True
    source = str(row.get("spot_source") or "").strip().lower()
    return "backfill" in source


def is_paper_row(row: dict[str, Any]) -> bool:
    return str(row.get("kind") or "").strip().lower() == "paper"


def is_live_play_row(row: dict[str, Any] | None) -> bool:
    """True for a live place row. Paper, exits, and recon backfills are out."""
    if not isinstance(row, dict):
        return False
    if is_paper_row(row):
        return False
    if is_journal_backfill(row):
        return False
    if str(row.get("action") or "").strip().lower() == "exit":
        return False
    return True


def filter_live_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    live: list[dict[str, Any]] = []
    paper = 0
    other = 0
    for row in rows:
        if is_paper_row(row):
            paper += 1
            continue
        if not is_live_play_row(row):
            other += 1
            continue
        live.append(row)
    return live, paper, other


def _pnl_of(row: dict[str, Any]) -> float | None:
    raw = row.get("pnl")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def summarize_live(rows: list[dict[str, Any]]) -> dict[str, Any]:
    filled = settled_filled(rows)
    wins = [row for row in filled if row.get("result") == "win"]
    losses = [row for row in filled if row.get("result") == "loss"]
    unfilled = [row for row in rows if row.get("result") == "unfilled"]
    pending = [row for row in rows if row.get("result") not in {"win", "loss", "unfilled"}]
    pnl = sum(float(row.get("pnl") or 0) for row in filled)
    return {
        "n_rows": len(rows),
        "n_filled_settled": len(filled),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_unfilled": len(unfilled),
        "n_pending": len(pending),
        "pnl": round(pnl, 4),
    }


def settled_filled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("result") in {"win", "loss"} and counts_as_filled(row)
    ]


def day_filled_pnl(rows: list[dict[str, Any]], now: datetime | None = None) -> float:
    total = 0.0
    for row in settled_filled(rows):
        stamp = (
            row.get("resolved_ts_iso")
            or row.get("resolved_ts")
            or row.get("ts_iso")
            or row.get("ts")
        )
        if not same_et_day(stamp, now):
            continue
        pnl = _pnl_of(row)
        if pnl is not None:
            total += pnl
    return round(total, 4)


def win_loss_streak(rows: list[dict[str, Any]]) -> str:
    settled = settled_filled(rows)
    if not settled:
        return "—"
    last = str(settled[-1].get("result") or "")
    n = 0
    for row in reversed(settled):
        if str(row.get("result") or "") != last:
            break
        n += 1
    tag = "W" if last == "win" else "L"
    return f"{n}{tag}" if n else "—"


def play_events(rows: list[dict[str, Any]], *, bot: str) -> list[PlayEvent]:
    events: list[PlayEvent] = []
    for row in rows:
        result = str(row.get("result") or "pending").strip().lower()
        pending = result not in {"win", "loss", "unfilled"}
        stamp = parse_ts(
            row.get("ts_iso")
            or row.get("ts")
            or row.get("resolved_ts_iso")
            or row.get("resolved_ts")
        )
        ticker = str(row.get("ticker") or "")
        asset = str(row.get("asset") or "?")
        if asset == "?" and ticker.upper().startswith("KXETH"):
            asset = "ETH"
        elif asset == "?" and ticker.upper().startswith("KXBTC"):
            asset = "BTC"
        side = str(row.get("side") or "")
        limit = row.get("limit_price") or row.get("kalshi_price")
        try:
            limit_s = f"@{float(limit):.2f}" if limit not in (None, "") else ""
        except (TypeError, ValueError):
            limit_s = ""
        fill = str(row.get("fill_status") or "")
        pnl = _pnl_of(row)
        bits = [part for part in (side, limit_s) if part]
        if pending and fill:
            bits.append(fill)
        if pnl is not None and not pending:
            bits.append(money(pnl))
        mark = "pending" if pending else result
        events.append(
            PlayEvent(
                ts=stamp,
                bot=bot,
                asset=asset,
                ticker=ticker,
                side=side,
                detail=" ".join(bits),
                result=mark,
                pnl=pnl,
                pending=pending,
            )
        )
    return events


def resolve_pot(
    *,
    pot_path: Path | None,
    start_default: float,
    ask_default: float,
    filled_pnl: float,
    env_bankroll: float | None = None,
) -> tuple[float, float, float, float, str, bool]:
    """Return pot, start, ask, realized, source label, file_missing."""
    payload = load_pot_payload(pot_path) if pot_path else None
    start = float(env_bankroll) if env_bankroll is not None else float(start_default)
    ask = max(float(ask_default), start * 2.0)
    if payload:
        file_start = _first_float(payload, POT_START_KEYS)
        if file_start is not None:
            start = file_start
        file_ask = _first_float(payload, POT_ASK_KEYS)
        if file_ask is not None:
            ask = file_ask
        file_pnl = _first_float(payload, POT_PNL_KEYS)
        realized = file_pnl if file_pnl is not None else filled_pnl
        balance = _first_float(payload, POT_BALANCE_KEYS)
        if balance is not None:
            source = pot_path.name if pot_path else "pot"
            return float(balance), float(start), float(ask), float(realized), source, False
        reconstructed = round(float(start) + float(filled_pnl), 4)
        source = f"{pot_path.name} (no balance; start+pnl)" if pot_path else "start+pnl"
        return reconstructed, float(start), float(ask), float(realized), source, False
    reconstructed = round(float(start) + float(filled_pnl), 4)
    if env_bankroll is not None:
        source = "BANKROLL+live pnl"
    else:
        source = "start+live pnl"
    return reconstructed, float(start), float(max(ask, start * 2.0)), float(filled_pnl), source, True


def _env_float(env: dict[str, str], key: str) -> float | None:
    raw = env.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def snapshot_bot(
    *,
    label: str,
    title: str,
    journal_path: Path,
    pot_path: Path | None,
    env_path: Path,
    start_default: float,
    ask_default: float,
    bankroll_env_key: str | None = None,
    now: datetime | None = None,
) -> BotSnapshot:
    raw_rows = load_trades(journal_path) if journal_path.is_file() else []
    live_rows, skipped_paper, skipped_other = filter_live_rows(raw_rows)
    summary = summarize_live(live_rows)
    filled = settled_filled(live_rows)
    filled_pnl = round(sum(_pnl_of(row) or 0.0 for row in filled), 4)
    env = read_dotenv(env_path)
    env_bankroll = _env_float(env, bankroll_env_key) if bankroll_env_key else None
    pot, start, ask, realized, pot_source, pot_missing = resolve_pot(
        pot_path=pot_path,
        start_default=start_default,
        ask_default=ask_default,
        filled_pnl=filled_pnl,
        env_bankroll=env_bankroll,
    )
    plays = play_events(live_rows, bot=label)
    return BotSnapshot(
        label=label,
        title=title,
        journal_path=journal_path,
        pot_path=pot_path,
        pot_source=pot_source,
        pot=pot,
        start=start,
        ask=ask,
        realized_pnl=realized,
        wins=int(summary["n_wins"]),
        losses=int(summary["n_losses"]),
        pending_n=int(summary["n_pending"]),
        unfilled=int(summary["n_unfilled"]),
        day_pnl=day_filled_pnl(live_rows, now=now),
        streak=win_loss_streak(live_rows),
        equity=pot_curve((_pnl_of(row) or 0.0 for row in filled), start=start),
        plays=plays,
        journal_missing=not journal_path.is_file(),
        pot_file_missing=pot_missing,
        gates=gate_status(env),
        skipped_paper=skipped_paper,
        skipped_other=skipped_other,
    )


def default_hourly_root() -> Path:
    return Path(os.environ.get("KALSHIBOT_ROOT") or DEFAULT_HOURLY_ROOT)


def default_fifteen_root() -> Path:
    return Path(os.environ.get("KALSHIBOT15_ROOT") or DEFAULT_FIFTEEN_ROOT)


def load_snapshots(
    *,
    hourly_root: Path | None = None,
    fifteen_root: Path | None = None,
    hourly_journal: Path | None = None,
    fifteen_journal: Path | None = None,
    hourly_pot: Path | None = None,
    fifteen_pot: Path | None = None,
    now: datetime | None = None,
) -> tuple[BotSnapshot, BotSnapshot]:
    fifteen_root = Path(fifteen_root) if fifteen_root else default_fifteen_root()
    hourly_root = Path(hourly_root) if hourly_root else default_hourly_root()
    fifteen_art = fifteen_root / "artifacts"
    hourly_art = hourly_root / "artifacts"

    fifteen = snapshot_bot(
        label=BOT_15M,
        title="15-minute",
        journal_path=Path(fifteen_journal) if fifteen_journal else fifteen_art / FIFTEEN_JOURNAL,
        pot_path=Path(fifteen_pot) if fifteen_pot else fifteen_art / FIFTEEN_POT,
        env_path=fifteen_root / ".env",
        start_default=FIFTEEN_START_DEFAULT,
        ask_default=FIFTEEN_ASK_DEFAULT,
        bankroll_env_key="FIFTEEN_BANKROLL",
        now=now,
    )
    hourly_pot_path = Path(hourly_pot) if hourly_pot else find_hourly_pot(hourly_art)
    hourly = snapshot_bot(
        label=BOT_1H,
        title="hourly",
        journal_path=Path(hourly_journal) if hourly_journal else hourly_art / HOURLY_JOURNAL,
        pot_path=hourly_pot_path,
        env_path=hourly_root / ".env",
        start_default=HOURLY_START_DEFAULT,
        ask_default=HOURLY_START_DEFAULT * 2.0,
        bankroll_env_key="BANKROLL",
        now=now,
    )
    return fifteen, hourly


def _gate_color(label: str) -> str:
    if label == "LIVE":
        return GREEN
    if label == "HALTED":
        return RED
    if label == "CONFIRM":
        return YELLOW
    return DIM


def _badge(event: PlayEvent) -> str:
    if event.pending or event.result == "pending":
        return "PEND"
    if event.result == "win":
        return "W"
    if event.result == "loss":
        return "L"
    if event.result == "unfilled":
        return "—"
    return event.result.upper()[:4] or "—"


def _badge_color(badge: str) -> str:
    if badge == "W":
        return GREEN
    if badge == "L":
        return RED
    if badge == "PEND":
        return YELLOW
    return DIM


def _bot_color(bot: str) -> str:
    return MAGENTA if bot == BOT_15M else CYAN


def format_gate_line(gates: dict[str, Any], *, color: bool) -> str:
    if not gates:
        return "  Gates   (no .env)"
    halted = "true" if gates.get("halted") else "false"
    live = "true" if gates.get("live_trading") else "false"
    confirm = str(gates.get("confirm_live") or "NO")
    label = str(gates.get("label") or "OFF")
    arrow = paint(label, _gate_color(label) + BOLD, color)
    return (
        f"  Gates   HALTED={halted}  LIVE_TRADING={live}  "
        f"CONFIRM_LIVE={confirm}  →  {arrow}"
    )


def format_bot_block(bot: BotSnapshot, *, color: bool) -> list[str]:
    tag = paint(f"{bot.label:<3}", _bot_color(bot.label) + BOLD, color)
    spark = sparkline(bot.equity)
    bar = meter(bot.pot, lo=0.0, hi=max(bot.ask, bot.pot, 1.0))
    journal = bot.journal_path.name if bot.journal_path else "?"
    missing = []
    if bot.journal_missing:
        missing.append("no journal yet")
    if bot.pot_file_missing:
        missing.append(f"pot via {bot.pot_source}")
    else:
        missing.append(bot.pot_source)
    extra = " · ".join(missing)
    return [
        (
            f"  {tag}  pot {money(bot.pot, signed=False)}   start ${bot.start:.2f}   "
            f"{bot.wins}W-{bot.losses}L   pnl {money(bot.realized_pnl)}   "
            f"day {money(bot.day_pnl)}   streak {bot.streak}"
        ),
        f"       {spark}   ${bot.start:.2f} → ${bot.pot:.2f}",
        f"       {bar}  ${bot.pot:.2f} / ${bot.ask:.2f}",
        format_gate_line(bot.gates, color=color),
        f"       {journal}  ·  {extra}  ·  pending {bot.pending_n}  unfilled {bot.unfilled}",
    ]


def format_timeline_line(event: PlayEvent, *, color: bool) -> str:
    when = short_time(event.ts)
    kind = paint("PLAY", BOLD, color)
    bot = paint(f"{event.bot:<3}", _bot_color(event.bot) + BOLD, color)
    asset = f"{event.asset:<3}"
    badge = _badge(event)
    badge_s = paint(f"{badge:>4}", _badge_color(badge), color)
    extra = event.ticker or ""
    detail = event.detail
    if extra and extra not in detail:
        detail = f"{extra}  {detail}"
    body = _clip(detail, 42)
    return f"  {when:<9} {kind} {bot} {asset} {body:<40} {badge_s}"


def format_combined_scoreboard(
    fifteen: BotSnapshot,
    hourly: BotSnapshot,
    *,
    now: datetime | None = None,
    color: bool | None = None,
) -> str:
    enabled = use_color() if color is None else bool(color)
    now = to_et(now)
    total_pot = round(fifteen.pot + hourly.pot, 4)
    total_pnl = round(fifteen.realized_pnl + hourly.realized_pnl, 4)
    total_day = round(fifteen.day_pnl + hourly.day_pnl, 4)
    total_wins = fifteen.wins + hourly.wins
    total_losses = fifteen.losses + hourly.losses
    total_pending = fifteen.pending_n + hourly.pending_n
    combined_equity = [
        round(a + b, 4)
        for a, b in zip(
            _pad_equity(fifteen.equity, hourly.equity),
            _pad_equity(hourly.equity, fifteen.equity),
        )
    ]
    ask = max(fifteen.ask + hourly.ask, total_pot, 1.0)
    header = f"{'KB COMBINED LIVE SCOREBOARD':<40} {format_et(now)}"
    lines = [
        "",
        paint(f"  {header}", BOLD, enabled),
        "  " + "=" * (WIDTH - 2),
        "  LIVE cash only · paper tapes not opened · 15M vs 1H labeled",
        (
            f"  ALL   pot {money(total_pot, signed=False)}   "
            f"{total_wins}W-{total_losses}L   pnl {money(total_pnl)}   "
            f"day {money(total_day)}   pending {total_pending}"
        ),
        f"       {sparkline(combined_equity)}   "
        f"${fifteen.start + hourly.start:.2f} → ${total_pot:.2f}",
        f"       {meter(total_pot, lo=0.0, hi=ask)}  "
        f"${total_pot:.2f} / ${ask:.2f}",
        "",
        *format_bot_block(fifteen, color=enabled),
        "",
        *format_bot_block(hourly, color=enabled),
    ]

    pending = [event for event in (*fifteen.plays, *hourly.plays) if event.pending]
    pending.sort(key=lambda event: event.ts or datetime.min.replace(tzinfo=ET))
    lines.append("  " + _hr("PENDING"))
    if pending:
        for event in pending:
            mark = paint("!", YELLOW + BOLD, enabled)
            when = short_time(event.ts)
            ticker = event.ticker or "?"
            bot = paint(f"{event.bot:<3}", _bot_color(event.bot) + BOLD, enabled)
            lines.append(
                f"  {mark} {when:<9} {bot} {event.asset:<3} {event.side:<3} "
                f"{_clip(ticker + '  ' + event.detail, WIDTH - 28)}"
            )
        lines.append("    waiting on fill / official BRTI · ETHUSD_RTI settlement")
    else:
        lines.append("    none — no open live tickets waiting to settle")

    timeline = [event for event in (*fifteen.plays, *hourly.plays)]
    timeline.sort(
        key=lambda event: (
            event.ts or datetime.min.replace(tzinfo=ET),
            0 if event.bot == BOT_15M else 1,
            event.ticker,
        )
    )
    lines.append("  " + _hr("TIMELINE  PLAY  (15M magenta · 1H cyan)"))
    shown = timeline[-TIMELINE_LIMIT:]
    if len(timeline) > TIMELINE_LIMIT:
        lines.append(f"    … {len(timeline) - TIMELINE_LIMIT} older plays omitted")
    if not shown:
        lines.append("    no live PLAY rows yet")
    for event in shown:
        lines.append(format_timeline_line(event, color=enabled))

    skipped = fifteen.skipped_paper + hourly.skipped_paper
    lines.extend(
        [
            "  " + "-" * (WIDTH - 2),
            "  PLAY 15M = fifteen_trade_log.  PLAY 1H = hourly trade_log.",
            "  Paper journals are never mixed into this live combined board.",
        ]
    )
    if skipped:
        lines.append(f"  Skipped {skipped} paper row(s) that leaked into a live journal.")
    lines.append("")
    return "\n".join(lines)


def _pad_equity(series: list[float], other: list[float]) -> list[float]:
    if not series:
        series = [0.0]
    if len(series) >= len(other):
        return series
    return series + [series[-1]] * (len(other) - len(series))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Combined LIVE Termius scoreboard for 15m + hourly. "
            "Read-only. Does not open paper tapes or change trading gates."
        )
    )
    parser.add_argument(
        "--fifteen-root",
        default=None,
        help=f"15m checkout (default: $KALSHIBOT15_ROOT or {DEFAULT_FIFTEEN_ROOT})",
    )
    parser.add_argument(
        "--hourly-root",
        default=None,
        help=f"Hourly checkout (default: $KALSHIBOT_ROOT or {DEFAULT_HOURLY_ROOT})",
    )
    parser.add_argument("--fifteen-journal", default=None)
    parser.add_argument("--hourly-journal", default=None)
    parser.add_argument("--fifteen-pot", default=None)
    parser.add_argument("--hourly-pot", default=None)
    parser.add_argument("--no-color", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fifteen, hourly = load_snapshots(
        hourly_root=Path(args.hourly_root) if args.hourly_root else None,
        fifteen_root=Path(args.fifteen_root) if args.fifteen_root else None,
        hourly_journal=Path(args.hourly_journal) if args.hourly_journal else None,
        fifteen_journal=Path(args.fifteen_journal) if args.fifteen_journal else None,
        hourly_pot=Path(args.hourly_pot) if args.hourly_pot else None,
        fifteen_pot=Path(args.fifteen_pot) if args.fifteen_pot else None,
    )
    color = False if args.no_color else None
    print(format_combined_scoreboard(fifteen, hourly, color=color))
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
