import argparse
import os
import time

from dotenv import load_dotenv

from .config import load_config
from .exchange import BinanceAPIError, BinanceFuturesClient
from .logger import setup_logging
from .state import StateStore, BasketRecorder
from .strategy import GridBollingerStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance ETHUSDT grid bot")
    parser.add_argument("--config", default="config/config.json", help="Path to config file")
    parser.add_argument("--drain", action="store_true", help="Drain mode: manage existing grid, no new entries")
    parser.add_argument("--seed", choices=["buy", "sell"], help="Force start a grid immediately at current price (buy=long, sell=short)")
    args = parser.parse_args()

    load_dotenv()
    cfg = load_config(args.config)
    env_label = "testnet" if cfg.exchange.testnet else "live"
    logger = setup_logging(cfg.log_dir, name=f"bot-{env_label}")

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit("BINANCE_API_KEY and BINANCE_API_SECRET must be set in the environment.")

    client = BinanceFuturesClient(api_key, api_secret, cfg.exchange.base_url, cfg.exchange.recv_window)
    state_store = StateStore(cfg.state_file)
    basket_recorder = BasketRecorder(f"state/baskets-{cfg.symbol.name}.csv")

    logger.info("Starting bot in %s mode for %s", env_label.upper(), cfg.symbol.name)

    try:
        client.set_leverage(cfg.symbol.name, cfg.symbol.leverage)
        logger.info("Leverage set to %sx for %s", cfg.symbol.leverage, cfg.symbol.name)
    except BinanceAPIError as exc:
        logger.error("Failed to set leverage: %s", exc)
    try:
        client.set_margin_mode(cfg.symbol.name, cfg.symbol.margin_mode)
        logger.info("Margin mode set to %s for %s", cfg.symbol.margin_mode, cfg.symbol.name)
    except BinanceAPIError as exc:
        # Binance returns an error if margin mode is already set; log and continue
        logger.warning("Setting margin mode returned: %s", exc)

    drain_mode = cfg.drain_mode or args.drain
    session_grids = 0
    session_gain = 0.0

    def on_basket_close(summary: dict) -> None:
        nonlocal session_grids, session_gain, drain_mode
        session_grids += 1
        pnl = summary.get("pnl")
        if pnl is not None:
            session_gain += pnl
        max_grids = cfg.session_limits.max_grids if cfg.session_limits else 0
        max_gain = cfg.session_limits.max_gain_usd if cfg.session_limits else 0.0
        hit_limit = False
        if max_grids > 0 and session_grids >= max_grids:
            hit_limit = True
            logger.info("Session max_grids=%d reached; enabling drain mode.", max_grids)
        if not hit_limit and max_gain > 0 and session_gain >= max_gain:
            hit_limit = True
            logger.info("Session max_gain_usd=%.2f reached (%.2f); enabling drain mode.", max_gain, session_gain)
        if hit_limit:
            drain_mode = True
            strat.set_drain_mode(True)

    if drain_mode:
        logger.info("Drain mode enabled: will not start new grids.")

    strat = GridBollingerStrategy(
        client,
        cfg.symbol,
        logger,
        state_store,
        drain_mode=drain_mode,
        on_basket_close=on_basket_close,
        basket_recorder=basket_recorder,
    )
    strat._log_throttle = cfg.log_throttle_seconds

    try:
        klines = client.get_klines(cfg.symbol.name, cfg.symbol.timeframe, limit=max(cfg.symbol.bollinger.period, 120))
        closes = [float(k[4]) for k in klines]
        strat.seed_indicator(closes)
        logger.info("Seeded Bollinger Bands with %d closes", len(closes))
    except BinanceAPIError as exc:
        logger.error("Could not fetch klines to seed indicator: %s", exc)

    price = None
    try:
        price = client.get_price(cfg.symbol.name)
        pos = client.get_position_info(cfg.symbol.name)
        open_orders = client.get_open_orders(cfg.symbol.name)
        strat.reconcile_position(price, pos, open_orders=open_orders)
    except BinanceAPIError as exc:
        logger.error("Failed to reconcile position on startup: %s", exc)

    if args.seed and price:
        direction = "long" if args.seed == "buy" else "short"
        try:
            strat.force_seed(direction, price)
        except BinanceAPIError as exc:
            logger.error("Failed to seed grid (%s): %s", direction, exc)

    logger.info("Starting loop for %s; poll every %ss", cfg.symbol.name, cfg.poll_interval_seconds)
    last_balance_log = 0.0
    while True:
        try:
            price = client.get_price(cfg.symbol.name)
            strat.on_price(price)
            now = time.time()
            if now - last_balance_log >= cfg.balance_log_interval_seconds:
                try:
                    account = client.get_account()
                    total_margin_balance = float(account.get("totalMarginBalance", 0))
                    available = float(account.get("availableBalance", 0))
                    maint = float(account.get("totalMaintMargin", 0))
                    margin_ratio = maint / total_margin_balance if total_margin_balance > 0 else 0.0
                    logger.info(
                        "Balance: total=%.2f available=%.2f maint=%.2f margin_ratio=%.3f",
                        total_margin_balance,
                        available,
                        maint,
                        margin_ratio,
                    )
                except BinanceAPIError as exc:
                    logger.error("Failed to fetch account info: %s", exc)
                last_balance_log = now
        except BinanceAPIError as exc:
            logger.error("Binance API error: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in loop: %s", exc)
        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
