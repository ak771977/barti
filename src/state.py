import json
from pathlib import Path
from typing import Optional
import csv
from datetime import datetime

from .grid import GridState


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> GridState:
        if not self.path.exists():
            return GridState()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return GridState.from_dict(data)
        except Exception:
            return GridState()

    def save(self, state: GridState) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f)
        tmp_path.replace(self.path)


class BasketRecorder:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "open_at",
                        "closed_at",
                        "basket_id",
                        "symbol",
                        "direction",
                        "levels",
                        "max_volume_eth",
                        "worst_drawdown",
                        "pnl",
                    ]
                )

    def append(self, symbol: str, summary: dict) -> None:
        max_volume = summary.get("max_volume_eth", 0.0)
        max_volume_str = f"{max_volume:.6f}".rstrip("0").rstrip(".")
        if not max_volume_str:
            max_volume_str = "0"
        worst_drawdown = abs(summary.get("worst_drawdown", 0.0))
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    summary.get("open_at"),
                    datetime.utcnow().isoformat(),
                    summary.get("basket_id"),
                    symbol,
                    summary.get("direction"),
                    summary.get("levels"),
                    max_volume_str,
                    f"{worst_drawdown:.6f}",
                    "" if summary.get("pnl") is None else f"{summary.get('pnl'):.2f}",
                ]
            )
