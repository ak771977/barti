from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List

from .utils import round_up


@dataclass
class GridState:
    direction: Optional[str] = None  # "long" | "short" | None
    last_entry_price: Optional[float] = None
    next_entry_price: Optional[float] = None
    levels_filled: int = 0
    cooldown_until_ts: Optional[float] = None
    basket_id: int = 0
    basket_start_balance: Optional[float] = None
    max_volume: float = 0.0
    worst_drawdown: float = 0.0  # negative values represent the worst unrealized loss in USDT
    basket_open_ts: Optional[float] = None
    entry_order_ids: List[int] = field(default_factory=list)
    tp_order_ids: List[int] = field(default_factory=list)

    def reset(self) -> None:
        self.direction = None
        self.last_entry_price = None
        self.next_entry_price = None
        self.levels_filled = 0
        self.cooldown_until_ts = None
        self.basket_id = 0
        self.basket_start_balance = None
        self.max_volume = 0.0
        self.worst_drawdown = 0.0
        self.basket_open_ts = None
        self.entry_order_ids = []
        self.tp_order_ids = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "last_entry_price": self.last_entry_price,
            "next_entry_price": self.next_entry_price,
            "levels_filled": self.levels_filled,
            "cooldown_until_ts": self.cooldown_until_ts,
            "basket_id": self.basket_id,
            "basket_start_balance": self.basket_start_balance,
            "max_volume": self.max_volume,
            "worst_drawdown": self.worst_drawdown,
            "basket_open_ts": self.basket_open_ts,
            "entry_order_ids": self.entry_order_ids,
            "tp_order_ids": self.tp_order_ids,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "GridState":
        return GridState(
            direction=data.get("direction"),
            last_entry_price=data.get("last_entry_price"),
            next_entry_price=data.get("next_entry_price"),
            levels_filled=int(data.get("levels_filled", 0)),
            cooldown_until_ts=data.get("cooldown_until_ts"),
            basket_id=int(data.get("basket_id", 0)),
            basket_start_balance=data.get("basket_start_balance"),
            max_volume=float(data.get("max_volume", 0.0)),
            worst_drawdown=float(data.get("worst_drawdown", 0.0)),
            basket_open_ts=data.get("basket_open_ts"),
            entry_order_ids=[int(o) for o in data.get("entry_order_ids", [])],
            tp_order_ids=[int(o) for o in data.get("tp_order_ids", [])],
        )


def level_qty(level: int, base_qty: float, repeat_every: int, multiplier: float, step: float) -> float:
    """
    Returns the quantity for the given level (1-indexed) using:
    - repeat the same qty every `repeat_every` levels
    - scale by multiplier and round up to the step when the repeat completes
    """
    qty = base_qty
    current_level = 1
    while current_level < level:
        if current_level % repeat_every == 0:
            qty = round_up(qty * multiplier, step)
        current_level += 1
    return qty
