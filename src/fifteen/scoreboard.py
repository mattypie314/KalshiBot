"""Termius ASCII scoreboards for 15m paper and live journals.

Read-only. Pi wrappers (`kbscore`, `kbscore-live`) call these entrypoints so
the board uses `scoreboard_rows` / `play_pot_equity` and never rewrites
`fifteen_trade_log.jsonl` or `fifteen_pot.json`. `kind=backfill` recon rows
stay labeled in the log (PR #65) and never score as fake $0 losses.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.clock import ET, format_et, parse_ts, same_et_day, to_et
from src.evaluate import summarize_trades
from src.fifteen.config import EXIT_OK, FifteenSettings
from src.fifteen.edge import fifteen_window_id
from src.fifteen.pot import DEFAULT_POT_DOUBLE, DEFAULT_POT_START, FifteenPot, load_pot
from src.fifteen.regime import CHOP_VETO_PHRASE
from src.journal import (
    asset_from_ticker,
    day_filled_pnl,
    filled_settled_score_rows,
    is_journal_backfill,
    load_trades,
    play_pot_equity,
    scoreboard_rows,
    win_loss_streak,
)
from src.paper import (
    FILL_SIT_UNSCORED,
    RESULT_LOSS,
    RESULT_PENDING,
    RESULT_SIT,
    RESULT_UNSCORED,
    RESULT_WIN,
    summarize_paper,
)

_SIT_RESULTS = {RESULT_SIT, RESULT_UNSCORED, "sit/unscored"}

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

SKIP_SIT_SNIPPETS = (
    "outside entry window",
    "already working",
    "no live kxbtc",
    "no live kxeth",
    "held back (one per asset",
    "no spot",
    "pass but size/room failed",
)


@dataclass(frozen=True)
class BoardEvent:
    ts: datetime | None
    kind: str  # PLAY | SIT
    asset: str
    ticker: str
    side: str
    detail: str
    result: str  # win | loss | pending | unfilled | sit | chop
    pnl: float | None
    pending: bool
    chop: bool
    window_id: str
    source: str  # journal | scan


def use_color(*, stream: Any | None = None) -> bool:
    """Honor FORCE_COLOR / CLICOLOR_FORCE; NO_COLOR wins."""
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


def gate_status(
    *,
    halted: bool,
    live_trading: bool,
    confirm_live: str,
) -> dict[str, Any]:
    """Real HALTED / LIVE / CONFIRM from .env. Never a hardcoded 'Live OFF'."""
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


def gate_status_from_settings(settings: FifteenSettings) -> dict[str, Any]:
    return gate_status(
        halted=settings.halted,
        live_trading=settings.live_trading,
        confirm_live=settings.confirm_live,
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


def classify_sit_note(note: str) -> str | None:
    """Return sit kind, or None to omit (routine timer noise)."""
    text = str(note or "").strip()
    if not text:
        return None
    low = text.lower()
    if any(snippet in low for snippet in SKIP_SIT_SNIPPETS):
        return None
    if "chop veto" in low or CHOP_VETO_PHRASE.lower() in low:
        return "chop"
    if "proxy" in low:
        return "proxy"
    if "news" in low:
        return "news"
    if "revenge" in low:
        return "revenge"
    if "session stopped" in low or "3 losses" in low:
        return "stopped"
    return None


def _ticker_from_note(note: str) -> str:
    left = str(note or "").split(":", 1)[0].strip()
    if left.upper().startswith("KX"):
        return left
    return ""


def _asset_from_note(note: str, ticker: str) -> str:
    if ticker:
        return asset_from_ticker(ticker)
    text = str(note or "")
    upper = text.upper()
    if upper.startswith("ETH") or " ETH" in upper:
        return "ETH"
    if upper.startswith("BTC") or " BTC" in upper:
        return "BTC"
    return ""


def _window_id_for(stamp: datetime | None, explicit: object = None) -> str:
    if explicit:
        return str(explicit)
    if stamp is None:
        return ""
    return fifteen_window_id(stamp)


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


def _pnl_of(row: dict[str, Any]) -> float | None:
    raw = row.get("pnl")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def play_events_from_journal(rows: list[dict[str, Any]], *, paper: bool) -> list[BoardEvent]:
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
                asset=asset,
                ticker=ticker,
                side=side,
                detail=detail,
                result=mark,
                pnl=pnl,
                pending=pending,
                chop=False,
                window_id=_window_id_for(stamp, row.get("window_id")),
                source="journal",
            )
        )
    return events


def sit_events_from_scans(scan_rows: list[dict[str, Any]]) -> list[BoardEvent]:
    events: list[BoardEvent] = []
    seen: set[tuple[str, str, str]] = set()
    for row in scan_rows:
        if not isinstance(row, dict):
            continue
        stamp = parse_ts(row.get("ts") or row.get("ts_iso"))
        window = str(row.get("window_id") or _window_id_for(stamp))
        for note in row.get("notes") or []:
            kind = classify_sit_note(str(note))
            if kind is None:
                continue
            ticker = _ticker_from_note(str(note))
            asset = _asset_from_note(str(note), ticker)
            key = (window, ticker or asset, kind)
            if key in seen:
                continue
            seen.add(key)
            chop = kind == "chop"
            events.append(
                BoardEvent(
                    ts=stamp,
                    kind="SIT",
                    asset=asset or "?",
                    ticker=ticker,
                    side="",
                    detail=str(note),
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
    play_tickers = {(event.window_id, event.ticker) for event in plays if event.ticker}
    merged: list[BoardEvent] = list(plays)
    for sit in sits:
        if sit.ticker and (sit.window_id, sit.ticker) in play_tickers:
            continue
        merged.append(sit)
    merged.sort(
        key=lambda event: (
            event.ts or datetime.min.replace(tzinfo=ET),
            0 if event.kind == "PLAY" else 1,
            event.ticker,
        )
    )
    return merged


def _paper_settled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in scoreboard_rows(rows)
        if str(row.get("result") or "") in {RESULT_WIN, RESULT_LOSS}
    ]


def paper_streak(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = _paper_settled(rows)
    if not settled:
        return {"result": None, "n": 0}
    last = str(settled[-1].get("result") or "")
    n = 0
    for row in reversed(settled):
        if str(row.get("result") or "") != last:
            break
        n += 1
    return {"result": last, "n": n}


def paper_day_pnl(rows: list[dict[str, Any]], now: datetime | None = None) -> float:
    total = 0.0
    for row in _paper_settled(rows):
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


def _hr(title: str = "") -> str:
    if not title:
        return "-" * WIDTH
    body = f" {title} "
    pad = max(0, WIDTH - len(body) - 2)
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


def format_gate_line(gates: dict[str, Any], *, color: bool) -> str:
    halted = "true" if gates["halted"] else "false"
    live = "true" if gates["live_trading"] else "false"
    confirm = str(gates["confirm_live"])
    label = str(gates["label"])
    arrow = paint(label, _gate_color(label) + BOLD, color)
    return (
        f"  Gates   HALTED={halted}  LIVE_TRADING={live}  "
        f"CONFIRM_LIVE={confirm}  →  {arrow}"
    )


def format_timeline_line(event: BoardEvent, *, color: bool) -> str:
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
    body = _clip(detail, 44)
    return f"  {when:<9} {kind} {asset} {body:<44} {badge_s}"


def format_scoreboard(
    *,
    paper: bool,
    settings: FifteenSettings,
    journal_rows: list[dict[str, Any]],
    scan_rows: list[dict[str, Any]],
    pot: FifteenPot | None = None,
    now: datetime | None = None,
    color: bool | None = None,
    gates: dict[str, Any] | None = None,
) -> str:
    """Beginner-friendly Termius board. Backfills never enter W/L, pot, or PLAY."""
    enabled = use_color() if color is None else bool(color)
    now = to_et(now)
    gates = gates or gate_status_from_settings(settings)
    start = float(getattr(settings, "pot_start", None) or DEFAULT_POT_START)
    ask = float(getattr(settings, "pot_double", None) or DEFAULT_POT_DOUBLE)
    n_backfills = sum(1 for row in journal_rows if is_journal_backfill(row))

    plays = play_events_from_journal(journal_rows, paper=paper)
    sits = sit_events_from_scans(scan_rows)
    timeline = merge_timeline(plays, sits)
    pending = [event for event in plays if event.pending]
    chop_sits = [event for event in sits if event.chop]
    # Journal sit/unscored rows (PROXY paper tickets) are already in plays.
    journal_sits = [event for event in plays if event.kind == "SIT"]

    if paper:
        summary = summarize_paper(journal_rows)
        wins = int(summary["n_wins"])
        losses = int(summary["n_losses"])
        pnl = float(summary["assumed_fill_pnl"])
        n_pending = int(summary["n_pending"])
        streak = paper_streak(journal_rows)
        day = paper_day_pnl(journal_rows, now=now)
        settled_pnls = [float(row.get("pnl") or 0) for row in _paper_settled(journal_rows)]
        equity = pot_curve(settled_pnls, start=start)
        pot_now = equity[-1]
        tape = "PAPER tape · assumed maker fills · not live cash"
        title = "KB15 PAPER SCOREBOARD"
    else:
        live = summarize_trades(journal_rows)
        wins = int(live["n_wins"])
        losses = int(live["n_losses"])
        pnl = float(live["pnl"])
        n_pending = int(live["n_pending"])
        streak = win_loss_streak(journal_rows)
        day = day_filled_pnl(journal_rows, now=now)
        settled_pnls = [
            float(row.get("pnl") or 0)
            for row in filled_settled_score_rows(journal_rows)
            if is_live_play_row(row)
        ]
        equity = pot_curve(settled_pnls, start=start)
        pot_now = play_pot_equity(journal_rows, start=start)
        tape = "LIVE tape · real money · fifteen_trade_log.jsonl"
        title = "KB15 LIVE SCOREBOARD"

    if streak.get("n"):
        tag = "W" if streak.get("result") == "win" else "L"
        streak_s = f"{streak['n']}{tag}"
    else:
        streak_s = "—"

    spark = sparkline(equity) or SPARK_CHARS[0]
    bar = meter(pot_now, lo=0.0, hi=ask)
    header = f"{title:<40} {format_et(now)}"
    lines = [
        "",
        paint(f"  {header}", BOLD, enabled),
        "  " + "=" * (WIDTH - 2),
        f"  {tape}",
        format_gate_line(gates, color=enabled),
        (
            f"  Pot     {_money(pot_now, signed=False)}   start ${start:.2f}   "
            f"ask ${ask:.2f}   empty $0"
        ),
        f"          {spark}   ${start:.2f} → ${pot_now:.2f}",
        f"          {bar}  ${pot_now:.2f} / ${ask:.2f}",
        (
            f"  Plays   {wins}W-{losses}L   pnl {_money(pnl)}   day {_money(day)}   "
            f"streak {streak_s}   pending {n_pending}"
        ),
        (
            f"  Sits    {len(chop_sits)} chop (count as W, $0)   ·   "
            f"{len(journal_sits)} journal sit/unscored"
        ),
    ]
    if not paper and pot is not None:
        file_bal = float(pot.balance)
        if abs(file_bal - pot_now) > 0.005:
            lines.append(
                f"  fifteen_pot.json ${file_bal:.2f} (display uses play-only "
                f"${pot_now:.2f}; file not changed)"
            )
        else:
            lines.append(
                f"  fifteen_pot.json ${file_bal:.2f} matches play-only  "
                f"stopped={pot.stopped}"
            )
    if n_backfills:
        lines.append(
            f"  Journal {len(journal_rows)} rows  ·  skipped {n_backfills} kind=backfill recon"
        )
    else:
        lines.append(f"  Journal {len(journal_rows)} rows  ·  no backfill recon rows")

    lines.append("  " + _hr("PENDING"))
    if pending:
        for event in pending:
            mark = paint("!", YELLOW + BOLD, enabled)
            when = _short_time(event.ts)
            ticker = event.ticker or "?"
            lines.append(
                f"  {mark} {when:<9} {event.asset:<3} {event.side:<3} "
                f"{_clip(ticker + '  ' + event.detail, WIDTH - 24)}"
            )
        lines.append("    waiting on the official BRTI / ETHUSD_RTI settlement print")
    else:
        lines.append("    none — no open assumed/live tickets waiting to settle")

    lines.append("  " + _hr("TIMELINE  PLAY + SIT  (chop sits are wins)"))
    shown = timeline[-TIMELINE_LIMIT:]
    if len(timeline) > TIMELINE_LIMIT:
        lines.append(f"    … {len(timeline) - TIMELINE_LIMIT} older events omitted")
    if not shown:
        lines.append("    no PLAY or notable SIT events yet")
    for event in shown:
        lines.append(format_timeline_line(event, color=enabled))

    lines.extend(
        [
            "  " + "-" * (WIDTH - 2),
            "  PLAY = took a ticket.  SIT = sat out.  chop sit = process win (no $).",
            "  Read-only board. Backfills stay in the jsonl and are not fake losses.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_scan_log(path: Path) -> list[dict[str, Any]]:
    return load_trades(path)


def _load_pot_file(settings: FifteenSettings) -> FifteenPot | None:
    dest = Path(settings.pot_path)
    if not dest.is_file():
        return None
    return load_pot(dest)


def run_paper_score(settings: FifteenSettings, *, color: bool | None = None) -> int:
    """Read-only paper board. Does not settle or rewrite journals / pot."""
    journal = load_trades(Path(settings.paper_log_path))
    scans = _load_scan_log(Path(settings.scan_log_path))
    print(
        format_scoreboard(
            paper=True,
            settings=settings,
            journal_rows=journal,
            scan_rows=scans,
            pot=_load_pot_file(settings),
            color=color,
        )
    )
    return EXIT_OK


def run_live_score(settings: FifteenSettings, *, color: bool | None = None) -> int:
    """Read-only live board. Does not rewrite fifteen_trade_log.jsonl or fifteen_pot.json."""
    journal = load_trades(Path(settings.trade_log_path))
    scans = _load_scan_log(Path(settings.scan_log_path))
    print(
        format_scoreboard(
            paper=False,
            settings=settings,
            journal_rows=journal,
            scan_rows=scans,
            pot=_load_pot_file(settings),
            color=color,
        )
    )
    return EXIT_OK
