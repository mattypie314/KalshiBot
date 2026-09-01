from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from kalshibot.assets import asset_by_key, identify_asset
from kalshibot.campaign.fifteen import (
    enough_room,
    fifteen_stake,
    fifteen_stopped,
    fifteen_window_id,
    fifteen_working,
    half_sigma_move,
    in_fifteen_entry_window,
    in_fifteen_revenge,
    in_fifteen_settlement,
    news_blackout,
    pass_fail,
    record_fifteen_result,
)
from kalshibot.campaign.playbook import playbook_from_settings
from kalshibot.campaign.sizing import cash_from_balance, playbook_from_sizing, total_value_from_balance
from kalshibot.campaign.rules import (
    classify_favorite,
    contracts_for_budget,
    flatten_reason,
    held_bid,
    hourly_scan_window,
    in_maker_window,
    maker_contract_price,
    maker_spread_ok,
    open_cost,
)
from kalshibot.campaign.blotter import map_kalshi_order, map_kalshi_position
from kalshibot.campaign.tracker import Tracker
from kalshibot.campaign.universe import FIFTEEN_SERIES, is_campaign_hourly_universe, is_daily_ticker, shard_for_series
from kalshibot.config import Settings, settings
from kalshibot.kalshi import KalshiClient
from kalshibot.models import (
    annual_vol_from_hourly,
    distance_in_sigma,
    hours_to_years,
    parse_strike,
    price_threshold_prob,
)
from kalshibot.money import parse_close_time, parse_dollars, seconds_until
from kalshibot.spots import SpotService

logger = logging.getLogger(__name__)


