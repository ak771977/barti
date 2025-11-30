# Binance ETHUSDT Futures Grid Bot

Minimal Python bot for ETHUSDT perpetuals on Binance Futures with Bollinger-triggered grid entries and per-order take-profit.

## Quick start
1. Create and activate a local venv (no system Python packages):
   ```bash
   ./scripts/setup_venv.sh
   source .venv/bin/activate
   ```
2. Set keys in a `.env` (or export env vars). Default config points to Binance Futures **testnet**; change `exchange.testnet` and `exchange.base_url` for live:
   ```
   BINANCE_API_KEY=your_key
   BINANCE_API_SECRET=your_secret
   ```
3. Adjust `config/config.json` as needed (poll interval, grid spacing, qty scaling, TP, cooldown).
4. Run (ensure venv is active):
   ```bash
   python -m src.runner
   ```
   - Drain existing grid only (no new entries): `python -m src.runner --drain`

## Strategy
- 1m Bollinger Bands (period 100, std 2.0). Short on price breaching upper band; long on price breaching lower band (no close confirmation).
- Grid spacing: market orders every `grid_spacing_usd` move deeper into the trade until `max_levels`.
- Qty scaling: start at `base_qty`, repeat for `repeat_every` levels, then multiply by `multiplier` and round up to `min_qty_step`.
- TP per fill: limit reduce-only order placed immediately with `target_profit_usd` per lot.

## Notes
- Leverage and margin mode (CROSS/ISOLATED) are set on startup (`leverage`, `margin_mode`).
- Cooldown: set `cooldown_minutes` to delay re-arming a new grid after full exit (0 = immediate).
- State is persisted to `state/state.json` and reloaded on startup; `--drain` manages existing grid but won’t start new ones.
- Balance/maintenance margin is logged every `balance_log_interval_seconds`.
- Each grid/basket is tagged with an incrementing ID; tracked stats include levels filled, max position size (ETH), worst drawdown (as pct of notional), and an approximate PnL at closure (wallet change over the basket; assumes no other trading).
- Session caps (in-memory): `session_limits.max_grids` stops starting new grids after N baskets; `session_limits.max_gain_usd` stops once cumulative session PnL meets/exceeds the target (enables drain mode automatically).
- Logs rotate daily under `logs/` and older months are zipped into `logs/archive/`.
- Poll loop uses REST every `poll_interval_seconds` (default 10s) to keep VPS footprint low. Websocket/reactive mode can be added later.
- Detailed logs include basket id, level, qty, entry price, next entry, and TP placement per level.
- Basket summaries are also appended to `state/baskets-<SYMBOL>.csv` (non-rotating) with open/close time, basket id, direction, levels, max volume, worst drawdown, and PnL.
- Separate log files per environment: `logs/bot-testnet.log*` for testnet, `logs/bot-live.log*` for live.

## Roadmap ideas
- Swap to CCXT + websocket price feed for faster reaction with lower REST noise.
- Add Telegram/Discord alerts (new levels, TP hit, margin ratio high).
- Add gap catch-up placement, order cleanup, and retry/backoff for rate limits.
- Add lightweight monitoring dashboard for live stats and health.
