import logging
import time
from typing import Optional, Tuple

from .config import SymbolConfig
from .exchange import BinanceFuturesClient, BinanceAPIError
from .grid import GridState, level_qty
from .indicators import BollingerBands
from .state import StateStore
from .utils import round_up, round_to_step


class GridBollingerStrategy:
    def __init__(
        self,
        client: BinanceFuturesClient,
        cfg: SymbolConfig,
        logger: logging.Logger,
        state_store: StateStore,
        drain_mode: bool = False,
        on_basket_close=None,
        basket_recorder=None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.bb = BollingerBands(cfg.bollinger.period, cfg.bollinger.stddev)
        self.state_store = state_store
        self.state = self.state_store.load()
        self.drain_mode = drain_mode
        self.on_basket_close = on_basket_close
        self.basket_recorder = basket_recorder
        if self.state.direction:
            self.log.info("Loaded state: %s", self.state)

    def seed_indicator(self, closes) -> None:
        for close in closes[-self.cfg.bollinger.period :]:
            self.bb.add(float(close))

    def _new_basket_id(self, opened_at: Optional[float] = None) -> int:
        """
        Basket ids are derived from the first fill timestamp (UTC) down to the second.
        """
        ts = opened_at or time.time()
        return int(time.strftime("%y%m%d%H%M%S", time.gmtime(ts)))

    def reconcile_position(self, price: float, pos_info: Optional[dict], open_orders: Optional[list] = None) -> None:
        position_qty = float(pos_info.get("positionAmt", 0)) if pos_info else 0.0
        if abs(position_qty) < 1e-8:
            if self.state.direction:
                self.log.info("No position on exchange; clearing saved grid state.")
                self.state.reset()
                self.state_store.save(self.state)
            return
        if self.state.direction:
            return

        direction = "long" if position_qty > 0 else "short"
        self.state.direction = direction
        abs_qty = abs(position_qty)
        level = 0
        cumulative = 0.0
        while cumulative + 1e-9 < abs_qty and level < self.cfg.max_levels:
            level += 1
            cumulative += level_qty(
                level,
                self.cfg.grid.base_qty,
                self.cfg.grid.repeat_every,
                self.cfg.grid.multiplier,
                self.cfg.min_qty_step,
            )
        # If we have open reduce-only orders, use them to infer depth too
        if open_orders:
            ro_count = sum(1 for o in open_orders if str(o.get("reduceOnly", "false")).lower() == "true")
            if ro_count > level:
                level = min(ro_count, self.cfg.max_levels)
        self.state.levels_filled = level
        inferred_entry = float(pos_info.get("entryPrice", price)) if pos_info else price
        self.state.last_entry_price = inferred_entry or price
        spacing = self.cfg.grid_spacing_usd
        self.state.next_entry_price = inferred_entry + spacing if direction == "short" else inferred_entry - spacing
        if self.state.basket_id == 0:
            now_ts = time.time()
            if self.state.basket_open_ts is None:
                self.state.basket_open_ts = now_ts
            self.state.basket_id = self._new_basket_id(self.state.basket_open_ts)
        # If we have open reduce-only orders, track their ids for visibility.
        if open_orders:
            tp_ids = []
            for o in open_orders:
                if str(o.get("reduceOnly", "false")).lower() == "true":
                    try:
                        tp_ids.append(int(o.get("orderId")))
                    except Exception:
                        continue
            if tp_ids:
                self.state.tp_order_ids = tp_ids
        if not self.state.entry_order_ids:
            populated = self._populate_entries_from_trades(abs_qty, direction)
            if populated:
                self.state_store.save(self.state)
        self.log.info(
            "Reconstructed grid from live position qty=%.6f as level %d direction %s", position_qty, level, direction
        )
        self.state_store.save(self.state)

    def _tp_price(self, entry_price: float, qty: float, direction: str) -> float:
        lots = max(qty / self.cfg.lot_size, 1e-9)
        target = lots * self.cfg.target_profit_usd
        delta = target / qty
        if direction == "short":
            raw = entry_price - delta
        else:
            raw = entry_price + delta
        return round_to_step(raw, self.cfg.price_tick_size)

    def _place_tp(self, entry_price: float, qty: float, direction: str, level: int) -> dict:
        tp_price = self._tp_price(entry_price, qty, direction)
        side = "BUY" if direction == "short" else "SELL"
        resp = self.client.place_limit_tp(self.cfg.name, side, qty, tp_price)
        self.log.info(
            "Basket #%d level %d TP placed %s qty=%.6f tp=%.2f resp=%s",
            self.state.basket_id,
            level,
            side,
            qty,
            tp_price,
            resp,
        )
        try:
            order_id = int(resp.get("orderId"))
            self.state.tp_order_ids.append(order_id)
        except Exception:
            pass
        return resp

    def _execute_market(self, side: str, qty: float, price: float) -> tuple[dict, float, float]:
        """
        Place a market order and ensure it filled by checking executedQty,
        otherwise re-check position delta to infer fill.
        """
        try:
            pre_pos = self.client.get_position_info(self.cfg.name)
            pre_qty = float(pre_pos.get("positionAmt", 0))
        except Exception:
            pre_qty = 0.0

        order = self.client.place_market_order(self.cfg.name, side, qty)
        executed = float(order.get("executedQty", 0))
        entry_price = float(order.get("avgPrice") or price)

        if executed <= 0:
            try:
                post_pos = self.client.get_position_info(self.cfg.name)
                post_qty = float(post_pos.get("positionAmt", 0))
                delta = abs(post_qty - pre_qty)
                if delta >= qty * 0.9:
                    executed = delta
                    entry_price = float(post_pos.get("entryPrice") or entry_price or price)
                    order["executedQty"] = executed
                    order["avgPrice"] = entry_price
            except Exception:
                pass

        # Try to refine fill price using trades if Binance returned zero/placeholder.
        try:
            if order.get("orderId"):
                trades = self.client.get_user_trades(self.cfg.name, int(order["orderId"]), limit=5)
                total_qty = 0.0
                total_quote = 0.0
                for t in trades:
                    if int(t.get("orderId", 0)) != int(order["orderId"]):
                        continue
                    tqty = float(t.get("qty", 0) or 0)
                    tquote = float(t.get("quoteQty", 0) or 0)
                    total_qty += tqty
                    total_quote += tquote
                if total_qty > 0 and total_quote > 0:
                    entry_price = total_quote / total_qty
        except Exception:
            pass

        if executed <= 0:
            raise BinanceAPIError(f"Market order not filled: {order}")
        if entry_price <= 0:
            entry_price = price
        return order, executed, entry_price

    def _tp_profit(self, entry_price: float, tp_price: float, qty: float, direction: str) -> float:
        if direction == "short":
            return (entry_price - tp_price) * qty
        return (tp_price - entry_price) * qty

    def _start_position(self, price: float, direction: str) -> None:
        qty = level_qty(1, self.cfg.grid.base_qty, self.cfg.grid.repeat_every, self.cfg.grid.multiplier, self.cfg.min_qty_step)
        min_qty = round_up(self.cfg.min_notional_usd / price, self.cfg.min_qty_step)
        if qty < min_qty:
            self.log.info("Adjusted qty to meet min notional: %.6f -> %.6f", qty, min_qty)
            qty = min_qty
        side = "SELL" if direction == "short" else "BUY"
        try:
            account = self.client.get_account()
            wallet = float(account.get("totalWalletBalance", 0))
        except Exception:
            wallet = None
        order, executed, entry_price = self._execute_market(side, qty, price)
        order_ts_ms = order.get("updateTime") or order.get("transactTime")
        if order_ts_ms:
            try:
                self.state.basket_open_ts = order_ts_ms / 1000
            except Exception:
                pass
        try:
            order_id = int(order.get("orderId"))
            self.state.entry_order_ids.append(order_id)
        except Exception:
            pass
        self.state.direction = direction
        self.state.last_entry_price = entry_price
        self.state.levels_filled = 1
        spacing = self.cfg.grid_spacing_usd
        self.state.next_entry_price = entry_price + spacing if direction == "short" else entry_price - spacing
        now_ts = time.time()
        if self.state.basket_open_ts is None:
            self.state.basket_open_ts = now_ts
        if self.state.basket_id == 0:
            self.state.basket_id = self._new_basket_id(self.state.basket_open_ts)
        self.state.basket_start_balance = wallet
        self.state.max_volume = max(self.state.max_volume, qty)
        self.state.worst_drawdown = 0.0
        self.log.info(
            "Basket #%d opened %s lvl1 qty=%.6f at %.2f next_entry=%.2f resp=%s",
            self.state.basket_id,
            direction,
            qty,
            entry_price,
            self.state.next_entry_price,
            order,
        )
        self._place_tp(entry_price, qty, direction, level=1)
        self.state_store.save(self.state)

    def _extend_grid_if_needed(self, price: float) -> None:
        spacing = self.cfg.grid_spacing_usd
        if not self.state.direction:
            return
        if self.state.levels_filled >= self.cfg.max_levels or spacing <= 0:
            return
        # Always anchor next entry exactly one spacing from last fill and only enter when we are beyond that distance.
        while True:
            if self.state.last_entry_price is None:
                return
            target_price = (
                self.state.last_entry_price + spacing if self.state.direction == "short" else self.state.last_entry_price - spacing
            )
            self.state.next_entry_price = target_price
            should_enter = False
            if self.state.direction == "short" and price >= target_price:
                should_enter = True
            elif self.state.direction == "long" and price <= target_price:
                should_enter = True

            if not should_enter:
                self.state_store.save(self.state)
                return
            if self.state.levels_filled >= self.cfg.max_levels:
                self.state_store.save(self.state)
                return

            level = self.state.levels_filled + 1
            qty = level_qty(level, self.cfg.grid.base_qty, self.cfg.grid.repeat_every, self.cfg.grid.multiplier, self.cfg.min_qty_step)
            min_qty = round_up(self.cfg.min_notional_usd / price, self.cfg.min_qty_step)
            if qty < min_qty:
                self.log.info("Adjusted qty to meet min notional: %.6f -> %.6f", qty, min_qty)
                qty = min_qty
            side = "SELL" if self.state.direction == "short" else "BUY"
            order, executed, entry_price = self._execute_market(side, qty, price)
            try:
                order_id = int(order.get("orderId"))
                self.state.entry_order_ids.append(order_id)
            except Exception:
                pass
            self.state.levels_filled = level
            self.state.last_entry_price = entry_price
            self.state.next_entry_price = entry_price + spacing if self.state.direction == "short" else entry_price - spacing
            cumulative_qty = sum(
                level_qty(i, self.cfg.grid.base_qty, self.cfg.grid.repeat_every, self.cfg.grid.multiplier, self.cfg.min_qty_step)
                for i in range(1, level + 1)
            )
            self.state.max_volume = max(self.state.max_volume, cumulative_qty)
            self.log.info(
                "Basket #%d extended %s lvl%d qty=%.6f at %.2f next_entry=%.2f resp=%s",
                self.state.basket_id,
                self.state.direction,
                level,
                qty,
                entry_price,
                self.state.next_entry_price,
                order,
            )
            self._place_tp(entry_price, qty, self.state.direction, level=level)
            self.state_store.save(self.state)

    def _ensure_basket_time_from_entries(self) -> None:
        if self.state.basket_open_ts is not None and self.state.basket_id:
            return
        if not self.state.entry_order_ids:
            return
        first_id = min(self.state.entry_order_ids)
        try:
            order = self.client.get_order(self.cfg.name, first_id)
            ts = order.get("updateTime") or order.get("time") or order.get("transactTime")
            if ts:
                self.state.basket_open_ts = ts / 1000
                self.state.basket_id = self._new_basket_id(self.state.basket_open_ts)
                self.state_store.save(self.state)
        except Exception:
            return

    def _log_orders_snapshot(self, open_orders: list) -> None:
        if not open_orders and not (self.state.entry_order_ids or self.state.tp_order_ids):
            return
        header = "Orders: id         side   px        qty        reduceOnly type"
        rows = []
        for o in open_orders:
            oid = o.get("orderId", "-")
            side = o.get("side", "-")
            price = o.get("price") or o.get("avgPrice") or "-"
            qty = o.get("origQty") or o.get("executedQty") or "-"
            reduce = str(o.get("reduceOnly", "false")).lower()
            otype = o.get("type", "-")
            rows.append(f"        {oid:<10} {side:<5} {price:<10} {qty:<10} {reduce:<10} {otype}")
        if rows:
            self.log.info(header)
            for r in rows:
                self.log.info(r)
        if self.state.entry_order_ids or self.state.tp_order_ids:
            self.log.info(
                "State order ids: entries=%s tps=%s",
                self.state.entry_order_ids if self.state.entry_order_ids else "[]",
                self.state.tp_order_ids if self.state.tp_order_ids else "[]",
            )

    def _log_fills_snapshot(self) -> None:
        if not (self.state.entry_order_ids or self.state.basket_open_ts):
            return
        try:
            trades = self.client.get_user_trades(self.cfg.name, limit=50)
        except Exception:
            return
        rows = []
        for t in trades:
            try:
                oid = int(t.get("orderId", 0))
            except Exception:
                continue
            if self.state.entry_order_ids and oid not in self.state.entry_order_ids:
                continue
            ts_ms = t.get("time")
            if ts_ms and self.state.basket_open_ts and ts_ms / 1000 < self.state.basket_open_ts - 1:
                continue
            price = t.get("price")
            qty = t.get("qty")
            quote = t.get("quoteQty")
            maker = t.get("maker", False)
            rows.append(
                f"        {oid:<10} {time.strftime('%H:%M:%S', time.gmtime(ts_ms/1000)) if ts_ms else '-':<9} {price:<10} {qty:<10} {quote:<10} {'maker' if maker else 'taker'}"
            )
        if rows:
            self.log.info("Fills:  id         time      price      qty        quote      role")
            for r in rows:
                self.log.info(r)

    def _populate_entries_from_trades(self, position_qty: float, direction: str) -> bool:
        """
        Infer active entry trades for the current open position by finding the last
        flat point (net position zero) and taking trades after that. This avoids
        including older, fully-closed baskets.
        """
        if position_qty <= 0 or not direction:
            return False
        try:
            trades_raw = self.client.get_user_trades(self.cfg.name, limit=200)
        except Exception:
            return False
        trades = []
        for t in trades_raw:
            try:
                trades.append(
                    {
                        "buyer": bool(t.get("buyer", False)),
                        "qty": float(t.get("qty", 0) or 0),
                        "price": float(t.get("price", 0) or 0),
                        "fee": float(t.get("commission", 0) or 0),
                        "time": int(t.get("time", 0)),
                        "oid": int(t.get("orderId", 0)),
                    }
                )
            except Exception:
                continue
        if not trades:
            return False
        trades.sort(key=lambda x: x["time"])  # chronological
        # Start after the last opposite-side trade (last exit) to avoid older baskets.
        last_opposite_idx = -1
        for idx, t in enumerate(trades):
            if (direction == "long" and not t["buyer"]) or (direction == "short" and t["buyer"]):
                last_opposite_idx = idx
        active_slice = trades[last_opposite_idx + 1 :] if last_opposite_idx + 1 < len(trades) else trades
        if not active_slice:
            return False
        # From the active slice, pick minimal recent trades (reverse) that sum to the open size.
        picked: list[dict] = []
        net = 0.0
        tol = max(1e-6, position_qty * 0.01)
        for t in reversed(active_slice):
            delta = t["qty"] if (direction == "long" and t["buyer"]) or (direction == "short" and not t["buyer"]) else -t["qty"]
            net += delta
            picked.append(t)
            if net >= position_qty - tol:
                break
        if net < position_qty - tol:
            return False
        picked.reverse()
        entry_trades = [t for t in picked if (t["buyer"] if direction == "long" else not t["buyer"])]
        if not entry_trades:
            return False
        self.state.entry_order_ids = list({t["oid"] for t in entry_trades})
        self.state.last_entry_price = entry_trades[-1]["price"] or self.state.last_entry_price
        if entry_trades[0]["time"]:
            self.state.basket_open_ts = entry_trades[0]["time"] / 1000
        spacing = self.cfg.grid_spacing_usd
        if spacing > 0 and self.state.last_entry_price:
            self.state.next_entry_price = (
                self.state.last_entry_price - spacing if direction == "long" else self.state.last_entry_price + spacing
            )
        if self.state.basket_open_ts:
            self.state.basket_id = self._new_basket_id(self.state.basket_open_ts)
        return True

    def _log_basket_summary(
        self,
        qty: float,
        avg_entry: float,
        last_entry: float,
        mark_price: float,
        direction: str,
    ) -> None:
        if qty <= 0 or not direction:
            return
        target_tp = self._tp_price(avg_entry if avg_entry > 0 else mark_price, qty, direction)
        tp_pnl = self._tp_profit(avg_entry if avg_entry > 0 else mark_price, target_tp, qty, direction)
        spacing = self.cfg.grid_spacing_usd
        next_add = last_entry - spacing if direction == "long" else last_entry + spacing
        tp_dist = target_tp - mark_price if direction == "long" else mark_price - target_tp
        add_dist = next_add - mark_price if direction == "long" else mark_price - next_add
        header = "Basket Summary: qty     avg_entry last_entry mark     tp_price tp_pnl  next_add add_dist tp_dist"
        row = (
            f"                 {qty:7.4f} {avg_entry:9.2f} {last_entry:9.2f} "
            f"{mark_price:7.2f} {target_tp:8.2f} {tp_pnl:6.4f} {next_add:8.2f} {add_dist:7.2f} {tp_dist:7.2f}"
        )
        self.log.info(header)
        self.log.info(row)

    def _collect_entry_trades(self) -> list:
        trades = []
        if not self.state.entry_order_ids and self.state.basket_open_ts is None:
            return trades
        try:
            raw = self.client.get_user_trades(self.cfg.name, limit=50)
        except Exception:
            return trades
        for t in raw:
            try:
                oid = int(t.get("orderId", 0))
                ts_ms = int(t.get("time", 0))
                price = float(t.get("price", 0) or 0)
                qty = float(t.get("qty", 0) or 0)
                fee = float(t.get("commission", 0) or 0)
            except Exception:
                continue
            if self.state.entry_order_ids and oid not in self.state.entry_order_ids:
                continue
            if self.state.basket_open_ts and ts_ms / 1000 < self.state.basket_open_ts - 1:
                continue
            trades.append({"oid": oid, "time": ts_ms, "price": price, "qty": qty, "fee": fee})
        trades.sort(key=lambda x: x["time"])
        return trades

    def _log_basket_panel(
        self,
        qty: float,
        avg_entry: float,
        last_entry: float,
        mark_price: float,
        direction: str,
        tp_price: float,
        tp_pnl: float,
    ) -> None:
        trades = self._collect_entry_trades()
        if qty <= 0 or not direction:
            return
        total_fee = sum(t["fee"] for t in trades) if trades else 0.0
        next_add = last_entry - self.cfg.grid_spacing_usd if direction == "long" else last_entry + self.cfg.grid_spacing_usd
        symbol_label = self.cfg.name
        if symbol_label.upper().endswith("USDT"):
            symbol_label = symbol_label[:-4]
        lines = []
        header = f"{symbol_label} BASKET #{self.state.basket_id} | {len(trades)} Orders | {qty:.3f} {symbol_label} | Avg: {avg_entry:,.2f}"
        border = "+" + "-" * len(header) + "+"
        lines.append(border)
        lines.append("| " + header + " |")
        lines.append(border)
        lines.append(" Order # | Time     | Price    | Size      | Fee")
        lines.append("---------+----------+----------+-----------+-----------")
        for idx, t in enumerate(trades, start=1):
            timestr = time.strftime("%H:%M:%S", time.gmtime(t["time"] / 1000)) if t["time"] else "-"
            lines.append(
                f" {idx:<7} | {timestr:<8} | {t['price']:>8.2f} | {t['qty']:>7.3f}   | {t['fee']:>7.4f}"
            )
        lines.append("---------+----------+----------+-----------+-----------")
        lines.append(f" TOTAL   |          |          | {qty:>7.3f}   | {total_fee:>7.4f}")
        lines.append("")
        pnl = (mark_price - avg_entry) * qty if direction == "long" else (avg_entry - mark_price) * qty
        tp_dist = tp_price - mark_price if direction == "long" else mark_price - tp_price
        add_dist = next_add - mark_price if direction == "long" else mark_price - next_add
        lines.append(f" Avg Price    | {avg_entry:>8.2f}")
        lines.append(f" Break Even   | {avg_entry + (total_fee / qty if qty else 0):>8.2f}")
        lines.append(f" Mark         | {mark_price:>8.2f}")
        lines.append(f" Next Add     | {next_add:>8.2f} (dist {add_dist:>.2f})")
        lines.append(f" PnL          | {pnl:>8.4f} USDT")
        lines.append(f" TP           | {tp_price:>8.2f} (pnl {tp_pnl:>.4f}, dist {tp_dist:>.2f})")
        for line in lines:
            self.log.info(line)

    def _maybe_reset_state(self, position_qty: float) -> None:
        if abs(position_qty) < 1e-8 and self.state.direction:
            pnl = None
            try:
                account = self.client.get_account()
                wallet = float(account.get("totalWalletBalance", 0))
                if self.state.basket_start_balance is not None:
                    pnl = wallet - self.state.basket_start_balance
            except Exception:
                wallet = None
            summary = {
                "basket_id": self.state.basket_id,
                "levels": self.state.levels_filled,
                "max_volume_eth": self.state.max_volume,
                "worst_drawdown": self.state.worst_drawdown,
                "pnl": pnl,
                "direction": self.state.direction,
                "open_at": None if self.state.basket_open_ts is None else time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.state.basket_open_ts)
                ),
            }
            self.log.info(
                "Basket #%d closed. Levels=%d MaxVol=%.6f WorstDD=%.4f PnL=%s",
                summary["basket_id"],
                summary["levels"],
                summary["max_volume_eth"],
                summary["worst_drawdown"],
                f"{summary['pnl']:.2f}" if summary["pnl"] is not None else "unknown",
            )
            if self.on_basket_close:
                try:
                    self.on_basket_close(summary)
                except Exception as exc:  # noqa: BLE001
                    self.log.error("on_basket_close callback failed: %s", exc)
            if self.basket_recorder:
                try:
                    self.basket_recorder.append(self.cfg.name, summary)
                except Exception as exc:  # noqa: BLE001
                    self.log.error("Recording basket summary failed: %s", exc)
            self.state.reset()
            if self.cfg.cooldown_minutes > 0:
                self.state.cooldown_until_ts = time.time() + self.cfg.cooldown_minutes * 60
                self.log.info("Cooldown active for %d minutes", self.cfg.cooldown_minutes)
                self.state_store.save(self.state)

    def on_price(self, price: float) -> None:
        self.bb.add(price)
        bands: Optional[Tuple[float, float, float]] = self.bb.bands()
        try:
            open_orders = self.client.get_open_orders(self.cfg.name)
        except Exception:
            open_orders = []
        pos_info = self.client.get_position_info(self.cfg.name)
        position_qty = float(pos_info.get("positionAmt", 0) or 0)
        mark_price = float(pos_info.get("markPrice", price) or price)
        unrealized = float(pos_info.get("unRealizedProfit", 0.0) or 0.0)
        entry_price = float(pos_info.get("entryPrice", 0.0) or 0.0)
        self._ensure_basket_time_from_entries()
        if not self.state.entry_order_ids and abs(position_qty) > 0 and self.state.direction:
            if self._populate_entries_from_trades(abs(position_qty), self.state.direction):
                self.state_store.save(self.state)
        if self.state.last_entry_price is None and entry_price:
            self.state.last_entry_price = entry_price
            spacing = self.cfg.grid_spacing_usd
            if spacing > 0:
                self.state.next_entry_price = (
                    entry_price + spacing if self.state.direction == "short" else entry_price - spacing
                )
            self.state_store.save(self.state)
        now = time.time()
        notional = abs(position_qty) * mark_price
        if notional > 0:
            drawdown = unrealized / notional
            if drawdown < self.state.worst_drawdown:
                self.state.worst_drawdown = drawdown
                self.state_store.save(self.state)
            if abs(position_qty) > self.state.max_volume:
                self.state.max_volume = abs(position_qty)
                self.state_store.save(self.state)
        # Position snapshot for visibility
        if abs(position_qty) > 0:
            best_tp = None
            ro_orders = [
                o for o in open_orders if str(o.get("reduceOnly", "false")).lower() == "true"
            ]
            if ro_orders:
                prices = [float(o.get("price", 0) or 0) for o in ro_orders]
                if self.state.direction == "long":
                    best_tp = min(prices)
                else:
                    best_tp = max(prices)

            dist = None
            if best_tp:
                dist = best_tp - mark_price if self.state.direction == "long" else mark_price - best_tp
            profit_at_tp = None
            if best_tp:
                profit_at_tp = self._tp_profit(entry_price if entry_price > 0 else mark_price, best_tp, abs(position_qty), self.state.direction or "long")
            throttle = getattr(self, "_log_throttle", 60)
            orders_throttle = getattr(self, "_orders_log_throttle", throttle)
            fills_throttle = getattr(self, "_fills_log_throttle", orders_throttle)
            summary_throttle = getattr(self, "_summary_log_throttle", throttle)
            panel_throttle = getattr(self, "_panel_log_throttle", summary_throttle)
            if now - getattr(self, "_last_pos_log", 0) >= throttle:
                self._last_pos_log = now
                add_price = self.state.next_entry_price or 0.0
                add_dist = add_price - mark_price if self.state.direction == "long" else mark_price - add_price if add_price else None
                avg_entry = entry_price
                last_entry = self.state.last_entry_price or entry_price
                self.log.info(
                    "POS | basket=%s | dir=%-5s | pos=%7.4f | avg=%7.2f | last=%7.2f | mark=%7.2f | uPnL=%8.4f | lvl=%2d | next=%7.2f | add_dist=%7s | TP=%7s | tp_dist=%7s | tp_pnl=%8s | openTPs=%d",
                    self.state.basket_id,
                    (self.state.direction or "-"),
                    position_qty,
                    avg_entry,
                    last_entry,
                    mark_price,
                    unrealized,
                    self.state.levels_filled,
                    self.state.next_entry_price or 0.0,
                    f"{add_dist:.2f}" if add_dist is not None else "-",
                    f"{best_tp:.2f}" if best_tp else "-",
                    f"{dist:.2f}" if dist is not None else "-",
                    f"{profit_at_tp:.4f}" if profit_at_tp is not None else "-",
                    len(ro_orders),
                )
            if now - getattr(self, "_last_orders_log", 0) >= orders_throttle:
                self._last_orders_log = now
                self._log_orders_snapshot(open_orders)
            if now - getattr(self, "_last_fills_log", 0) >= fills_throttle:
                self._last_fills_log = now
                self._log_fills_snapshot()
            if now - getattr(self, "_last_summary_log", 0) >= summary_throttle:
                self._last_summary_log = now
                last_entry = self.state.last_entry_price or entry_price or mark_price
                self._log_basket_summary(abs(position_qty), entry_price if entry_price > 0 else mark_price, last_entry, mark_price, self.state.direction)
            if now - getattr(self, "_last_panel_log", 0) >= panel_throttle:
                self._last_panel_log = now
                last_entry = self.state.last_entry_price or entry_price or mark_price
                target_tp = self._tp_price(entry_price if entry_price > 0 else mark_price, abs(position_qty), self.state.direction or "long")
                tp_val = self._tp_profit(entry_price if entry_price > 0 else mark_price, target_tp, abs(position_qty), self.state.direction or "long")
                self._log_basket_panel(abs(position_qty), entry_price if entry_price > 0 else mark_price, last_entry, mark_price, self.state.direction, target_tp, tp_val)
            # Ensure TP aligns with current config/size
            try:
                target_tp = self._tp_price(entry_price if entry_price > 0 else mark_price, abs(position_qty), self.state.direction or "long")
                needs_replace = False
                if len(ro_orders) != 1:
                    needs_replace = True
                else:
                    o = ro_orders[0]
                    oqty = float(o.get("origQty", 0) or 0)
                    oprice = float(o.get("price", 0) or 0)
                    qty_diff = abs(oqty - abs(position_qty))
                    price_diff = abs(oprice - target_tp)
                    if qty_diff > self.cfg.min_qty_step / 2 or price_diff > self.cfg.price_tick_size:
                        needs_replace = True
                if needs_replace:
                    self.log.info("Resetting TP to match current position/target.")
                    try:
                        self.client.cancel_all_open_orders(self.cfg.name)
                    except Exception as exc:
                        self.log.warning("Failed to cancel open orders: %s", exc)
                    tp_side = "BUY" if self.state.direction == "short" else "SELL"
                    resp = self.client.place_limit_tp(self.cfg.name, tp_side, abs(position_qty), target_tp)
                    self.log.info("Placed TP qty=%.6f price=%.2f resp=%s", abs(position_qty), target_tp, resp)
            except Exception as exc:
                self.log.warning("Could not ensure TP placement: %s", exc)
        self._maybe_reset_state(position_qty)
        if bands is None:
            return

        lower, mid, upper = bands
        if abs(position_qty) < 1e-8:
            self.log.info(
                "Tick price=%.2f BB: lower=%.2f mid=%.2f upper=%.2f dir=%s levels=%d next=%.2f cooldown=%s",
                price,
                lower,
                mid,
                upper,
                self.state.direction or "-",
                self.state.levels_filled,
                self.state.next_entry_price or 0.0,
                f"{int(self.state.cooldown_until_ts - time.time())}s" if self.state.cooldown_until_ts else "none",
            )

        if not self.state.direction:
            if self.drain_mode:
                return
            if self.state.cooldown_until_ts and time.time() < self.state.cooldown_until_ts:
                return
            if price > upper:
                self._start_position(price, "short")
            elif price < lower:
                self._start_position(price, "long")
            return

        # Use mark price for extension decisions so we react to current liquidation-relevant price.
        self._extend_grid_if_needed(mark_price)

    def set_drain_mode(self, drain: bool) -> None:
        self.drain_mode = drain

    def force_seed(self, direction: str, price: float) -> None:
        if self.state.direction:
            self.log.info("Cannot seed; grid already active (direction=%s).", self.state.direction)
            return
        self.log.info("Force seeding grid: %s at price %.2f", direction, price)
        self._start_position(price, direction)
