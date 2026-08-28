from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from kalshibot.assets import identify_asset
from kalshibot.campaign.rules import (
    POT_FIFTEEN,
    POT_HOURLY,
    already_there,
    classify_favorite,
    contracts_for_budget,
    flatten_reason,
    hourly_scan_window,
    in_maker_window,
    maker_join_ok,
    maker_size,
    open_cost,
    room,
    size_for_conviction,
)
from kalshibot.campaign.tracker import Tracker
from kalshibot.campaign.universe import FIFTEEN_SERIES, is_campaign_hourly_universe, shard_for_series
from kalshibot.config import Settings, settings
from kalshibot.kalshi import KalshiClient
from kalshibot.models import parse_strike, price_threshold_prob
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
        self.tracker = Tracker(self.cfg.tracker_path, self.cfg.fifteen_bankroll, self.cfg.hourly_bankroll)
        self.live = bool(self.cfg.kalshi_live and self.kalshi.can_trade)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def status(self) -> dict[str, Any]:
        self.tracker.load()
        state = self.tracker.snapshot()
        tickets = [t for t in state["tickets"] if t.get("status") == "open"]
        pots = {}
        for name in (POT_FIFTEEN, POT_HOURLY):
            pot = state["pots"][name]
            cost = open_cost(state["tickets"], name)
            pots[name] = {
                **pot,
                "open_cost": round(cost, 4),
                "room": round(room(float(pot["bankroll"]), float(pot["realized"]), cost), 4),
            }
        return {
            "live": self.live,
            "can_trade": self.kalshi.can_trade,
            "tracker_path": str(self.tracker.path),
            "pots": pots,
            "open_tickets": tickets,
            "rests": [r for r in state.get("rests", []) if r.get("status") == "open"],
            "log": list(reversed(state.get("log", [])[-20:])),
            "updated_at": state.get("updated_at"),
        }

    async def fire(self, loop: str) -> dict[str, Any]:
        self.tracker.load()
        actions: list[str] = []
        try:
            if loop == "maker" and not in_maker_window():
                msg = "Maker window closed; stay quiet."
                actions.append(msg)
                return self._finish(loop, actions, quiet=True)

            if self.live:
                actions.extend(self._drop_practice_tickets())

            pots_to_manage = [POT_FIFTEEN, POT_HOURLY] if loop == "maker" else [loop if loop != "fifteen" else POT_FIFTEEN]
            if loop == "fifteen":
                pots_to_manage = [POT_FIFTEEN]
            elif loop == "hourly":
                pots_to_manage = [POT_HOURLY]

            quotes = await self._quotes_for_open_tickets()
            for pot in pots_to_manage:
                actions.extend(await self._manage_open(pot, quotes, loop))

            for pot in pots_to_manage:
                actions.extend(self._maybe_stop_pot(pot, loop))

            if loop == "fifteen":
                actions.extend(await self._enter_taker(POT_FIFTEEN, FIFTEEN_SERIES, loop, skip_last=self.cfg.skip_last_seconds))
            elif loop == "hourly":
                series = await self._hourly_series()
                actions.extend(await self._enter_taker(POT_HOURLY, series, loop, skip_last=self.cfg.skip_last_seconds))
            elif loop == "maker":
                actions.extend(await self._enter_maker())

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

    def _finish(self, loop: str, actions: list[str], quiet: bool) -> dict[str, Any]:
        for action in actions:
            self.tracker.note(action, loop, quiet=quiet and action == actions[-1])
        self.tracker.save()
        return {"loop": loop, "live": self.live, "actions": actions, "status": self.status()}

    def _maybe_stop_pot(self, pot: str, loop: str) -> list[str]:
        state = self.tracker.state["pots"][pot]
        realized = float(state["realized"])
        if realized <= self.cfg.pot_stop and not state["stopped"]:
            state["stopped"] = True
            state["stop_reason"] = f"realized {realized:.2f} hit stop {self.cfg.pot_stop:.2f}"
            return [f"Tell Matt: {pot} pot stopped ({state['stop_reason']})."]
        return []

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

    async def _manage_open(self, pot: str, quotes: dict[str, dict[str, float]], loop: str) -> list[str]:
        actions: list[str] = []
        for ticket in list(self.tracker.state["tickets"]):
            if ticket.get("status") != "open" or ticket.get("pot") != pot:
                continue
            q = quotes.get(ticket["ticker"])
            if not q:
                continue
            reason = flatten_reason(ticket, q["yes_bid"], q["yes_ask"])
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
            resp = await self.kalshi.create_order_v2(payload)
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
        pot = ticket["pot"]
        self.tracker.state["pots"][pot]["realized"] = round(
            float(self.tracker.state["pots"][pot]["realized"]) + pnl, 4
        )
        mode = "LIVE" if self.live else "DRY"
        return f"{mode} flatten {ticket['ticker']} {ticket['side']} {reason} pnl {pnl:+.2f}"

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
            events = await self.kalshi.open_events(ticker, limit=2)
            for event in events:
                for market in event.get("markets") or []:
                    if str(market.get("status") or "").lower() not in {"active", "open"}:
                        continue
                    out.append({"series": ticker, "event": event, "market": market})
        return out

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
        years = max((secs or 0) / (365.25 * 24 * 3600), 1e-8)
        model_yes = price_threshold_prob(spec, spot, years, asset.annual_vol)
        if model_yes is None:
            model_yes = 0.5 if spec.kind == "unknown" else (0.99 if spot >= strike else 0.01)
        fav = classify_favorite(
            spot=spot,
            strike=strike,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            model_yes=model_yes,
        )
        if not fav:
            return None
        return {
            "series": series,
            "event": event,
            "market": market,
            "asset": asset,
            "spot": spot,
            "strike": strike,
            "secs": secs,
            "favorite": fav,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "exchange_index": shard_for_series(series, event.get("title") or ""),
        }

    async def _enter_taker(self, pot: str, series_tickers: list[str], loop: str, skip_last: float) -> list[str]:
        actions: list[str] = []
        pot_state = self.tracker.state["pots"][pot]
        if pot_state.get("stopped"):
            return actions
        cost = open_cost(self.tracker.state["tickets"], pot)
        available = room(float(pot_state["bankroll"]), float(pot_state["realized"]), cost)
        min_room = 0.50 if pot == POT_FIFTEEN else 1.0
        if available < min_room:
            return [f"{pot} room ${available:.2f} < ${min_room:.2f}; skip new {pot}."]

        scored: list[dict[str, Any]] = []
        for row in await self._load_candidates(list(series_tickers)):
            item = await self._score_market(row)
            if not item:
                continue
            if item["secs"] < skip_last:
                continue
            if not item["favorite"].is_real_or_better:
                continue
            scored.append(item)
        scored.sort(key=lambda r: r["favorite"].model_side, reverse=True)
        mode = "LIVE" if self.live else "DRY"
        placed = 0
        for pick in scored:
            if placed >= 3:
                break
            pot_state = self.tracker.state["pots"][pot]
            available = room(
                float(pot_state["bankroll"]),
                float(pot_state["realized"]),
                open_cost(self.tracker.state["tickets"], pot),
            )
            if available < min_room:
                break
            fav = pick["favorite"]
            budget = min(size_for_conviction(pot, fav.conviction), available)
            if budget < 0.50:
                continue
            if already_there(fav):
                continue
            count = contracts_for_budget(budget, fav.take_price)
            ticker = pick["market"]["ticker"]
            book_side = "bid" if fav.side == "yes" else "ask"
            payload = {
                "ticker": ticker,
                "side": book_side,
                "count": f"{count:.2f}",
                "price": f"{fav.take_price:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": False,
                "client_order_id": str(uuid.uuid4()),
                "exchange_index": -1,
            }
            fill_count = 0.0
            avg = fav.take_price
            order_id = None
            if self.live:
                resp = await self.kalshi.create_order_v2(payload)
                fill_count = float(resp.get("fill_count") or 0)
                avg = float(resp.get("average_fill_price") or fav.take_price)
                order_id = resp.get("order_id")
            else:
                fill_count = count
            if fill_count <= 0:
                actions.append(f"{mode} IOC {ticker} {fav.side} no fill")
                continue
            ticket = {
                "id": str(uuid.uuid4()),
                "pot": pot,
                "ticker": ticker,
                "side": fav.side,
                "fill": avg,
                "count": fill_count,
                "cost": round(avg * fill_count, 4),
                "filled_at": datetime.now(timezone.utc).isoformat(),
                "status": "open",
                "exchange_index": pick["exchange_index"],
                "order_id": order_id,
                "conviction": fav.conviction,
                "paper": not self.live,
            }
            self.tracker.state["tickets"].append(ticket)
            rest_msg = await self._rest_99(ticket)
            actions.append(
                f"{mode} IOC {fav.conviction} {fav.side} {ticker} {fill_count:.2f}@ {avg:.2f} · {fav.rationale}"
            )
            if rest_msg:
                actions.append(rest_msg)
            placed += 1
        return actions

    async def _rest_99(self, ticket: dict[str, Any]) -> str | None:
        side = "ask" if ticket["side"] == "yes" else "bid"
        payload = {
            "ticker": ticket["ticker"],
            "side": side,
            "count": f"{float(ticket['count']):.2f}",
            "price": "0.9900",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "maker",
            "post_only": True,
            "reduce_only": True,
            "client_order_id": str(uuid.uuid4()),
            "exchange_index": -1,
        }
        order_id = None
        if self.live:
            if ticket.get("paper") or not ticket.get("order_id"):
                return None
            resp = await self.kalshi.create_order_v2(payload)
            order_id = resp.get("order_id")
        rest = {
            "id": str(uuid.uuid4()),
            "ticket_id": ticket["id"],
            "ticker": ticket["ticker"],
            "price": 0.99,
            "status": "open",
            "order_id": order_id,
        }
        self.tracker.state.setdefault("rests", []).append(rest)
        mode = "LIVE" if self.live else "DRY"
        return f"{mode} rest 99¢ post-only on {ticket['ticker']} (never rest under the bid)"

    async def _enter_maker(self) -> list[str]:
        actions: list[str] = []
        series = list(FIFTEEN_SERIES)
        if hourly_scan_window():
            series.extend(await self._hourly_series())
        scored: list[dict[str, Any]] = []
        for row in await self._load_candidates(series):
            item = await self._score_market(row)
            if not item or item["secs"] < self.cfg.maker_skip_last_seconds:
                continue
            if not maker_join_ok(item["favorite"]):
                continue
            scored.append(item)
        scored.sort(key=lambda r: r["favorite"].model_side, reverse=True)

        for item in scored[:4]:
            fav = item["favorite"]
            ticker = item["market"]["ticker"]
            # 15m series use fifteen pot; hourly tickers use hourly pot
            pot = POT_HOURLY if item["series"] not in FIFTEEN_SERIES else POT_FIFTEEN
            pot_state = self.tracker.state["pots"][pot]
            if pot_state.get("stopped"):
                continue
            cost = open_cost(self.tracker.state["tickets"], pot)
            available = room(float(pot_state["bankroll"]), float(pot_state["realized"]), cost)
            min_room = 0.50 if pot == POT_FIFTEEN else 1.0
            if available < min_room:
                continue
            budget = min(maker_size(fav.conviction), available)
            count = contracts_for_budget(budget, fav.join_price)
            book_side = "bid" if fav.side == "yes" else "ask"
            payload = {
                "ticker": ticker,
                "side": book_side,
                "count": f"{count:.2f}",
                "price": f"{fav.join_price:.4f}",
                "time_in_force": "good_till_canceled",
                "self_trade_prevention_type": "maker",
                "post_only": True,
                "client_order_id": str(uuid.uuid4()),
                "exchange_index": -1,
            }
            order_id = None
            if self.live:
                resp = await self.kalshi.create_order_v2(payload)
                order_id = resp.get("order_id")
            rest = {
                "id": str(uuid.uuid4()),
                "pot": pot,
                "ticker": ticker,
                "side": fav.side,
                "price": fav.join_price,
                "count": count,
                "status": "open",
                "order_id": order_id,
                "kind": "maker_join",
            }
            self.tracker.state.setdefault("rests", []).append(rest)
            mode = "LIVE" if self.live else "DRY"
            actions.append(
                f"{mode} maker rest {fav.side} {ticker} {count:.2f}@ {fav.join_price:.2f} ({fav.conviction})"
            )
        return actions


async def run_scheduler(engine: CampaignEngine) -> None:
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    last_fifteen = 0.0
    last_hourly = 0.0
    last_maker_minute = None
    while True:
        now = datetime.now(et)
        loop_time = asyncio.get_running_loop().time()
        if loop_time - last_fifteen >= 180:
            await engine.fire("fifteen")
            last_fifteen = loop_time
        if loop_time - last_hourly >= 300:
            await engine.fire("hourly")
            last_hourly = loop_time
        minute_key = (now.hour, now.minute)
        if in_maker_window(now) and minute_key != last_maker_minute:
            await engine.fire("maker")
            last_maker_minute = minute_key
        await asyncio.sleep(5)
