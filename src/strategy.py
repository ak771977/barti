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
        self.state.direction = direction
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
