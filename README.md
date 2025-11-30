# Binance ETHUSDT Futures Grid Bot

Minimal Python bot for ETHUSDT perpetuals on Binance Futures with Bollinger-triggered grid entries and per-order take-profit.

## Quick start
1. Create and activate a local venv (no system Python packages):
   ```bash
   ./scripts/setup_venv.sh
   source .venv/bin/activate
   ```
2. Set keys in a `.env` (or export env vars). Default config points to Binance Futures **testnet**; keep both key pairs and the bot will pick the right pair automatically when `--live` is supplied:
   ```
   BINANCE_LIVE_API_KEY=your_live_key
   BINANCE_LIVE_API_SECRET=your_live_secret
   BINANCE_TESTNET_API_KEY=your_testnet_key
   BINANCE_TESTNET_API_SECRET=your_testnet_secret
   ```
3. Adjust `config/config.json` as needed (poll interval, grid spacing, qty scaling, TP, cooldown); keep the live endpoint inside `config/live.json`.
4. Run (ensure venv is active or run through `./scripts/init_and_run.sh`):
   ```bash
   python -m src.runner
   ```
   - Drain existing grid only (no new entries): `python -m src.runner --drain`
   - Run live markets using the same config: `python -m src.runner --live`
   - Helper scripts forward CLI switches, e.g., `./scripts/run_foreground.sh --live --seed buy` or `./scripts/run_tmux.sh --drain`; they all accept `--live`, `--seed`, `--drain` exactly like the direct invocation.
   - Use `./scripts/run_live.sh` to launch the live config with `--live` pre-applied so it automatically keeps the live base URL and credentials separate from the default.

## Strategy
- 1m Bollinger Bands (period 100, std 2.0). Short on price breaching upper band; long on price breaching lower band (no close confirmation).
- Grid spacing: market orders every `grid_spacing_usd` move deeper into the trade until `max_levels`.
- Qty scaling: start at `base_qty`, repeat for `repeat_every` levels, then multiply by `multiplier` and round up to `min_qty_step`.
- TP per fill: limit reduce-only order placed immediately with `target_profit_usd` per ETH (price offset is fixed, so actual TP profit scales linearly with the filled quantity).

## Notes
- Leverage and margin mode (CROSS/ISOLATED) are set on startup (`leverage`, `margin_mode`).
- Cooldown: set `cooldown_minutes` to delay re-arming a new grid after full exit (0 = immediate).
- State persistence mirrors the log/csv naming (`state/testnet-state.json` vs `state/live-state.json`), keeping each environment isolated; use `--state-file` to override and `--drain` to manage an existing grid without opening new entries.
- Balance/maintenance margin is logged every `balance_log_interval_seconds`.
- Each grid/basket is tagged with an incrementing ID; tracked stats include levels filled, max position size (ETH), worst drawdown (absolute USDT magnitude), and an approximate PnL at closure (wallet change over the basket; assumes no other trading).
- Session caps (in-memory): `session_limits.max_grids` stops starting new grids after N baskets; `session_limits.max_gain_usd` stops once cumulative session PnL meets/exceeds the target (enables drain mode automatically).
- Logs rotate daily under `logs/` and older months are zipped into `logs/archive/`.
- Poll loop uses REST every `poll_interval_seconds` (default 10s) to keep VPS footprint low. Websocket/reactive mode can be added later.
- Detailed logs include basket id, level, qty, entry price, next entry, and TP placement per level.
- Basket summaries are also appended to `state/baskets-<ENV>-<SYMBOL>.csv` (non-rotating, ENV is `testnet` or `live`) with open/close time, basket id, direction, levels, max volume, margin used, margin ratio, grid spacing, TP per lot, holding time (minutes), worst drawdown, and PnL.
- Separate log files per environment: `logs/bot-testnet.log*` for testnet, `logs/bot-live.log*` for live.
- The `--live` flag forces the live API credentials while the separate `config/live.json` already points at the correct live endpoint; keep both configs around so you can run testnet and live in parallel using the helper scripts.

## Roadmap ideas
- Swap to CCXT + websocket price feed for faster reaction with lower REST noise.
- Add Telegram/Discord alerts (new levels, TP hit, margin ratio high).
- Add gap catch-up placement, order cleanup, and retry/backoff for rate limits.
- Add lightweight monitoring dashboard for live stats and health.