class CampaignEngine:
    def __init__(self, cfg: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg or settings
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            timeout=self.cfg.request_timeout_seconds,
            headers={"User-Agent": "KalshiBot/0.1"},
        )
        self.kalshi = KalshiClient(
            self.cfg.kalshi_base_url,
            self.cfg.request_timeout_seconds,
            client=self._http,
            min_interval=self.cfg.kalshi_min_interval,
            api_key_id=self.cfg.kalshi_api_key_id,
            private_key_path=self.cfg.kalshi_private_key_path,
        )
        self.spots = SpotService(self._http)
        self.tracker = Tracker(self.cfg.tracker_path, self.cfg.campaign_bankroll)
        self.playbook = playbook_from_settings(self.cfg)
        self._live_override: bool | None = None
        self._fire_lock = asyncio.Lock()
        self._book_cache: tuple[float, list[dict[str, Any]], list[dict[str, Any]]] | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def status(self) -> dict[str, Any]:
        self.tracker.load()
        state = self.tracker.snapshot()
        tickets = [t for t in state["tickets"] if t.get("status") == "open"]
        cost = open_cost(state["tickets"])
        bankroll = float(state["bankroll"])
        realized = float(state["realized"])
        sizing = state.get("sizing") or {}
        equity = self._equity()
        typical = float(self.playbook.typical_risk_max) * equity
        return {
            "live": self.live,
            "can_trade": self.kalshi.can_trade,
            "tracker_path": str(self.tracker.path),
            "bankroll": bankroll,
            "realized": round(realized, 4),
            "open_cost": round(cost, 4),
            "room": round(self._room(), 4),
            "equity": round(equity, 4),
            "kalshi_cash": state.get("kalshi_cash"),
            "kalshi_total_value": state.get("kalshi_total_value"),
            "follow_kalshi_cash": bool(sizing.get("follow_kalshi_cash", True)),
            "maker_auto": bool(sizing.get("maker_auto", True)),
            "halted": bool(sizing.get("halted", False)),
            "auto": bool(self.cfg.kalshi_auto),
            "fifteen_stopped": fifteen_stopped(state),
            "fifteen_revenge": in_fifteen_revenge(state),
            "fifteen_look": in_fifteen_entry_window(),
            "bankroll_cap": sizing.get("bankroll_cap"),
            "typical_idea": round(typical, 2),
            "open_tickets": tickets,
            "rests": [r for r in state.get("rests", []) if r.get("status") == "open"],
            "rests_source": "local",
            "positions_source": "local",
            "log": list(reversed(state.get("log", [])[-20:])),
            "updated_at": state.get("updated_at"),
            "playbook": self.playbook.as_status(),
            "sizing": sizing,
        }

    async def public_status(self) -> dict[str, Any]:
        """Phone blotter. Positions come from Kalshi when keys are loaded, not the local tracker."""
        payload = self.status()
        if not self.kalshi.can_trade:
            return payload
        orders, positions, errors = await self._exchange_book()
        paper_rests = [row for row in payload["rests"] if row.get("paper")]
        paper_tickets = [row for row in payload["open_tickets"] if row.get("paper")]
        if "orders" not in errors:
            payload["rests"] = [mapped for order in orders if (mapped := map_kalshi_order(order))] + paper_rests
            payload["rests_source"] = "kalshi"
        if "positions" not in errors:
            payload["open_tickets"] = [
                mapped for pos in positions if (mapped := map_kalshi_position(pos))
            ] + paper_tickets
            payload["positions_source"] = "kalshi"
        if errors:
            payload["blotter_error"] = ",".join(sorted(errors))
        return payload

    async def _exchange_book(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        now = time.monotonic()
        cached = self._book_cache
        if cached and now - cached[0] < 15.0:
            return cached[1], cached[2], set()

        orders: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        errors: set[str] = set()

        async def _orders() -> list[dict[str, Any]]:
            return await self.kalshi.get_orders(status="resting", limit=200)

        async def _positions() -> list[dict[str, Any]]:
            return await self.kalshi.get_positions(limit=200)

        fetched_orders, fetched_positions = await asyncio.gather(_orders(), _positions(), return_exceptions=True)
        if isinstance(fetched_orders, BaseException):
            logger.exception("Kalshi orders refresh failed", exc_info=fetched_orders)
            errors.add("orders")
        else:
            orders = fetched_orders
        if isinstance(fetched_positions, BaseException):
            logger.exception("Kalshi positions refresh failed", exc_info=fetched_positions)
            errors.add("positions")
        else:
            positions = fetched_positions
        if not errors:
            self._book_cache = (now, orders, positions)
        return orders, positions, errors

    def _reload_playbook(self) -> None:
        self.playbook = playbook_from_sizing(self.cfg, self.tracker.state.get("sizing") or {})

    def _follow_kalshi(self) -> bool:
        return bool((self.tracker.state.get("sizing") or {}).get("follow_kalshi_cash", True))

    def _bankroll_cap(self) -> float | None:
        cap = (self.tracker.state.get("sizing") or {}).get("bankroll_cap")
        if cap in (None, ""):
            return None
        return float(cap)

    def _equity(self) -> float:
        cash = self.tracker.state.get("kalshi_cash")
        if self._follow_kalshi() and cash is not None:
            equity = float(cash) + open_cost(self.tracker.state["tickets"])
            cap = self._bankroll_cap()
            if cap is not None:
                equity = min(equity, cap)
            return max(equity, 0.0)
        return float(self.tracker.state["bankroll"]) + float(self.tracker.state["realized"])

    def _total_value(self) -> float:
        """Kalshi total bankroll. Never size off a portfolio_value smaller than live cash."""
        equity = self._equity()
        tv = self.tracker.state.get("kalshi_total_value")
        if self._follow_kalshi() and tv is not None:
            value = max(float(tv), equity)
            cap = self._bankroll_cap()
            if cap is not None:
                value = min(value, cap)
            return max(value, 0.0)
        return equity

    def _revenge_active(self) -> bool:
        raw = self.tracker.state.get("last_loss_at")
        if not raw:
            return False
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds() < self.cfg.revenge_seconds

    def _room(self) -> float:
        open_c = open_cost(self.tracker.state["tickets"])
        budget = self._equity() - open_c
        cash = self.tracker.state.get("kalshi_cash")
        if self._follow_kalshi() and cash is not None:
            return max(0.0, min(float(cash), budget))
        return max(0.0, budget)

    async def _sync_kalshi_cash(self) -> str | None:
        if not (self.live and self.kalshi.can_trade and self._follow_kalshi()):
            return None
        try:
            payload = await self.kalshi.get_balance()
        except Exception as exc:  # noqa: BLE001 — keep last cash rather than crash the job
            logger.warning("Kalshi balance failed: %s", exc)
            return f"Could not refresh Kalshi cash ({exc}); using the saved book."
        cash = cash_from_balance(payload)
        total = total_value_from_balance(payload)
        if cash is not None:
            self.tracker.state["kalshi_cash"] = round(cash, 4)
        if total is not None:
            self.tracker.state["kalshi_total_value"] = round(total, 4)
        if cash is None:
            return None
        return f"Kalshi cash ${cash:.2f} · campaign equity ${self._equity():.2f}."

    def _open_idea_count(self) -> int:
        tickets = sum(1 for t in self.tracker.state["tickets"] if t.get("status") == "open")
        rests = sum(1 for r in self.tracker.state.get("rests", []) if r.get("status") == "open")
        return tickets + rests

    def _maker_auto(self) -> bool:
        return bool((self.tracker.state.get("sizing") or {}).get("maker_auto", True))

    def _halted(self) -> bool:
        return bool((self.tracker.state.get("sizing") or {}).get("halted", False))

    def _live_from_book(self) -> bool:
        if not self.kalshi.can_trade:
            return False
        sizing = self.tracker.state.get("sizing") or {}
        if "live" in sizing:
            return bool(sizing["live"])
        return bool(self.cfg.kalshi_live)

    @property
    def live(self) -> bool:
        if self._live_override is not None:
            return bool(self._live_override)
        return self._live_from_book()

    @live.setter
    def live(self, value: bool) -> None:
        self._live_override = bool(value)

    async def fire(self, loop: str) -> dict[str, Any]:
        async with self._fire_lock:
            return await self._fire(loop)

    async def _fire(self, loop: str) -> dict[str, Any]:
        self.tracker.load()
        self._reload_playbook()
        self.spots.clear()
        actions: list[str] = []
        try:
            synced = await self._sync_kalshi_cash()
            if synced:
                actions.append(synced)

            if self.live:
                actions.extend(self._drop_practice_tickets())

            if self._halted():
                quotes = await self._quotes_for_open_tickets()
                actions.extend(await self._manage_open(quotes, loop))
                actions.extend(await self._cancel_open_rests("halted"))
                actions.append("Campaign halted until further notice. No new trades.")
                if loop == "fifteen":
                    tell = [self._tell_fifteen(a, expect_ticket=False) for a in actions]
                    return self._finish(loop, actions, quiet=True, tell=tell)
                return self._finish(loop, actions, quiet=False)

            quotes = await self._quotes_for_open_tickets()
            actions.extend(await self._manage_open(quotes, loop))
            actions.extend(await self._manage_rests())

            expect_ticket = False
            if loop == "fifteen":
                expect_ticket = in_fifteen_entry_window()
                actions.extend(await self._fifteen_gate_and_enter(expect_ticket=expect_ticket))
            elif self._revenge_active() and loop in {"hourly", "maker"}:
                actions.append("Sit out: no revenge betting after a loss.")
            elif loop == "hourly":
                series = await self._hourly_series()
                actions.extend(
                    await self._enter_limit(
                        series,
                        loop,
                        skip_last=self.cfg.skip_last_seconds,
                        max_secs=self.cfg.hourly_max_seconds,
                    )
                )
            elif loop == "maker":
                if not self._maker_auto():
                    actions.append("Maker auto is off. Run workflow and set maker_auto to yes to start.")
                elif not in_maker_window():
                    actions.append("Maker window closed; stay quiet.")
                else:
                    actions.extend(await self._enter_maker())

            if loop == "fifteen":
                if not actions:
                    actions.append("Nothing new.")
                tell = [self._tell_fifteen(a, expect_ticket=expect_ticket) for a in actions]
                return self._finish(loop, actions, quiet=True, tell=tell)
            if not actions:
                actions.append("Nothing new.")
                return self._finish(loop, actions, quiet=True)
            return self._finish(loop, actions, quiet=False)
        except Exception as exc:  # noqa: BLE001 — campaign loop must not crash the GitHub job
            logger.exception("Campaign %s failed", loop)
            mode = "live" if self.live else "practice mode"
            actions.append(f"Loop error ({mode}): {exc}")
            return self._finish(loop, actions, quiet=False)

    def _drop_practice_tickets(self) -> list[str]:
        """Practice fills were never on Kalshi. Do not flatten them live."""
        dropped: list[str] = []
        for ticket in self.tracker.state["tickets"]:
            if ticket.get("status") != "open":
                continue
            if ticket.get("order_id") and not ticket.get("paper"):
                continue
            ticket["status"] = "flat"
            ticket["exit_reason"] = "practice_ticket"
            ticket["realized"] = 0.0
            dropped.append(str(ticket.get("ticker") or "?"))
        for rest in self.tracker.state.get("rests", []):
            if rest.get("status") == "open" and not rest.get("order_id"):
                rest["status"] = "canceled"
                rest["exit_reason"] = "practice_ticket"
        if not dropped:
            return []
        sample = ", ".join(dropped[:6])
        extra = "" if len(dropped) <= 6 else f" (+{len(dropped) - 6} more)"
        return [f"Cleared {len(dropped)} practice ticket(s) so live orders can start: {sample}{extra}."]

    def _finish(self, loop: str, actions: list[str], quiet: bool, tell: list[bool] | None = None) -> dict[str, Any]:
        for i, action in enumerate(actions):
            if tell is not None:
                is_quiet = not tell[i] if i < len(tell) else True
            else:
                is_quiet = quiet and action == actions[-1]
            self.tracker.note(action, loop, quiet=is_quiet)
        self.tracker.save()
        return {"loop": loop, "live": self.live, "actions": actions, "status": self.status()}

    def _tell_fifteen(self, action: str, *, expect_ticket: bool) -> bool:
        low = action.lower()
        if "three 15m losses" in low or "15m loop stopped" in low:
            return True
        if "halted until further notice" in low:
            return True
        if "flatten" in low and "pnl -" in low:
            return True
        if " filled " in low:
            return True
        if not expect_ticket:
            return False
        if low.startswith("fail") or " fail " in low:
            return True
        if "revenge" in low or "not live" in low or "news candle" in low:
            return True
        if "below 3%" in low or "15m skipped" in low or "already stopped" in low:
            return True
        if "working this window" in low:
            return True
        if "post-only" in low:
            return True
        return False

    async def _quotes_for_open_tickets(self) -> dict[str, dict[str, float]]:
        quotes: dict[str, dict[str, float]] = {}
        tickers = {t["ticker"] for t in self.tracker.state["tickets"] if t.get("status") == "open"}
        for ticker in tickers:
            data = await self.kalshi.get_json(f"/markets/{ticker}")
            market = data.get("market") or data
            bid = parse_dollars(market.get("yes_bid_dollars")) or 0.0
            ask = parse_dollars(market.get("yes_ask_dollars")) or 0.0
            quotes[ticker] = {"yes_bid": bid, "yes_ask": ask}
        return quotes

    async def _manage_open(self, quotes: dict[str, dict[str, float]], loop: str) -> list[str]:
        actions: list[str] = []
        for ticket in list(self.tracker.state["tickets"]):
            if ticket.get("status") != "open":
                continue
            q = quotes.get(ticket["ticker"])
            if not q:
                continue
            reason = flatten_reason(ticket, q["yes_bid"], q["yes_ask"])
            if is_daily_ticker(ticket["ticker"]):
                reason = "wrong_universe_daily"
            if ticket.get("loop") == "fifteen":
                if not reason:
                    reason = await self._fifteen_manage_reason(ticket, q["yes_bid"], q["yes_ask"])
            elif ticket.get("kind") == "maker_spread":
                if not reason:
                    continue
            elif not reason and ticket.get("model_prob") is not None:
                idea = await self._rescore_held(ticket, q["yes_bid"], q["yes_ask"])
                if idea is not None and self.playbook.edge_decayed(idea):
                    reason = "edge_decay"
                elif idea is None:
                    market_now = held_bid(ticket["side"], q["yes_bid"], q["yes_ask"])
                    if float(ticket["model_prob"]) - market_now < self.playbook.edge_decay_floor:
                        reason = "edge_decay"
            if not reason:
                continue
            sell_px = max(q["yes_bid"] if ticket["side"] == "yes" else q["yes_ask"], 0.01)
            actions.append(
                await self._flatten(ticket, sell_px, reason)
            )
        return [a for a in actions if a]

    async def _flatten(self, ticket: dict[str, Any], price: float, reason: str) -> str:
        count = float(ticket["count"])
        side = "ask" if ticket["side"] == "yes" else "bid"
        payload = {
            "ticker": ticket["ticker"],
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True,
            "post_only": False,
            "client_order_id": str(uuid.uuid4()),
            "exchange_index": -1,
        }
        fill_count = count
        avg = price
        if self.live:
            if ticket.get("paper") or not ticket.get("order_id"):
                ticket["status"] = "flat"
                ticket["exit_reason"] = "practice_ticket"
                ticket["exit_price"] = price
                ticket["realized"] = 0.0
                return f"Dropped practice ticket {ticket['ticker']} (never a real Kalshi fill)."
            try:
                resp = await self.kalshi.create_order_v2(payload)
            except httpx.HTTPStatusError as exc:
                return f"LIVE flatten failed {ticket['ticker']}: {exc}"
            fill_count = float(resp.get("fill_count") or 0)
            avg = float(resp.get("average_fill_price") or price)
        fill = float(ticket["fill"])
        if ticket["side"] == "yes":
            pnl = (avg - fill) * fill_count
        else:
            pnl = ((1.0 - avg) - fill) * fill_count
        ticket["status"] = "flat"
        ticket["exit_reason"] = reason
        ticket["exit_price"] = avg
        ticket["realized"] = round(pnl, 4)
        self.tracker.state["realized"] = round(float(self.tracker.state["realized"]) + pnl, 4)
        extra = ""
        if ticket.get("loop") == "fifteen":
            fair = ticket.get("model_yes")
            if fair is not None:
                extra = f" · fair {float(fair):.2f}"
            stop_msg = record_fifteen_result(self.tracker.state, pnl)
            if stop_msg:
                extra = f"{extra}. {stop_msg}"
        elif pnl < 0:
            self.tracker.state["last_loss_at"] = datetime.now(timezone.utc).isoformat()
        mode = "LIVE" if self.live else "DRY"
        return f"{mode} flatten {ticket['ticker']} {ticket['side']} {reason} pnl {pnl:+.2f}{extra}"

    async def _hourly_series(self) -> list[str]:
        tickers: list[str] = []
        for category in ("Crypto", "Commodities"):
            series_list = await self.kalshi.series_for_category(category)
            for series in series_list:
                if is_campaign_hourly_universe(series):
                    tickers.append(series["ticker"])
        return tickers[:24]

    async def _load_candidates(self, series_tickers: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for ticker in series_tickers:
            if is_daily_ticker(ticker):
                continue
            events = await self.kalshi.open_events(ticker, limit=2)
            for event in events:
                for market in event.get("markets") or []:
                    if str(market.get("status") or "").lower() not in {"active", "open"}:
                        continue
                    if is_daily_ticker(str(market.get("ticker") or ticker)):
                        continue
                    out.append({"series": ticker, "event": event, "market": market})
        return out

    async def _rescore_held(self, ticket: dict[str, Any], yes_bid: float, yes_ask: float):
        asset = asset_by_key(ticket.get("asset_key"))
        strike = ticket.get("strike")
        spec_kind = ticket.get("spec_kind") or "greater"
        if not asset or not strike:
            return None
        spec = parse_strike(
            {
                "strike_type": spec_kind,
                "custom_strike": {
                    "floor_strike": None if spec_kind in {"less", "less_or_equal"} else strike,
                    "cap_strike": ticket.get("cap") or (strike if spec_kind in {"less", "less_or_equal", "range"} else None),
                },
            }
        )
        spots = await self.spots.prices_for([asset])
        spot = spots.get(asset.key)
        if not spot:
            return None
        secs = float(ticket.get("secs_left") or self.playbook.min_time_seconds)
        close = parse_close_time(ticket.get("close_at"))
        if close:
            secs = seconds_until(close) or secs
        hour_vol = await self.spots.hourly_vol(asset)
        vol = annual_vol_from_hourly(hour_vol)
        hours_left = max(secs, 1.0) / 3600.0
        model_yes = price_threshold_prob(spec, spot, hours_to_years(hours_left), vol)
        if model_yes is None:
            return None
        sigma = distance_in_sigma(spot, float(strike), hour_vol, hours_left)
        return self.playbook.evaluate(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            model_yes=model_yes,
            sigma=sigma,
            secs_left=secs,
            equity=self._equity(),
        )

    async def _manage_rests(self) -> list[str]:
        actions: list[str] = []
        for rest in list(self.tracker.state.get("rests", [])):
            if rest.get("status") != "open":
                continue
            ticker = rest.get("ticker") or ""
            if is_daily_ticker(ticker):
                actions.append(await self._cancel_rest(rest, "wrong_universe_daily"))
                continue
            close = parse_close_time(rest.get("close_at"))
            if close and (seconds_until(close) or 0) <= 0:
                actions.append(await self._cancel_rest(rest, "expired"))
                continue
            try:
                data = await self.kalshi.get_json(f"/markets/{ticker}")
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    actions.append(await self._cancel_rest(rest, "expired"))
                continue
            except Exception:
                continue
            market = data.get("market") or data
            yes_bid = parse_dollars(market.get("yes_bid_dollars")) or 0.0
            yes_ask = parse_dollars(market.get("yes_ask_dollars")) or 0.0
            status = str(market.get("status") or "").lower()
            close = parse_close_time(market.get("close_time")) or close
            secs_left = seconds_until(close)
            if status in {"determined", "closed", "settled", "finalized"} or (
                close and secs_left is not None and secs_left <= 0
            ):
                actions.append(await self._cancel_rest(rest, "expired"))
                continue
            if rest.get("kind") == "maker_spread":
                close = parse_close_time(market.get("close_time")) or parse_close_time(rest.get("close_at"))
                secs = seconds_until(close) or 0
                if secs < 10:
                    actions.append(await self._cancel_rest(rest, "near_settlement"))
                    continue
                mid = (yes_bid + yes_ask) / 2.0
                if rest.get("side") == "yes" and mid < 0.45:
                    actions.append(await self._cancel_rest(rest, "favorite_flipped"))
                elif rest.get("side") == "no" and mid > 0.55:
                    actions.append(await self._cancel_rest(rest, "favorite_flipped"))
                continue
            if rest.get("loop") == "fifteen":
                reason = await self._fifteen_manage_reason(rest, yes_bid, yes_ask)
                if reason:
                    actions.append(await self._cancel_rest(rest, reason))
                continue
            idea = await self._rescore_held(rest, yes_bid, yes_ask)
            if idea is not None and self.playbook.edge_decayed(idea):
                actions.append(await self._cancel_rest(rest, "edge_decay"))
        return [a for a in actions if a]

    async def _cancel_open_rests(self, reason: str) -> list[str]:
        actions: list[str] = []
        for rest in self.tracker.state.get("rests", []):
            if rest.get("status") == "open":
                actions.append(await self._cancel_rest(rest, reason))
        return [a for a in actions if a]

    async def _cancel_rest(self, rest: dict[str, Any], reason: str) -> str:
        if self.live and rest.get("order_id") and not rest.get("paper"):
            try:
                await self.kalshi.cancel_order(str(rest["order_id"]), ticker=str(rest.get("ticker") or "") or None)
            except httpx.HTTPStatusError as exc:
                gone = exc.response is not None and exc.response.status_code == 404
                if not gone:
                    return f"LIVE cancel failed {rest.get('ticker')}: {exc}"
        rest["status"] = "canceled"
        rest["exit_reason"] = reason
        mode = "LIVE" if self.live else "DRY"
        return f"{mode} cancel rest {rest.get('ticker')} ({reason})"

    async def _score_market(self, row: dict[str, Any]) -> dict[str, Any] | None:
        market = row["market"]
        event = row["event"]
        series = row["series"]
        yes_bid = parse_dollars(market.get("yes_bid_dollars"))
        yes_ask = parse_dollars(market.get("yes_ask_dollars"))
        if yes_bid is None or yes_ask is None or yes_bid <= 0 or yes_ask <= 0:
            return None
        close = parse_close_time(market.get("close_time"))
        secs = seconds_until(close) or 0
        asset = identify_asset(series, event.get("title") or "", market.get("title") or "")
        spec = parse_strike(market)
        strike = spec.floor or spec.cap
        if not asset or not strike:
            return None
        spots = await self.spots.prices_for([asset])
        spot = spots.get(asset.key)
        if not spot:
            return None
        hour_vol = await self.spots.hourly_vol(asset)
        vol = annual_vol_from_hourly(hour_vol)
        hours_left = max(secs, 1.0) / 3600.0
        model_yes = price_threshold_prob(spec, spot, hours_to_years(hours_left), vol)
        if model_yes is None:
            model_yes = 0.5 if spec.kind == "unknown" else (0.99 if spot >= strike else 0.01)
        sigma = distance_in_sigma(spot, float(strike), hour_vol, hours_left)
        idea = self.playbook.evaluate(
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            model_yes=model_yes,
            sigma=sigma,
            secs_left=secs,
            equity=self._equity(),
        )
        return {
            "series": series,
            "event": event,
            "market": market,
            "asset": asset,
            "spot": spot,
            "strike": strike,
            "spec_kind": spec.kind,
            "cap": spec.cap,
            "secs": secs,
            "close": close.isoformat() if close else None,
            "hourly_vol": hour_vol,
            "model_yes": model_yes,
            "sigma": sigma,
            "idea": idea,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "exchange_index": shard_for_series(series, event.get("title") or ""),
        }

    def _yes_to_cost(self, side: str, yes_price: float) -> float:
        return yes_price if side == "yes" else max(0.0, 1.0 - yes_price)

    def _already_in(self, ticker: str) -> bool:
        for ticket in self.tracker.state["tickets"]:
            if ticket.get("status") == "open" and ticket.get("ticker") == ticker:
                return True
        for rest in self.tracker.state.get("rests", []):
            if rest.get("status") == "open" and rest.get("ticker") == ticker:
                return True
        return False

    def _ticket_fields(self, pick: dict[str, Any], idea, count: float, fill: float, order_id: str | None) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid4()),
            "loop": pick.get("loop") or "campaign",
            "ticker": pick["market"]["ticker"],
            "title": pick["market"].get("title") or pick["event"].get("title"),
            "side": idea.side,
            "fill": fill,
            "count": count,
            "cost": round(fill * count, 4),
            "filled_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "exchange_index": pick["exchange_index"],
            "order_id": order_id,
            "model_prob": idea.model_prob,
            "model_yes": pick["model_yes"],
            "sigma": pick["sigma"],
            "spot": pick["spot"],
            "strike": pick["strike"],
            "spec_kind": pick["spec_kind"],
            "cap": pick.get("cap"),
            "asset_key": pick["asset"].key,
            "close_at": pick.get("close"),
            "hourly_vol": pick.get("hourly_vol"),
            "paper": not self.live,
            "kind": pick.get("kind"),
            "window_id": pick.get("window_id"),
            "pass_line": pick.get("pass_line"),
        }

    async def _fifteen_manage_reason(self, held: dict[str, Any], yes_bid: float, yes_ask: float) -> str | None:
        close = parse_close_time(held.get("close_at"))
        secs = seconds_until(close) if close else 0.0
        if close and secs is not None and secs <= 0:
            return "settlement"
        if held.get("rechecked"):
            return None
        asset = asset_by_key(held.get("asset_key"))
        if not asset:
            return None
        spots = await self.spots.prices_for([asset])
        spot = spots.get(asset.key)
        if not spot:
            return None
        hour_vol = float(held.get("hourly_vol") or 0)
        if not hour_vol:
            hour_vol = await self.spots.hourly_vol(asset)
        entry_spot = float(held.get("spot") or 0)
        if not half_sigma_move(spot, entry_spot, hour_vol):
            return None
        held["rechecked"] = True
        idea = await self._rescore_held(held, yes_bid, yes_ask)
        if idea is None:
            return "edge_died"
        model_yes = idea.model_prob if idea.side == "yes" else (1.0 - idea.model_prob)
        decision = pass_fail(
            model_yes=model_yes,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            secs_left=secs or 1.0,
            sigma=idea.sigma,
            news=news_blackout(),
        )
        if not decision.passed:
            return "edge_died"
        if decision.side != held.get("side"):
            return "edge_flipped"
        return None

    async def _fifteen_gate_and_enter(self, *, expect_ticket: bool) -> list[str]:
        state = self.tracker.state
        if fifteen_stopped(state):
            if expect_ticket:
                return ["15m already stopped this session."]
            return []
        if in_fifteen_settlement():
            return []
        if self.cfg.kalshi_live and not self.kalshi.can_trade:
            if expect_ticket:
                return ["Book not live. 15m skipped."]
            return []
        if in_fifteen_revenge(state):
            if expect_ticket:
                return ["Skip this 15m window (revenge after a loss)."]
            return []
        if not expect_ticket:
            return []
        if fifteen_working(state):
            if expect_ticket:
                return ["Already have a 15m working this window."]
            return []
        total = self._total_value()
        room = self._room()
        if not enough_room(room, total):
            return [f"Room ${room:.2f} below 3% of bankroll ${total:.2f}. 15m skipped."]
        return await self._enter_fifteen(news=news_blackout())

    async def _enter_fifteen(self, *, news: str | None) -> list[str]:
        """One post-only limit after a Pass. Never market, never IOC pay-through."""
        from types import SimpleNamespace

        scored: list[dict[str, Any]] = []
        fails: list[str] = []
        for row in await self._load_candidates(list(FIFTEEN_SERIES)):
            item = await self._score_market(row)
            if not item:
                continue
            decision = pass_fail(
                model_yes=item["model_yes"],
                yes_bid=item["yes_bid"],
                yes_ask=item["yes_ask"],
                secs_left=item["secs"],
                sigma=item["sigma"],
                news=news,
            )
            item["decision"] = decision
            item["loop"] = "fifteen"
            item["kind"] = "fifteen_edge"
            item["window_id"] = fifteen_window_id()
            item["pass_line"] = decision.line
            if decision.passed:
                scored.append(item)
            else:
                fails.append(decision.line)
        scored.sort(key=lambda row: abs(row["decision"].edge), reverse=True)
        if not scored:
            return [fails[0] if fails else "FAIL no live 15m book"]

        pick = None
        for candidate in scored:
            if not self._already_in(candidate["market"]["ticker"]):
                pick = candidate
                break
        if pick is None:
            return []

        decision = pick["decision"]
        ticker = pick["market"]["ticker"]
        join = decision.join_price
        cost_px = self._yes_to_cost(decision.side, join)
        budget = fifteen_stake(self._total_value(), self._room())
        count = contracts_for_budget(budget, cost_px)
        cost = count * cost_px
        if count <= 0 or cost < self.playbook.min_stake:
            return [f"{decision.line} · size blocked. Sitting out."]

        book_side = "bid" if decision.side == "yes" else "ask"
        payload = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{count:.2f}",
            "price": f"{join:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "maker",
            "post_only": True,
            "client_order_id": str(uuid.uuid4()),
            "exchange_index": -1,
        }
        fill_count = 0.0
        avg_yes = join
        order_id = None
        mode = "LIVE" if self.live else "DRY"
        if self.live:
            try:
                resp = await self.kalshi.create_order_v2(payload)
            except httpx.HTTPStatusError as exc:
                return [f"LIVE limit failed {ticker}: {exc}"]
            fill_count = float(resp.get("fill_count") or 0)
            avg_yes = float(resp.get("average_fill_price") or join)
            order_id = resp.get("order_id")
        fill_cost = self._yes_to_cost(decision.side, avg_yes)
        idea = SimpleNamespace(side=decision.side, model_prob=decision.model_prob)
        rest = {
            "id": str(uuid.uuid4()),
            "loop": "fifteen",
            "kind": "fifteen_edge",
            "ticker": ticker,
            "side": decision.side,
            "price": join,
            "count": count,
            "status": "open",
            "order_id": order_id,
            "model_prob": decision.model_prob,
            "model_yes": pick["model_yes"],
            "sigma": pick["sigma"],
            "spot": pick["spot"],
            "strike": pick["strike"],
            "spec_kind": pick["spec_kind"],
            "cap": pick.get("cap"),
            "asset_key": pick["asset"].key,
            "close_at": pick.get("close"),
            "hourly_vol": pick.get("hourly_vol"),
            "window_id": pick["window_id"],
            "pass_line": decision.line,
            "paper": not self.live,
        }
        if fill_count > 0:
            ticket = self._ticket_fields(pick, idea, fill_count, fill_cost, order_id)
            self.tracker.state["tickets"].append(ticket)
            return [f"{mode} filled {decision.side} {ticker} {fill_count:.2f}@ {fill_cost:.2f} · {decision.line}"]
        self.tracker.state.setdefault("rests", []).append(rest)
        return [f"{mode} post-only {decision.side} {ticker} {count:.2f}@ {join:.2f} · {decision.line}"]

    async def _enter_limit(
        self,
        series_tickers: list[str],
        loop: str,
        skip_last: float,
        max_secs: float | None = None,
    ) -> list[str]:
        actions: list[str] = []
        book = self.playbook
        if self._open_idea_count() >= book.max_open_ideas:
            return ["Already in two ideas. Sitting out."]
        available = self._room()
        if available < book.min_stake:
            return [f"room ${available:.2f} < ${book.min_stake:.2f}; sitting out."]

        scored: list[dict[str, Any]] = []
        sat_out = 0
        for row in await self._load_candidates(list(series_tickers)):
            item = await self._score_market(row)
            if not item:
                continue
            if item["secs"] < skip_last:
                continue
            if max_secs is not None and item["secs"] > max_secs:
                continue
            if item["series"] not in FIFTEEN_SERIES and item["secs"] > self.cfg.hourly_max_seconds:
                continue
            item["loop"] = loop
            if item["idea"].sit_out:
                sat_out += 1
                continue
            scored.append(item)
        scored.sort(key=lambda r: r["idea"].net_edge, reverse=True)
        mode = "LIVE" if self.live else "DRY"
        placed = 0
        for pick in scored:
            if placed >= book.max_new_ideas_per_fire:
                break
            if self._open_idea_count() >= book.max_open_ideas:
                break
            available = self._room()
            if available < book.min_stake:
                break
            idea = pick["idea"]
            ticker = pick["market"]["ticker"]
            if self._already_in(ticker):
                continue
            join = idea.join_price
            cost_px = self._yes_to_cost(idea.side, join)
            equity = self._equity()
            budget = min(book.kelly_stake(equity, idea.model_prob, cost_px), available)
            if budget < book.min_stake:
                continue
            if budget > book.risk_hard_max * equity:
                budget = book.risk_hard_max * equity
            count = contracts_for_budget(budget, cost_px)
            cost = count * cost_px
            if cost > book.risk_hard_max * equity + 1e-9:
                count = contracts_for_budget(book.risk_hard_max * equity, cost_px)
                cost = count * cost_px
            if count <= 0 or cost < book.min_stake:
                continue
            book_side = "bid" if idea.side == "yes" else "ask"
            payload = {
                "ticker": ticker,
                "side": book_side,
                "count": f"{count:.2f}",
                "price": f"{join:.4f}",
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "maker",
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "exchange_index": -1,
            }
            fill_count = 0.0
            avg_yes = join
            order_id = None
            if self.live:
                try:
                    resp = await self.kalshi.create_order_v2(payload)
                except httpx.HTTPStatusError as exc:
                    actions.append(f"LIVE limit failed {ticker}: {exc}")
                    continue
                fill_count = float(resp.get("fill_count") or 0)
                avg_yes = float(resp.get("average_fill_price") or join)
                order_id = resp.get("order_id")
            fill_cost = self._yes_to_cost(idea.side, avg_yes)
            rest = {
                "id": str(uuid.uuid4()),
                "loop": loop,
                "ticker": ticker,
                "side": idea.side,
                "price": join,
                "count": count,
                "status": "open",
                "order_id": order_id,
                "kind": "limit_join",
                "model_prob": idea.model_prob,
                "model_yes": pick["model_yes"],
                "sigma": pick["sigma"],
                "spot": pick["spot"],
                "strike": pick["strike"],
                "spec_kind": pick["spec_kind"],
                "cap": pick.get("cap"),
                "asset_key": pick["asset"].key,
                "close_at": pick.get("close"),
                "hourly_vol": pick.get("hourly_vol"),
                "paper": not self.live,
            }
            if fill_count > 0:
                ticket = self._ticket_fields(pick, idea, fill_count, fill_cost, order_id)
                self.tracker.state["tickets"].append(ticket)
                actions.append(
                    f"{mode} filled {idea.side} {ticker} {fill_count:.2f}@ {fill_cost:.2f} · {idea.rationale}"
                )
            else:
                self.tracker.state.setdefault("rests", []).append(rest)
                actions.append(
                    f"{mode} post-only {idea.side} {ticker} {count:.2f}@ {join:.2f} · {idea.rationale}"
                )
            placed += 1
        if placed == 0:
            if scored:
                actions.append("Filters passed but size/room blocked a ticket. Sitting out is a valid trade.")
            elif sat_out:
                actions.append("No idea passed the filters. Sitting out is a valid trade.")
            else:
                actions.append("Nothing in range. Sitting out is a valid trade.")
        return actions

    async def _enter_maker(self) -> list[str]:
        """Rest post-only bids on 74–93¢ favorites in the last 3 minutes.

        Taker is at (or near) break-even after fees. The edge is the spread.
        """
        book = self.playbook
        if self._open_idea_count() >= book.max_open_ideas:
            return ["Already in two ideas. Sitting out of last-3-min maker."]
        available = self._room()
        if available < book.min_stake:
            return [f"room ${available:.2f}; sitting out of last-3-min maker."]

        series = list(FIFTEEN_SERIES)
        if hourly_scan_window():
            series.extend(await self._hourly_series())

        scored: list[dict[str, Any]] = []
        for row in await self._load_candidates(series):
            item = await self._score_market(row)
            if not item:
                continue
            if item["secs"] < book.maker_min_seconds or item["secs"] > book.maker_max_seconds:
                continue
            fav = classify_favorite(
                spot=item["spot"],
                strike=item["strike"],
                yes_bid=item["yes_bid"],
                yes_ask=item["yes_ask"],
                model_yes=item["model_yes"],
            )
            if not fav:
                continue
            if not maker_spread_ok(
                fav,
                item["yes_bid"],
                item["yes_ask"],
                join_min=book.maker_join_min,
                join_max=book.maker_join_max,
                min_spread=book.maker_min_spread,
                taker_net_min=book.maker_taker_net_min,
            ):
                continue
            item["favorite"] = fav
            scored.append(item)
        scored.sort(key=lambda r: r["favorite"].model_side, reverse=True)

        from types import SimpleNamespace

        actions: list[str] = []
        mode = "LIVE" if self.live else "DRY"
        placed = 0
        for pick in scored:
            if placed >= book.maker_max_new:
                break
            if self._open_idea_count() >= book.max_open_ideas:
                break
            available = self._room()
            if available < book.min_stake:
                break
            fav = pick["favorite"]
            ticker = pick["market"]["ticker"]
            if self._already_in(ticker):
                continue
            join = fav.join_price
            cost_px = maker_contract_price(fav)
            equity = self._equity()
            cap = min(book.maker_risk_cap, book.risk_limit(equity)) * equity
            budget = min(book.kelly_stake(equity, fav.model_side, cost_px), available, cap)
            if budget < book.min_stake:
                budget = min(book.min_stake, available, cap)
            if budget < book.min_stake:
                continue
            count = contracts_for_budget(budget, cost_px)
            if count <= 0 or count * cost_px > book.risk_hard_max * equity + 1e-9:
                continue
            book_side = "bid" if fav.side == "yes" else "ask"
            payload = {
                "ticker": ticker,
                "side": book_side,
                "count": f"{count:.2f}",
                "price": f"{join:.4f}",
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "maker",
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "exchange_index": -1,
            }
            order_id = None
            fill_count = 0.0
            avg_yes = join
            if self.live:
                try:
                    resp = await self.kalshi.create_order_v2(payload)
                except httpx.HTTPStatusError as exc:
                    actions.append(f"LIVE maker failed {ticker}: {exc}")
                    continue
                fill_count = float(resp.get("fill_count") or 0)
                avg_yes = float(resp.get("average_fill_price") or join)
                order_id = resp.get("order_id")
            fill_cost = self._yes_to_cost(fav.side, avg_yes)
            rest = {
                "id": str(uuid.uuid4()),
                "loop": "maker",
                "kind": "maker_spread",
                "ticker": ticker,
                "side": fav.side,
                "price": join,
                "count": count,
                "status": "open",
                "order_id": order_id,
                "model_prob": fav.model_side,
                "model_yes": pick["model_yes"],
                "sigma": pick["sigma"],
                "spot": pick["spot"],
                "strike": pick["strike"],
                "spec_kind": pick["spec_kind"],
                "cap": pick.get("cap"),
                "asset_key": pick["asset"].key,
                "close_at": pick.get("close"),
                "paper": not self.live,
            }
            if fill_count > 0:
                idea = SimpleNamespace(side=fav.side, model_prob=fav.model_side)
                ticket = self._ticket_fields(pick, idea, fill_count, fill_cost, order_id)
                ticket["kind"] = "maker_spread"
                ticket["loop"] = "maker"
                self.tracker.state["tickets"].append(ticket)
                actions.append(
                    f"{mode} maker filled {fav.side} {ticker} {fill_count:.2f}@ {fill_cost:.2f} · {fav.rationale}"
                )
            else:
                self.tracker.state.setdefault("rests", []).append(rest)
                spread = pick["yes_ask"] - pick["yes_bid"]
                actions.append(
                    f"{mode} maker rest {fav.side} {ticker} {count:.2f}@ {join:.2f} "
                    f"(74–93¢ · spread {spread:.2f} · {fav.conviction}) · {fav.rationale}"
                )
            placed += 1
        if placed == 0:
            actions.append("No 74–93¢ favorite in the last 3 minutes. Sitting out is a valid trade.")
        return actions


async def run_scheduler(engine: CampaignEngine) -> None:
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    last_hourly = 0.0
    last_fifteen_minute = None
    last_maker_minute = None
    while True:
        now = datetime.now(et)
        loop_time = asyncio.get_running_loop().time()
        minute_key = (now.hour, now.minute)
        look = in_fifteen_entry_window(now)
        if look and minute_key != last_fifteen_minute:
            await engine.fire("fifteen")
            last_fifteen_minute = minute_key
        elif now.minute % 5 == 0 and minute_key != last_fifteen_minute:
            await engine.fire("fifteen")
            last_fifteen_minute = minute_key
        if loop_time - last_hourly >= 300:
            await engine.fire("hourly")
            last_hourly = loop_time
        if in_maker_window(now) and minute_key != last_maker_minute:
            await engine.fire("maker")
            last_maker_minute = minute_key
        await asyncio.sleep(5)
