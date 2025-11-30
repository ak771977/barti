import logging
import time
from typing import Optional, Tuple

from .config import SymbolConfig
from .exchange import BinanceFuturesClient
from .grid import GridState, level_qty
from .indicators import BollingerBands
from .state import StateStore


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

    def reconcile_position(self, price: float, position_qty: float) -> None:
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
        self.state.direction = direction
        self.state.levels_filled = level
        self.state.last_entry_price = price
        spacing = self.cfg.grid_spacing_usd
        self.state.next_entry_price = price + spacing if direction == "short" else price - spacing
        self.log.info(
            "Reconstructed grid from live position qty=%.6f as level %d direction %s", position_qty, level, direction
        )
        self.state_store.save(self.state)

    def _tp_price(self, entry_price: float, qty: float, direction: str) -> float:
        delta = self.cfg.target_profit_usd / qty
        if direction == "short":
            return entry_price - delta
        return entry_price + delta

    def _place_tp(self, entry_price: float, qty: float, direction: str, level: int) -> None:
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
        order = self.client.place_market_order(self.cfg.name, side, qty)
        entry_price = float(order.get("avgPrice") or price)
        self.state.direction = direction
        self.state.last_entry_price = entry_price
        self.state.levels_filled = 1
        spacing = self.cfg.grid_spacing_usd
        self.state.next_entry_price = entry_price + spacing if direction == "short" else entry_price - spacing
        self.state.basket_id += 1
        self.state.basket_start_balance = wallet
        self.state.max_volume = max(self.state.max_volume, qty)
        self.state.worst_drawdown = 0.0
        self.state.basket_open_ts = time.time()
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
        if not self.state.direction or self.state.next_entry_price is None:
            return
        if self.state.levels_filled >= self.cfg.max_levels:
            return

        should_enter = False
        if self.state.direction == "short" and price >= self.state.next_entry_price:
            should_enter = True
        elif self.state.direction == "long" and price <= self.state.next_entry_price:
            should_enter = True

        if not should_enter:
            return

        level = self.state.levels_filled + 1
        qty = level_qty(level, self.cfg.grid.base_qty, self.cfg.grid.repeat_every, self.cfg.grid.multiplier, self.cfg.min_qty_step)
        min_qty = round_up(self.cfg.min_notional_usd / price, self.cfg.min_qty_step)
        if qty < min_qty:
            self.log.info("Adjusted qty to meet min notional: %.6f -> %.6f", qty, min_qty)
            qty = min_qty
        side = "SELL" if self.state.direction == "short" else "BUY"
        order = self.client.place_market_order(self.cfg.name, side, qty)
        entry_price = float(order.get("avgPrice") or price)
        self.state.levels_filled = level
        self.state.last_entry_price = entry_price
        spacing = self.cfg.grid_spacing_usd
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
        pos_info = self.client.get_position_info(self.cfg.name)
        position_qty = pos_info["positionAmt"]
        mark_price = pos_info.get("markPrice", price) or price
        unrealized = pos_info.get("unRealizedProfit", 0.0)
        notional = abs(position_qty) * mark_price
        if notional > 0:
            drawdown = unrealized / notional
            if drawdown < self.state.worst_drawdown:
                self.state.worst_drawdown = drawdown
                self.state_store.save(self.state)
            if abs(position_qty) > self.state.max_volume:
                self.state.max_volume = abs(position_qty)
                self.state_store.save(self.state)
        self._maybe_reset_state(position_qty)
        if bands is None:
            return

        lower, mid, upper = bands
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

        self._extend_grid_if_needed(price)

    def set_drain_mode(self, drain: bool) -> None:
        self.drain_mode = drain

    def force_seed(self, direction: str, price: float) -> None:
        if self.state.direction:
            self.log.info("Cannot seed; grid already active (direction=%s).", self.state.direction)
            return
        self.log.info("Force seeding grid: %s at price %.2f", direction, price)
        self._start_position(price, direction)
