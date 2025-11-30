import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass
class BollingerConfig:
    period: int = 100
    stddev: float = 2.0


@dataclass
class GridShapeConfig:
    base_qty: float
    repeat_every: int
    multiplier: float


@dataclass
class SymbolConfig:
    name: str
    timeframe: str
    leverage: int
    margin_mode: str
    max_levels: int
    grid_spacing_usd: float
    target_profit_usd: float
    min_qty_step: float
    min_notional_usd: float
    grid: GridShapeConfig
    bollinger: BollingerConfig
    cooldown_minutes: int = 0


@dataclass
class ExchangeConfig:
    base_url: str
    recv_window: int = 5000
    testnet: bool = False


@dataclass
class BotConfig:
    poll_interval_seconds: int
    log_dir: str
    exchange: ExchangeConfig
    symbol: SymbolConfig
    state_file: str
    drain_mode: bool
    balance_log_interval_seconds: int = 60
    session_limits: "SessionLimits | None" = None


@dataclass
class SessionLimits:
    max_grids: int = 0
    max_gain_usd: float = 0.0


def _parse_grid(cfg: Dict[str, Any]) -> GridShapeConfig:
    return GridShapeConfig(
        base_qty=cfg["base_qty"],
        repeat_every=cfg.get("repeat_every", 2),
        multiplier=cfg.get("multiplier", 1.5),
    )


def _parse_bollinger(cfg: Dict[str, Any]) -> BollingerConfig:
    return BollingerConfig(
        period=cfg.get("period", 100),
        stddev=cfg.get("stddev", 2.0),
    )


def _parse_symbol(cfg: Dict[str, Any]) -> SymbolConfig:
    return SymbolConfig(
        name=cfg["name"],
        timeframe=cfg.get("timeframe", "1m"),
        leverage=cfg.get("leverage", 25),
        margin_mode=cfg.get("margin_mode", "CROSS").upper(),
        max_levels=cfg.get("max_levels", 10),
        grid_spacing_usd=cfg["grid_spacing_usd"],
        target_profit_usd=cfg["target_profit_usd"],
        min_qty_step=cfg.get("min_qty_step", 0.001),
        min_notional_usd=cfg.get("min_notional_usd", 20.0),
        grid=_parse_grid(cfg["grid"]),
        bollinger=_parse_bollinger(cfg["bollinger"]),
        cooldown_minutes=cfg.get("cooldown_minutes", 0),
    )


def _parse_exchange(cfg: Dict[str, Any]) -> ExchangeConfig:
    return ExchangeConfig(
        base_url=cfg["base_url"],
        recv_window=cfg.get("recv_window", 5000),
        testnet=cfg.get("testnet", False),
    )


def load_config(path: str | Path) -> BotConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    session_data = data.get("session_limits", {})
    session_limits = SessionLimits(
        max_grids=int(session_data.get("max_grids", 0)),
        max_gain_usd=float(session_data.get("max_gain_usd", 0.0)),
    )
    return BotConfig(
        poll_interval_seconds=data.get("poll_interval_seconds", 10),
        log_dir=data.get("log_dir", "logs"),
        exchange=_parse_exchange(data["exchange"]),
        symbol=_parse_symbol(data["symbol"]),
        state_file=data.get("state_file", "state/state.json"),
        drain_mode=data.get("drain_mode", False),
        balance_log_interval_seconds=data.get("balance_log_interval_seconds", 60),
        session_limits=session_limits,
    )
