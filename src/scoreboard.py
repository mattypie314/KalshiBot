"""Read-only Termius scoreboards for 15m and hourly (paper or live).

Classic PLAY/SIT + pot graph. Never writes journals or pots. Never mixes
paper rows into a live board or live fills into a paper board.

Pi wrappers in scripts/ symlink to ~/.local/bin. Combined boards read both
checkouts (/home/KalshiBot15 and /home/KalshiBot) and tag every row.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.clock import ET, format_et, hour_key, parse_ts, same_et_day, to_et
from src.evaluate import summarize_trades
from src.journal import counts_as_filled, load_trades
from src.paper import (
    FILL_SIT_UNSCORED,
    RESULT_LOSS,
    RESULT_PENDING,
    RESULT_SIT,
    RESULT_UNSCORED,
    RESULT_WIN,
    summarize_paper,
)

WIDTH = 80
TIMELINE_LIMIT = 24
SPARK_CHARS = "▁▂▃▄▅▆▇█"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

DEFAULT_FIFTEEN_ROOT = Path("/home/KalshiBot15")
DEFAULT_HOURLY_ROOT = Path("/home/KalshiBot")

FIFTEEN_LIVE_LOG = "fifteen_trade_log.jsonl"
FIFTEEN_PAPER_LOG = "fifteen_paper_log.jsonl"
FIFTEEN_SCAN_LOG = "fifteen_scan_log.jsonl"
FIFTEEN_POT_FILE = "fifteen_pot.json"
HOURLY_LIVE_LOG = "trade_log.jsonl"
HOURLY_PAPER_LOG = "paper_log.jsonl"
HOURLY_SCAN_LOG = "scan_log.jsonl"

LIVE_LOG_NAMES = frozenset({FIFTEEN_LIVE_LOG, HOURLY_LIVE_LOG})
PAPER_LOG_NAMES = frozenset({FIFTEEN_PAPER_LOG, HOURLY_PAPER_LOG})

SKIP_SIT_SNIPPETS = (
    "outside entry window",
    "already working",
    "no live kxbtc",
    "no live kxeth",
    "held back (one per asset",
    "no spot",
    "pass but size/room failed",
)

_SIT_RESULTS = {RESULT_SIT, RESULT_UNSCORED, "sit/unscored"}
_SCORED = {RESULT_WIN, RESULT_LOSS}


@dataclass(frozen=True)
class BoardEvent:
    ts: datetime | None
    kind: str  # PLAY | SIT
    bot: str  # 15m | hourly
    asset: str
    ticker: str
    side: str
    detail: str
    result: str
    pnl: float | None
    pending: bool
    chop: bool
    window_id: str
    source: str


@dataclass(frozen=True)
class BotTape:
    bot: str
    paper: bool
    root: Path
    journal_path: Path
    scan_path: Path
    pot_path: Path | None
    start: float
    ask: float
    gates: dict[str, Any]
    journal_rows: list[dict[str, Any]] = field(default_factory=list)
    scan_rows: list[dict[str, Any]] = field(default_factory=list)
    pot_file_balance: float | None = None
    pot_file_realized: float | None = None
    pot_file_stopped: bool | None = None


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


def parse_dotenv(path: Path) -> dict[str, str]:
    """KEY=value from a checkout .env. No exports into os.environ."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
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
        values[key] = value.strip().strip('"').strip("'")
    return values


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def gate_status(
    *,
    halted: bool,
    live_trading: bool,
    confirm_live: str,
) -> dict[str, Any]:
    confirm = str(confirm_live or "NO").strip().upper() or "NO"
    halted_on = bool(halted)
    live_on = bool(live_trading)
    armed = (not halted_on) and live_on and confirm == "YES"
    if halted_on:
        label = "HALTED"
    elif armed:
        label = "LIVE"
    elif live_on:
        label = "CONFIRM"
    else:
        label = "OFF"
    return {
        "halted": halted_on,
        "live_trading": live_on,
        "confirm_live": confirm,
        "armed": armed,
        "label": label,
    }


def gates_from_env(env: dict[str, str]) -> dict[str, Any]:
    return gate_status(
        halted=_truthy(env.get("HALTED", "true")),
        live_trading=_truthy(env.get("LIVE_TRADING", "false")),
        confirm_live=str(env.get("CONFIRM_LIVE") or "NO"),
    )


def sparkline(values: Iterable[float], width: int = 28) -> str:
    series = [float(v) for v in values]
    if not series:
        return ""
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


def asset_from_ticker(ticker: str) -> str:
    text = str(ticker or "").upper()
    if "ETH" in text:
        return "ETH"
    if "BTC" in text:
        return "BTC"
    return ""


def is_journal_backfill(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    kind = str(row.get("kind") or "").strip().lower()
    if kind == "backfill":
        return True
    if row.get("backfill") is True:
        return True
    return str(row.get("source") or "").strip().lower() == "backfill"


def scoreboard_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict) and not is_journal_backfill(row)]


def is_live_play_row(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    if is_journal_backfill(row):
        return False
    if str(row.get("kind") or "").strip().lower() == "paper":
        return False
    if str(row.get("action") or "").strip().lower() == "exit":
        return False
    return True


def is_pending_ticket(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict) or is_journal_backfill(row):
        return False
    result = str(row.get("result") or RESULT_PENDING).strip().lower()
    if result in {RESULT_WIN, RESULT_LOSS, "unfilled", RESULT_SIT, RESULT_UNSCORED}:
        return False
    fill_model = str(row.get("fill_model") or "").strip().lower()
    if fill_model == FILL_SIT_UNSCORED:
        return False
    return True


def classify_sit_note(note: str) -> str | None:
    text = str(note or "").strip()
    if not text:
        return None
    low = text.lower()
    if any(snippet in low for snippet in SKIP_SIT_SNIPPETS):
        return None
    if "chop veto" in low:
        return "chop"
    if "proxy" in low:
        return "proxy"
    if "news" in low:
        return "news"
    if "revenge" in low:
        return "revenge"
    if "session stopped" in low or "3 losses" in low:
        return "stopped"
    if "close strike" in low or "net edge" in low or "no actionable" in low:
        return "filter"
    return None


def _ticker_from_note(note: str) -> str:
    left = str(note or "").split(":", 1)[0].strip()
    if left.upper().startswith("KX"):
        return left
    return ""


def _asset_from_note(note: str, ticker: str) -> str:
    if ticker:
        return asset_from_ticker(ticker)
    upper = str(note or "").upper()
    if upper.startswith("ETH") or " ETH" in upper:
        return "ETH"
    if upper.startswith("BTC") or " BTC" in upper:
        return "BTC"
    return ""


def _window_id_for(stamp: datetime | None, explicit: object = None, *, bot: str) -> str:
    if explicit:
        return str(explicit)
    if stamp is None:
        return ""
    if bot == "hourly":
        return hour_key(stamp)
    minute = (to_et(stamp).minute // 15) * 15
    return to_et(stamp).replace(minute=minute, second=0, microsecond=0).isoformat()


def _short_time(stamp: datetime | None, fallback: str = "") -> str:
    if stamp is None:
        return fallback or "—"
    local = to_et(stamp)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{local.strftime('%M %p')}"


def _money(value: float | None, *, signed: bool = True) -> str:
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


def _pnl_of(row: dict[str, Any]) -> float | None:
    raw = row.get("pnl")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _float_env(env: dict[str, str], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        raw = env.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return default


def _path_from_env(root: Path, env: dict[str, str], keys: tuple[str, ...], default: Path) -> Path:
    for key in keys:
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = root / path
        return path
    return default


def assert_tape_path(path: Path, *, paper: bool) -> Path:
    name = path.name
    if paper and name in LIVE_LOG_NAMES:
        raise ValueError(f"paper board must not read live journal {name}")
    if (not paper) and name in PAPER_LOG_NAMES:
        raise ValueError(f"live board must not read paper journal {name}")
    return path


def play_events_from_journal(
    rows: list[dict[str, Any]],
    *,
    paper: bool,
    bot: str,
) -> list[BoardEvent]:
    events: list[BoardEvent] = []
    source_rows = scoreboard_rows(rows)
    if not paper:
        source_rows = [row for row in source_rows if is_live_play_row(row)]
    for row in source_rows:
        result = str(row.get("result") or RESULT_PENDING).strip().lower()
        fill_model = str(row.get("fill_model") or "").strip().lower()
        sit = result in _SIT_RESULTS or fill_model == FILL_SIT_UNSCORED
        pending = is_pending_ticket(row)
        stamp = parse_ts(
            row.get("ts_iso")
            or row.get("ts")
            or row.get("resolved_ts_iso")
            or row.get("resolved_ts")
        )
        ticker = str(row.get("ticker") or "")
        asset = str(row.get("asset") or asset_from_ticker(ticker) or "?")
        side = str(row.get("side") or "")
        limit = row.get("limit_price") or row.get("kalshi_price")
        try:
            limit_s = f"@{float(limit):.2f}" if limit not in (None, "") else ""
        except (TypeError, ValueError):
            limit_s = ""
        fill = str(row.get("fill_status") or row.get("fill_model") or "")
        pnl = _pnl_of(row)
        if sit:
            kind = "SIT"
            detail = str(row.get("note") or "sit/unscored")
            mark = "sit"
        else:
            kind = "PLAY"
            bits = [part for part in (side, limit_s, fill) if part]
            if pnl is not None and not pending:
                bits.append(_money(pnl))
            detail = " ".join(bits)
            mark = "pending" if pending else result
        events.append(
            BoardEvent(
                ts=stamp,
                kind=kind,
                bot=bot,
                asset=asset,
                ticker=ticker,
                side=side,
                detail=detail,
                result=mark,
                pnl=pnl,
                pending=pending,
                chop=False,
                window_id=_window_id_for(stamp, row.get("window_id"), bot=bot),
                source="journal",
            )
        )
    return events


def sit_events_from_scans(scan_rows: list[dict[str, Any]], *, bot: str) -> list[BoardEvent]:
    events: list[BoardEvent] = []
    seen: set[tuple[str, str, str]] = set()
    for row in scan_rows:
        if not isinstance(row, dict):
            continue
        stamp = parse_ts(row.get("ts") or row.get("ts_iso"))
        window = str(row.get("window_id") or _window_id_for(stamp, bot=bot))
        notes: list[str] = []
        for note in row.get("notes") or []:
            notes.append(str(note))
        for note in row.get("nearby") or []:
            notes.append(str(note))
        if not notes and not (row.get("ideas") or []):
            notes.append("no actionable edge")
        for note in notes:
            kind = classify_sit_note(note)
            if kind is None and note != "no actionable edge":
                if bot == "hourly" and not (row.get("ideas") or []):
                    kind = "filter"
                else:
                    continue
            if kind is None:
                if note == "no actionable edge":
                    kind = "filter"
                else:
                    continue
            ticker = _ticker_from_note(note)
            asset = _asset_from_note(note, ticker)
            key = (window, ticker or asset or note[:24], kind)
            if key in seen:
                continue
            seen.add(key)
            chop = kind == "chop"
            events.append(
                BoardEvent(
                    ts=stamp,
                    kind="SIT",
                    bot=bot,
                    asset=asset or "?",
                    ticker=ticker,
                    side="",
                    detail=note,
                    result="chop" if chop else "sit",
                    pnl=0.0 if chop else None,
                    pending=False,
                    chop=chop,
                    window_id=window,
                    source="scan",
                )
            )
    return events


def merge_timeline(plays: list[BoardEvent], sits: list[BoardEvent]) -> list[BoardEvent]:
    play_tickers = {(event.window_id, event.ticker, event.bot) for event in plays if event.ticker}
    merged: list[BoardEvent] = list(plays)
    for sit in sits:
        if sit.ticker and (sit.window_id, sit.ticker, sit.bot) in play_tickers:
            continue
        merged.append(sit)
    merged.sort(
        key=lambda event: (
            event.ts or datetime.min.replace(tzinfo=ET),
            0 if event.kind == "PLAY" else 1,
            event.bot,
            event.ticker,
        )
    )
    return merged


def _settled_play_rows(rows: list[dict[str, Any]], *, paper: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in scoreboard_rows(rows):
        if not paper and not is_live_play_row(row):
            continue
        if str(row.get("result") or "") not in _SCORED:
            continue
        if paper and str(row.get("fill_model") or "") not in {"", "assumed-maker-fill"}:
            if str(row.get("fill_model") or "") != "assumed-maker-fill":
                continue
        if (not paper) and not counts_as_filled(row):
            continue
        out.append(row)
    return out


def play_pot_equity(rows: list[dict[str, Any]], *, start: float, paper: bool) -> float:
    total = float(start)
    for row in _settled_play_rows(rows, paper=paper):
        try:
            total += float(row.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def streak(rows: list[dict[str, Any]], *, paper: bool) -> dict[str, Any]:
    settled = _settled_play_rows(rows, paper=paper)
    if not settled:
        return {"result": None, "n": 0}
    last = str(settled[-1].get("result") or "")
    n = 0
    for row in reversed(settled):
        if str(row.get("result") or "") != last:
            break
        n += 1
    return {"result": last, "n": n}


def day_pnl(rows: list[dict[str, Any]], *, paper: bool, now: datetime | None = None) -> float:
    total = 0.0
    for row in _settled_play_rows(rows, paper=paper):
        stamp = row.get("resolved_ts_iso") or row.get("resolved_ts") or row.get("ts_iso") or row.get("ts")
        if not same_et_day(stamp, now):
            continue
        try:
            total += float(row.get("pnl") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def _result_badge(event: BoardEvent) -> str:
    if event.chop or event.result == "chop":
        return "W"
    if event.pending or event.result == "pending":
        return "PEND"
    if event.result == "win":
        return "W"
    if event.result == "loss":
        return "L"
    if event.result == "unfilled":
        return "—"
    if event.kind == "SIT":
        return "SIT"
    return event.result.upper()[:4] or "—"


def _badge_color(badge: str) -> str:
    if badge == "W":
        return GREEN
    if badge == "L":
        return RED
    if badge == "PEND":
        return YELLOW
    if badge == "SIT":
        return CYAN
    return DIM


def _gate_color(label: str) -> str:
    if label == "LIVE":
        return GREEN
    if label == "HALTED":
        return RED
    if label == "CONFIRM":
        return YELLOW
    return DIM


def _hr(title: str = "", width: int = WIDTH) -> str:
    if not title:
        return "-" * width
    body = f" {title} "
    pad = max(0, width - len(body) - 2)
    left = pad // 2
    right = pad - left
    return "-" * left + body + "-" * right


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def format_gate_line(gates: dict[str, Any], *, color: bool, label: str = "") -> str:
    halted = "true" if gates["halted"] else "false"
    live = "true" if gates["live_trading"] else "false"
    confirm = str(gates["confirm_live"])
    arrow = paint(str(gates["label"]), _gate_color(str(gates["label"])) + BOLD, color)
    prefix = f"  {label:<7} " if label else "  Gates   "
    return (
        f"{prefix}HALTED={halted}  LIVE_TRADING={live}  "
        f"CONFIRM_LIVE={confirm}  →  {arrow}"
    )


def format_timeline_line(event: BoardEvent, *, color: bool, show_bot: bool) -> str:
    when = _short_time(event.ts)
    kind = paint(f"{event.kind:<4}", CYAN if event.kind == "SIT" else BOLD, color)
    asset = f"{event.asset:<3}"
    badge = _result_badge(event)
    badge_s = paint(f"{badge:>4}", _badge_color(badge), color)
    extra = event.ticker or ""
    detail = event.detail
    if event.chop:
        detail = "chop veto (process win, $0)"
        if extra:
            detail = f"{extra}  {detail}"
    elif extra and extra not in detail:
        detail = f"{extra}  {detail}"
    body_w = 40 if show_bot else 46
    body = _clip(detail, body_w)
    bot = f"{event.bot:<6} " if show_bot else ""
    return f"  {when:<9} {bot}{kind} {asset} {body:<{body_w}} {badge_s}"


def _load_pot_file(path: Path | None) -> tuple[float | None, float | None, bool | None]:
    if path is None or not path.is_file():
        return None, None, None
    try:
        import json

        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None, None
    if not isinstance(raw, dict):
        return None, None, None
    try:
        balance = float(raw.get("balance")) if raw.get("balance") is not None else None
    except (TypeError, ValueError):
        balance = None
    try:
        realized = float(raw.get("realized_pnl")) if raw.get("realized_pnl") is not None else None
    except (TypeError, ValueError):
        realized = None
    stopped = raw.get("stopped")
    return balance, realized, bool(stopped) if stopped is not None else None


def load_bot_tape(
    *,
    bot: str,
    paper: bool,
    root: Path,
    env: dict[str, str] | None = None,
) -> BotTape:
    if bot not in {"15m", "hourly"}:
        raise ValueError(f"unknown bot {bot}")
    root = Path(root)
    env = env if env is not None else parse_dotenv(root / ".env")
    artifacts = root / "artifacts"
    if bot == "15m":
        journal = _path_from_env(
            root,
            env,
            ("FIFTEEN_PAPER_LOG_PATH", "PAPER_LOG_PATH") if paper else ("FIFTEEN_TRADE_LOG_PATH", "TRADE_LOG_PATH"),
            artifacts / (FIFTEEN_PAPER_LOG if paper else FIFTEEN_LIVE_LOG),
        )
        scan = _path_from_env(
            root,
            env,
            ("FIFTEEN_SCAN_LOG_PATH", "SCAN_LOG_PATH"),
            artifacts / FIFTEEN_SCAN_LOG,
        )
        pot_path = _path_from_env(root, env, ("FIFTEEN_POT_PATH",), artifacts / FIFTEEN_POT_FILE)
        start = _float_env(env, ("FIFTEEN_POT_START", "POT_START", "FIFTEEN_BANKROLL"), 5.0)
        ask = _float_env(env, ("FIFTEEN_POT_DOUBLE", "POT_DOUBLE"), 10.0)
    else:
        journal = _path_from_env(
            root,
            env,
            ("PAPER_LOG_PATH",) if paper else ("TRADE_LOG_PATH",),
            artifacts / (HOURLY_PAPER_LOG if paper else HOURLY_LIVE_LOG),
        )
        scan = _path_from_env(root, env, ("SCAN_LOG_PATH",), artifacts / HOURLY_SCAN_LOG)
        pot_path = None
        start = _float_env(env, ("BANKROLL",), 40.0)
        # Meter room only — hourly has no $10 "ask Matt" pot. 2× bankroll.
        ask = start * 2.0
    assert_tape_path(journal, paper=paper)
    balance, realized, stopped = _load_pot_file(pot_path)
    return BotTape(
        bot=bot,
        paper=paper,
        root=root,
        journal_path=journal,
        scan_path=scan,
        pot_path=pot_path,
        start=start,
        ask=ask,
        gates=gates_from_env(env),
        journal_rows=load_trades(journal),
        scan_rows=load_trades(scan),
        pot_file_balance=balance,
        pot_file_realized=realized,
        pot_file_stopped=stopped,
    )


@dataclass
class TapeStats:
    wins: int
    losses: int
    pnl: float
    pending: int
    day: float
    streak: dict[str, Any]
    pot_now: float
    equity: list[float]
    n_backfills: int
    n_journal: int
    chop_sits: int
    journal_sits: int
    plays: list[BoardEvent]
    sits: list[BoardEvent]
    timeline: list[BoardEvent]


def stats_for_tape(tape: BotTape, *, now: datetime | None = None) -> TapeStats:
    plays = play_events_from_journal(tape.journal_rows, paper=tape.paper, bot=tape.bot)
    sits = sit_events_from_scans(tape.scan_rows, bot=tape.bot)
    timeline = merge_timeline(plays, sits)
    chop_sits = [event for event in sits if event.chop]
    journal_sits = [event for event in plays if event.kind == "SIT"]
    n_backfills = sum(1 for row in tape.journal_rows if is_journal_backfill(row))
    if tape.paper:
        summary = summarize_paper(scoreboard_rows(tape.journal_rows))
        wins = int(summary["n_wins"])
        losses = int(summary["n_losses"])
        pnl = float(summary["assumed_fill_pnl"])
        n_pending = int(summary["n_pending"])
    else:
        live_rows = [row for row in scoreboard_rows(tape.journal_rows) if is_live_play_row(row)]
        live = summarize_trades(live_rows)
        wins = int(live["n_wins"])
        losses = int(live["n_losses"])
        pnl = float(live["pnl"])
        n_pending = int(live["n_pending"])
    settled = _settled_play_rows(tape.journal_rows, paper=tape.paper)
    settled_pnls = []
    for row in settled:
        try:
            settled_pnls.append(float(row.get("pnl") or 0))
        except (TypeError, ValueError):
            settled_pnls.append(0.0)
    equity = pot_curve(settled_pnls, start=tape.start)
    return TapeStats(
        wins=wins,
        losses=losses,
        pnl=pnl,
        pending=n_pending,
        day=day_pnl(tape.journal_rows, paper=tape.paper, now=now),
        streak=streak(tape.journal_rows, paper=tape.paper),
        pot_now=play_pot_equity(tape.journal_rows, start=tape.start, paper=tape.paper),
        equity=equity,
        n_backfills=n_backfills,
        n_journal=len(tape.journal_rows),
        chop_sits=len(chop_sits),
        journal_sits=len(journal_sits),
        plays=plays,
        sits=sits,
        timeline=timeline,
    )


def _streak_s(slot: dict[str, Any]) -> str:
    if slot.get("n"):
        tag = "W" if slot.get("result") == "win" else "L"
        return f"{slot['n']}{tag}"
    return "—"


def format_single_board(
    tape: BotTape,
    *,
    now: datetime | None = None,
    color: bool | None = None,
) -> str:
    enabled = use_color() if color is None else bool(color)
    now = to_et(now)
    stats = stats_for_tape(tape, now=now)
    mode = "PAPER" if tape.paper else "LIVE"
    bot = "15m" if tape.bot == "15m" else "HOURLY"
    title = f"KB {bot} {mode} SCOREBOARD"
    if tape.paper:
        tape_line = (
            f"PAPER tape · assumed maker fills · not live cash · {tape.journal_path.name}"
        )
    else:
        tape_line = f"LIVE tape · real money · {tape.journal_path.name} · not paper"
    spark = sparkline(stats.equity) or SPARK_CHARS[0]
    bar = meter(stats.pot_now, lo=0.0, hi=max(tape.ask, tape.start, 1.0))
    header = f"{title:<42} {format_et(now)}"
    lines = [
        "",
        paint(f"  {header}", BOLD, enabled),
        "  " + "=" * (WIDTH - 2),
        f"  {tape_line}",
        format_gate_line(tape.gates, color=enabled),
        (
            f"  Pot     {_money(stats.pot_now, signed=False)}   start ${tape.start:.2f}   "
            + (
                f"ask ${tape.ask:.2f}"
                if tape.bot == "15m"
                else f"bankroll ${tape.start:.2f}   meter to ${tape.ask:.2f}"
            )
        ),
        f"          {spark}   ${tape.start:.2f} → ${stats.pot_now:.2f}",
        f"          {bar}  ${stats.pot_now:.2f} / ${tape.ask:.2f}",
        (
            f"  Plays   {stats.wins}W-{stats.losses}L   pnl {_money(stats.pnl)}   "
            f"day {_money(stats.day)}   streak {_streak_s(stats.streak)}   "
            f"pending {stats.pending}"
        ),
        (
            f"  Sits    {stats.chop_sits} chop (count as W, $0)   ·   "
            f"{stats.journal_sits} journal sit/unscored"
        ),
    ]
    if (not tape.paper) and tape.pot_file_balance is not None:
        lines.append(
            f"  Pot file ${_money(tape.pot_file_balance, signed=False)[1:]} "
            f"realized {_money(tape.pot_file_realized)} "
            f"stopped={tape.pot_file_stopped}  "
            f"(play-only equity ${stats.pot_now:.2f})"
        )
    if stats.n_backfills:
        lines.append(
            f"  Journal {stats.n_journal} rows  ·  skipped {stats.n_backfills} kind=backfill recon"
        )
    else:
        lines.append(f"  Journal {stats.n_journal} rows  ·  no backfill recon rows")
    lines.extend(_pending_and_timeline(stats, color=enabled, show_bot=False))
    lines.extend(
        [
            "  " + "-" * (WIDTH - 2),
            "  PLAY = took a ticket.  SIT = sat out.  chop sit = process win (no $).",
            "  Paper and live never share a board. Backfills stay in the jsonl.",
            "",
        ]
    )
    return "\n".join(lines)


def format_combined_board(
    tapes: list[BotTape],
    *,
    paper: bool,
    now: datetime | None = None,
    color: bool | None = None,
) -> str:
    enabled = use_color() if color is None else bool(color)
    now = to_et(now)
    mode = "PAPER" if paper else "LIVE"
    title = f"KB COMBINED {mode} SCOREBOARD"
    tape_line = (
        "PAPER tape · 15m + hourly assumed fills · not live cash"
        if paper
        else "LIVE tape · 15m + hourly real money · not paper"
    )
    header = f"{title:<42} {format_et(now)}"
    lines = [
        "",
        paint(f"  {header}", BOLD, enabled),
        "  " + "=" * (WIDTH - 2),
        f"  {tape_line}",
    ]
    stats_by_bot: dict[str, TapeStats] = {}
    total_start = 0.0
    total_pot = 0.0
    total_pnl = 0.0
    total_wins = 0
    total_losses = 0
    total_pending = 0
    all_plays: list[BoardEvent] = []
    all_sits: list[BoardEvent] = []
    for tape in tapes:
        if tape.paper != paper:
            raise ValueError("combined board refused mixed paper/live tapes")
        stats = stats_for_tape(tape, now=now)
        stats_by_bot[tape.bot] = stats
        total_start += tape.start
        total_pot += stats.pot_now
        total_pnl += stats.pnl
        total_wins += stats.wins
        total_losses += stats.losses
        total_pending += stats.pending
        all_plays.extend(stats.plays)
        all_sits.extend(stats.sits)
        lines.append(format_gate_line(tape.gates, color=enabled, label=tape.bot))
        spark = sparkline(stats.equity) or SPARK_CHARS[0]
        lines.append(
            f"  {tape.bot:<7} pot {_money(stats.pot_now, signed=False)}   "
            f"{stats.wins}W-{stats.losses}L   pnl {_money(stats.pnl)}   "
            f"day {_money(stats.day)}   pending {stats.pending}"
        )
        lines.append(f"          {spark}   ${tape.start:.2f} → ${stats.pot_now:.2f}")
        if not tape.root.is_dir():
            lines.append(f"          checkout missing: {tape.root}")
        else:
            lines.append(f"          {tape.journal_path}")
    lines.append(
        f"  TOTAL   pot {_money(total_pot, signed=False)}   "
        f"{total_wins}W-{total_losses}L   pnl {_money(total_pnl)}   "
        f"pending {total_pending}   (starts ${total_start:.2f})"
    )
    combined = TapeStats(
        wins=total_wins,
        losses=total_losses,
        pnl=round(total_pnl, 4),
        pending=total_pending,
        day=round(sum(stats_by_bot[bot].day for bot in stats_by_bot), 4),
        streak={"result": None, "n": 0},
        pot_now=round(total_pot, 4),
        equity=[],
        n_backfills=sum(stats_by_bot[bot].n_backfills for bot in stats_by_bot),
        n_journal=sum(stats_by_bot[bot].n_journal for bot in stats_by_bot),
        chop_sits=sum(stats_by_bot[bot].chop_sits for bot in stats_by_bot),
        journal_sits=sum(stats_by_bot[bot].journal_sits for bot in stats_by_bot),
        plays=all_plays,
        sits=all_sits,
        timeline=merge_timeline(all_plays, all_sits),
    )
    lines.append(
        f"  Sits    {combined.chop_sits} chop (count as W, $0)   ·   "
        f"{combined.journal_sits} journal sit/unscored"
    )
    if combined.n_backfills:
        lines.append(
            f"  Journal {combined.n_journal} rows  ·  skipped {combined.n_backfills} kind=backfill recon"
        )
    lines.extend(_pending_and_timeline(combined, color=enabled, show_bot=True))
    lines.extend(
        [
            "  " + "-" * (WIDTH - 2),
            "  Rows tagged 15m vs hourly. Totals are the sum of each bot's pot/PnL.",
            "  Paper and live never share a board.",
            "",
        ]
    )
    return "\n".join(lines)


def _pending_and_timeline(stats: TapeStats, *, color: bool, show_bot: bool) -> list[str]:
    pending = [event for event in stats.plays if event.pending]
    lines = ["  " + _hr("PENDING")]
    if pending:
        for event in pending:
            mark = paint("!", YELLOW + BOLD, color)
            when = _short_time(event.ts)
            bot = f"{event.bot:<6} " if show_bot else ""
            ticker = event.ticker or "?"
            lines.append(
                f"  {mark} {when:<9} {bot}{event.asset:<3} {event.side:<3} "
                f"{_clip(ticker + '  ' + event.detail, WIDTH - 28)}"
            )
        lines.append("    waiting on the official BRTI / ETHUSD_RTI settlement print")
    else:
        lines.append("    none — no open assumed/live tickets waiting to settle")
    lines.append("  " + _hr("TIMELINE  PLAY + SIT  (chop sits are wins)"))
    shown = stats.timeline[-TIMELINE_LIMIT:]
    if len(stats.timeline) > TIMELINE_LIMIT:
        lines.append(f"    … {len(stats.timeline) - TIMELINE_LIMIT} older events omitted")
    if not shown:
        lines.append("    no PLAY or notable SIT events yet")
    for event in shown:
        lines.append(format_timeline_line(event, color=color, show_bot=show_bot))
    return lines


def default_fifteen_root() -> Path:
    raw = os.environ.get("KALSHIBOT15_ROOT") or os.environ.get("SCOREBOARD_FIFTEEN_ROOT")
    if raw:
        return Path(os.path.expanduser(raw))
    if DEFAULT_FIFTEEN_ROOT.is_dir():
        return DEFAULT_FIFTEEN_ROOT
    cwd = Path.cwd()
    if (cwd / "kb15").is_file():
        return cwd
    return DEFAULT_FIFTEEN_ROOT


def default_hourly_root() -> Path:
    raw = os.environ.get("KALSHIBOT_ROOT") or os.environ.get("SCOREBOARD_HOURLY_ROOT")
    if raw:
        return Path(os.path.expanduser(raw))
    if DEFAULT_HOURLY_ROOT.is_dir():
        return DEFAULT_HOURLY_ROOT
    cwd = Path.cwd()
    if (cwd / "kb").is_file() and (cwd / "src" / "main.py").is_file():
        return cwd
    return DEFAULT_HOURLY_ROOT


def resolve_board(
    name: str,
    *,
    fifteen_root: Path | None = None,
    hourly_root: Path | None = None,
) -> tuple[str, bool, list[BotTape]]:
    """Map a command name to (title_kind, paper, tapes)."""
    fifteen_root = Path(fifteen_root) if fifteen_root else default_fifteen_root()
    hourly_root = Path(hourly_root) if hourly_root else default_hourly_root()
    mapping = {
        "score": ("15m", True, [load_bot_tape(bot="15m", paper=True, root=fifteen_root)]),
        "livescore": ("15m", False, [load_bot_tape(bot="15m", paper=False, root=fifteen_root)]),
        "score-hourly": ("hourly", True, [load_bot_tape(bot="hourly", paper=True, root=hourly_root)]),
        "livescore-hourly": ("hourly", False, [load_bot_tape(bot="hourly", paper=False, root=hourly_root)]),
        "scoreall": (
            "combined",
            True,
            [
                load_bot_tape(bot="15m", paper=True, root=fifteen_root),
                load_bot_tape(bot="hourly", paper=True, root=hourly_root),
            ],
        ),
        "livescore-all": (
            "combined",
            False,
            [
                load_bot_tape(bot="15m", paper=False, root=fifteen_root),
                load_bot_tape(bot="hourly", paper=False, root=hourly_root),
            ],
        ),
    }
    aliases = {
        "kbscore": "score",
        "kbscore-live": "livescore",
        "kbscore-hourly": "score-hourly",
        "kbscore-hourly-live": "livescore-hourly",
        "hscore": "score-hourly",
        "hlivescore": "livescore-hourly",
        "score-all": "scoreall",
        "livescoreall": "livescore-all",
        "fifteen-paper": "score",
        "fifteen-live": "livescore",
        "hourly-paper": "score-hourly",
        "hourly-live": "livescore-hourly",
        "paper-all": "scoreall",
        "live-all": "livescore-all",
        "combined-paper": "scoreall",
        "combined-live": "livescore-all",
    }
    key = aliases.get(name, name)
    if key not in mapping:
        raise ValueError(f"unknown scoreboard {name}")
    return mapping[key]


def render_board(
    name: str,
    *,
    fifteen_root: Path | None = None,
    hourly_root: Path | None = None,
    now: datetime | None = None,
    color: bool | None = None,
) -> str:
    kind, paper, tapes = resolve_board(name, fifteen_root=fifteen_root, hourly_root=hourly_root)
    if kind == "combined":
        return format_combined_board(tapes, paper=paper, now=now, color=color)
    return format_single_board(tapes[0], now=now, color=color)


def run_board(name: str, **kwargs: Any) -> int:
    print(render_board(name, **kwargs))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Termius PLAY/SIT scoreboards. Paper and live never mix."
    )
    parser.add_argument(
        "board",
        nargs="?",
        default="score",
        help=(
            "score | livescore | score-hourly | livescore-hourly | "
            "scoreall | livescore-all"
        ),
    )
    parser.add_argument("--fifteen-root", default=None)
    parser.add_argument("--hourly-root", default=None)
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    color = False if args.no_color else None
    try:
        return run_board(
            args.board,
            fifteen_root=Path(args.fifteen_root) if args.fifteen_root else None,
            hourly_root=Path(args.hourly_root) if args.hourly_root else None,
            color=color,
        )
    except ValueError as exc:
        print(f"scoreboard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
