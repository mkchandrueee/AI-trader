# AI Trader — CLAUDE.md

Intraday NIFTY options paper-trading system. Collects live ticks, trains XGBoost ML models, scores trade signals, and presents everything through a Next.js dashboard backed by a Flask API.

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | Next.js 16 + React 19 + Tailwind v4 + Recharts |
| Backend API | Flask (Python 3.13), SSE stream at `/api/stream` |
| Database | TimescaleDB (PostgreSQL 17) — hypertables for tick/candle data |
| ML | XGBoost + LightGBM (scikit-learn pipeline), joblib `.pkl` models |
| Data Feed | AngelOne SmartAPI (live WebSocket + historical REST, free) + jugaad-data (free EOD bhavcopy) |
| Broker | AngelOne SmartAPI (free API access) — paper trading by default. mStock Trading API (Type A) is the intended broker for live order execution going forward (`TRADE_MODE=mstock`) |
| Runtime | macOS, Python venv at `.venv/`, Node in `dashboard/node_modules/` |

---

## Directory Structure

```
ai-trader/
├── backend/app.py          # Flask API server (port 5050) — the main backend
├── dashboard/               # Next.js frontend (port 3000)
│   └── app/
│       ├── live/            # Live trading page (SSE stream, positions, suggestions)
│       ├── charts/          # Option chain + candle charts
│       ├── backtest/        # Backtest runner + results
│       ├── trades/          # Trade history
│       ├── scanner/         # Market Scanner — candle-quality scan across the whole NSE board
│       ├── predictions/     # AI Forecast — NSE-Neuron 5-day LSTM/BiLSTM/GRU/CNN-LSTM forecast
│       ├── news/            # News Brief — free RSS + sentiment
│       ├── ai/              # AI chat
│       └── settings/        # Risk profile + config
├── scripts/
│   ├── collect_ticks.py          # LIVE tick collector (runs during market hours)
│   ├── incremental_train.py      # Daily macro/micro model retraining after market close
│   ├── train_outcome_models.py   # Train strategy models on actual trade outcomes (WIN/LOSS)
│   ├── train_rl_on_journeys.py   # Retrain RL exit agent on all saved journey data
│   ├── tick_replay_backtest.py   # Tick-level replay backtest engine
│   ├── fetch_missing_ticks.py    # Backfill single symbol via REST
│   └── backfill_today.py         # Backfill all today's symbols via REST
├── models/
│   ├── train_model.py       # MacroModelTrainer + MicroModelTrainer classes
│   ├── strategy_models.py   # Strategy-specific XGBoost models
│   ├── rl_exit_agent.py     # RLExitAgent (Q-learning, tabular, 8-feature state)
│   ├── predict.py           # Predictor wrapper (load + infer)
│   └── saved/               # .pkl files (macro_model.pkl, micro_model.pkl, rl_exit_agent.pkl)
│       ├── strategy/        # Per-strategy outcome models (bearish_momentum_model.pkl, etc.)
│       └── backups/YYYYMMDD/ # Date-organized model backups before each retrain
├── data/
│   ├── market_data_adapter.py  # AngelOne (live+historical) + jugaad-data (EOD) client
│   ├── angelone_symbols.py     # Instrument master → symboltoken resolution
│   ├── jugaad_adapter.py       # Free EOD bhavcopy / stock / index history
│   ├── news_sentiment.py       # RSS news fetch + sentiment scoring (free)
│   └── tick_collector.py       # TickCollector (buffers 200 ticks → DB flush)
├── predictions/             # Vendored NSE-Neuron (LSTM/BiLSTM/GRU/CNN-LSTM), jugaad-data-ified
│   ├── forecast.py          # forecast_symbol() / forecast_all() / regime_analysis()
│   ├── nn_config.py         # NSE-Neuron's own config (renamed to avoid colliding with config/)
│   ├── models/               # The four regression models + their BUY/HOLD/SELL classifiers
│   └── utils/                 # data_fetcher (jugaad-data), preprocessor, regime/pattern detection
├── features/
│   ├── indicators.py        # compute_all_macro_indicators() — 58 features
│   └── micro_features.py    # compute_micro_features() — 5 features
├── strategy/
│   ├── signal_generator.py       # Generates BUY/SELL signals per strategy
│   ├── math_decision_strategy.py # Deterministic ATM CE/PE candle-quality strategy (the "math strategy")
│   ├── market_scanner.py         # Same candle-quality maths, applied across the whole NSE EOD board
│   ├── trade_scorer.py      # Composite score = 0.5×ML + 0.3×flow + 0.2×tech
│   ├── regime_detector.py   # TRENDING_BULL/BEAR/SIDEWAYS/HIGH_VOL/LOW_VOL
│   └── options_flow_detector.py
├── config/
│   ├── settings.py          # All constants (DB URL, symbols, thresholds)
│   └── risk_profiles.py     # LOW/MEDIUM/HIGH RiskProfile dataclasses
├── database/
│   ├── db.py                # read_sql / write_df / upsert_candles / init_db
│   └── schema.sql           # Full TimescaleDB schema
└── backtest/
    ├── backtest_engine.py
    └── option_resolver.py   # get_nearest_expiry(date) → next Tuesday expiry
```

---

## How to Run

### Prerequisites
```bash
brew services start postgresql@17   # TimescaleDB must be running
cd /Users/aaryansinha/Dev/Projects/ai-trader
source .venv/bin/activate
```

### Backend (Flask API)
```bash
python backend/app.py              # http://localhost:5050
```
Auto-starts `collect_ticks.py` during market hours via `_ensure_collector()`.

### Frontend (Next.js dashboard)
```bash
cd dashboard && npm run dev         # http://localhost:3000
```

### Live Tick Collector (market hours only: 9:15–15:30 IST)
```bash
# Flask auto-starts this. To run manually:
nohup .venv/bin/python scripts/collect_ticks.py >> logs/tick_collector_YYYYMMDD.log 2>&1 &
```
Writes to `/tmp/td_live_prices.json` every 1s (Flask reads this for streaming prices).

### Retrain Models (after market close)
```bash
# Full retrain on ALL historical data (preferred):
python scripts/incremental_train.py

# Incremental only (1–2 new days, uses warm-start):
python scripts/incremental_train.py --days 1

# Full retrain from scratch:
python main.py train
```

### Fill Missing Data
```bash
# Fill today's candles + ticks for NIFTY-I + all ATM options via REST:
python scripts/backfill_today.py

# Fill specific date for a single symbol:
python scripts/fetch_missing_ticks.py --dates 2026-03-25 --symbol NIFTY-I
```

### DB Init / Schema
```bash
python -c "from database.db import init_db; init_db()"
```

---

## Database Schema (TimescaleDB Hypertables)

| Table | Key Columns | Notes |
|---|---|---|
| `tick_data` | `timestamp, symbol, price, volume, oi, bid_price, ask_price` | Hypertable; ~8k ticks/day/symbol |
| `minute_candles` | `timestamp, symbol, open, high, low, close, volume, vwap, oi` | Primary ML training source; upsert by (timestamp, symbol) |
| `option_chain` | `timestamp, symbol, underlying, expiry, strike, option_type, ltp, oi, iv, delta` | Snapshots |
| `symbol_master` | `symbol, underlying, expiry, strike, option_type, lot_size` | F&O universe (AngelOne instrument master) |
| `trade_log` | `entry_time, exit_time, symbol, side, entry_price, pnl, ml_score, final_score` | Paper trade history |
| `features_macro` | 17 feature columns | Computed from minute_candles |
| `features_micro` | 5 feature columns | bid_ask_spread, order_imbalance, etc. |
| `daily_performance` | `date, total_trades, wins, net_pnl, win_rate` | EOD summary |

All timestamps are `TIMESTAMPTZ` (stored UTC, displayed as IST +5:30).

---

## AngelOne + jugaad-data Integration

Replaces the paid TrueData feed and Zerodha Kite Connect broker. Free for
retail AngelOne account holders — no separate API subscription.

### Symbol Naming
```
NIFTY-I            → NIFTY continuous futures — a stable internal alias the
                      whole DB/dashboard/ML pipeline is keyed on; resolved to
                      AngelOne's real current-month contract (e.g.
                      NIFTY26APRFUT) inside data/market_data_adapter.py
NIFTY 50           → NIFTY spot index (websocket subscription only)
NIFTY{YYMMDD}{STRIKE}{CE|PE}  → Options e.g. NIFTY26033022950PE
```
NIFTY weekly expiry is on **Tuesdays** (confirmed from symbol_master table).

### WebSocket (live streaming)
- URL: `wss://smartapisocket.angelone.in/smart-stream`
- Auth: JWT + feed token from `generateSession()` (TOTP-based login, no OAuth redirect)
- Subscribe: `SmartWebSocketV2.subscribe(correlation_id, mode, [{"exchangeType": 2, "tokens": [...]}])` — mode 3 = SNAP_QUOTE (LTP + depth + OI)
- Ticks arrive via the SDK's `on_data` callback as a dict (token, last_traded_price in paise, best_5_buy_data/best_5_sell_data, open_interest, ...)
- `_subscribed_tokens` in `MarketDataAdapter` maps `token → symbol name` (the internal alias, e.g. "NIFTY-I") built during `ws_subscribe()`

### Known gap — no free historical tick API
`fetch_historical_ticks` / `fetch_last_n_ticks` always return empty — AngelOne's
free tier has no historical tick-level endpoint. Minute/day candles are fully
available via `getCandleData`; only tick-level history is missing. Callers
already fall back to candles when ticks are sparse (`tick_replay_backtest.py`).

### Rate Limits
- Historical REST: ~3 requests/second (self-imposed 0.35s spacing in `fetch_historical_minute_bars`)
- WebSocket: check current AngelOne plan limits for max tokens per connection

### REST Endpoints
```
POST https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword  → session (via SmartConnect.generateSession)
POST .../rest/secure/angelbroking/historical/v1/getCandleData                       → OHLCV candles
GET  https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json → free instrument master (symboltoken lookup)
```
jugaad-data (`pip install jugaad-data`) covers free EOD bhavcopy / index /
stock history — no auth, no endpoint to document (see `data/jugaad_adapter.py`).

---

## mStock Trading API (broker for live execution)

`broker/mstock_adapter.py` — Type A (Kite-Connect-shaped) API, official SDK
`pip install mStock-TradingApi-A` (`tradingapi_a.mconnect.MConnect` for
REST, `tradingapi_a.mticker.MTicker` for WebSocket). Auth mirrors AngelOne:
non-interactive via `.env` (API key + user ID + password + TOTP secret,
6-digit code computed live via `pyotp`), or an interactive dashboard
Connect (Settings page) taking a manually-entered TOTP code. Never run
`scripts/mstock_check_auth.py` from chat — the user runs it locally.

```
Login:           POST https://api.mstock.trade/openapi/typea/connect/login
Session (TOTP):  POST https://api.mstock.trade/openapi/typea/session/verifytotp  → access_token
WebSocket:       wss://ws.mstock.trade?API_KEY=...&ACCESS_TOKEN=...
```

`place_order` takes `tradingsymbol` + `exchange` directly (no instrument
token, unlike AngelOne). `get_historical_chart`/market-data calls DO need a
token, resolved via `data/mstock_symbols.py` from mStock's own
`get_instruments()` (requires an authenticated session, unlike AngelOne's
anonymous public dump).

**Order execution**: set `TRADE_MODE=mstock` to route orders through it,
independent of `TRADE_MODE`, market data is now dual-source, mStock
primary / AngelOne fallback:
- **Live WebSocket ticks**: `scripts/collect_ticks.py` connects both
  `data/mstock_market_data.py`'s `MStockMarketData` (primary) and
  `data/market_data_adapter.py`'s `MarketDataAdapter` (redundant) at
  market open, feeding the same `on_tick()` handler — live-verified, real
  ticks flowing from both.
- **Historical option-candle fetches**: `data/multi_source_market_data.py`'s
  `MultiSourceMarketData` is a drop-in replacement for `MarketDataAdapter`
  (tries mStock first, falls back to AngelOne on any failure, tags
  `df.attrs["source"]`) — used by the bulk backfill scripts
  (`backfill_today.py`, `backfill_option_days.py`, `backfill_history.py`,
  `fetch_missing_ticks.py`'s candle path). `strategy/premarket.py`'s
  `_fetch_option_candle()` (the live agent's real-time decision path) has
  its own equivalent dual-fetch, since it needs AngelOne's live-resolved
  tradingsymbol for the AngelOne leg specifically (the DB-alias format
  silently fails for today's freshest contracts there — see that
  function's docstring) alongside mStock's DB-alias-resolved leg.
- **Historical tick-level data**: neither broker has this endpoint
  (AngelOne's free-tier gap; mStock doesn't even stub the method) —
  `fetch_historical_ticks()` always returns empty on both sides.
- **Symbol resolution for market data is broker-specific and NOT
  interchangeable**: mStock's real tradingsymbol convention
  (`NIFTY2692229450CE`, single-digit month code for weeklies) differs
  from AngelOne's (`NIFTY08SEP2622100PE`) for the same contract — don't
  pass one broker's resolved symbol string into the other's
  `fetch_historical_bars()`.

---

## ML Pipeline

### Models
| Model | File | Algorithm | Training data |
|---|---|---|---|
| Macro | `models/saved/macro_model.pkl` | XGBoost | All minute_candles for NIFTY-I (6+ months) |
| Micro | `models/saved/micro_model.pkl` | XGBoost | All tick_data (all available days) |
| Strategy models | `models/saved/strategy/*.pkl` | XGBoost | Per-strategy actual trade outcomes (WIN/LOSS) |
| RL Exit Agent | `models/saved/rl_exit_agent.pkl` | Q-learning | Premium trajectories from all backtest journeys |

### Training
- **MacroModelTrainer**: `train(df, walk_forward=True, n_splits=5)` — 5-fold walk-forward validation on 58 features
- **MicroModelTrainer**: `train(df, walk_forward=True, n_splits=3)` — 3-fold on 5 micro features
- Always backup existing model before retraining: `models/saved/backups/YYYYMMDD/model_HHMMSS.pkl`

> **WARNING — Do NOT retrain macro/micro models until there are enough outcome-labeled trades.**
> Retraining disrupts the XGBoost weights → different entry bar selection → RL agent never sees trained states → RL_EXIT stops firing → fragile P&L baseline breaks.
> The macro model's label quality is especially sensitive: `generate_macro_labels(forward_periods=15, threshold=0.001)` gives ~14% positive rate and varied 0.2–0.7 outputs. Using higher thresholds (e.g., 0.004/25 bars) compresses all outputs near 0, kills discrimination.

### Outcome-Based Strategy Model Training (NEW)
```bash
# Train per-strategy models on ACTUAL trade outcomes from backtest CSVs:
python scripts/train_outcome_models.py

# Options:
python scripts/train_outcome_models.py --min-samples 20  # lower threshold
python scripts/train_outcome_models.py --evaluate        # stats only, no train
```
- Reads `backtest_results/trades_{high,medium,low}_risk.csv`
- For each trade: fetches NIFTY-I candle at `entry_time` from DB → 50-feature vector
- Labels: 1 = WIN (TRAILING_SL/TIMEOUT/RL_EXIT/EOD_CLOSE/TARGET or pnl>0), 0 = LOSS
- Saves `models/saved/strategy/{strategy_name}_model.pkl` with backup

### RL Exit Agent — Journey-Based Retraining (NEW)
```bash
# Retrain RL Q-learning agent on ALL saved trade journeys:
python scripts/train_rl_on_journeys.py            # default 30 epochs
python scripts/train_rl_on_journeys.py --epochs 50
python scripts/train_rl_on_journeys.py --evaluate  # policy stats only
python scripts/train_rl_on_journeys.py --fresh     # reset Q-table from scratch
```
- Reads `backtest_results/journeys_{high,medium,low}_risk.json`
- State features are all trade-relative (pnl_pct, bars_held_norm, momentum, etc.) — no entry timing dependency
- Each epoch shuffles journey order for generalization; best Q-table (by avg reward) is restored at end
- Run after every significant batch of new backtest trades

### Features (58 macro, 5 micro)
**Macro** (from `FEATURE_COLUMNS_MACRO` in settings.py): RSI, MACD, EMA(9/20/50), SMA200, VWAP distance, Bollinger, ATR, StochRSI, Williams%R, ROC, ADX, CCI, OBV slope, MFI, PCR, IV, OI change, days_to_expiry, multi-timeframe RSI/EMA (5m/15m), session time features.

**Micro** (from `FEATURE_COLUMNS_MICRO`): `bid_ask_spread, order_imbalance, trade_size_spike, volume_burst, tick_momentum`.

### Trade Scoring
```
final_score = 0.5 × ML_prob + 0.3 × flow_score + 0.2 × technical_strength ± regime_bonus
```
- `ML_prob` = directional probability from macro model (CALL: direct, PUT: 1 − prob)
- `flow_score` = PCR-based: 0.5 default; `min(0.3*(pcr>1.2)+0.2, 1.0)` when PCR available
- `technical_strength` = from signal generator per-strategy

### Training Workflow (Post-Market)
```bash
# 1. Retrain outcome-based strategy models (safe — small XGBoost, separate from macro):
python scripts/train_outcome_models.py

# 2. Retrain RL agent with new journey data:
python scripts/train_rl_on_journeys.py --epochs 50

# 3. Full macro/micro retrain (DANGEROUS — only when baseline is confirmed stable):
python scripts/incremental_train.py
```

---

## Risk Profiles

| Profile | Score Threshold (CALL/PUT) | SL | Target | Max Trades/Day | Afternoon Cut |
|---|---|---|---|---|---|
| LOW (Conservative) | 0.70 / 0.80 | 20% | 35% | 3 | 11:45 IST |
| MEDIUM (Balanced) | 0.60 / 0.70 | 20% | 50% | 5 | 12:30 IST |
| HIGH (Aggressive) | 0.58 / 0.65 | 20% | 80% | 8 | 13:15 IST |

NIFTY lot size = 65 units. Suggestion SL/target in `backend/app.py`:
```python
INITIAL_SL_PCT = 0.15   # SL = entry × 0.85
TGT_PCT = 0.50          # Target = entry × 1.50
SUGGESTION_COOLDOWN_SECS = 300  # 5 min between re-suggesting same signal
```

---

## Flask API Routes (backend/app.py — port 5050)

```
GET  /api/state                → full scanner state (regime, suggestions, positions)
GET  /api/stream               → SSE stream (NIFTY price, state updates every ~1s)
POST /api/scan                 → manual trigger scan
POST /api/paper/enter          → enter paper trade
POST /api/paper/exit           → exit paper trade
GET  /api/paper/positions      → open positions
POST /api/paper/clear          → clear all positions
GET  /api/live/prices          → live prices from tick cache
GET  /api/trades/history       → past trades
GET  /api/equity/curve         → equity curve for chart
GET  /api/risk/profiles        → list risk profiles
GET  /api/days                 → dates with candle data
GET  /api/market/candles       → last N candles for symbol
GET  /api/market/candles/date  → candles for specific date+symbol
GET  /api/market/ticks/date    → ticks for specific date+symbol
GET  /api/candle_dates         → dates available in minute_candles
GET  /api/options/expiries     → available option expiry dates
GET  /api/options/chain        → option chain for date+expiry (from minute_candles)
GET  /api/options/ticks        → tick chart for a specific option symbol+date
POST /api/backtest/run         → run backtest
GET  /api/backtest/results     → saved backtest results
GET  /api/backtest/progress    → backtest progress SSE
GET  /api/scanner/scan         → candle-quality scan across the NSE EOD board (free bhavcopy)
GET  /api/predictions/forecast → NSE-Neuron 5-day forecast (lstm|bilstm|gru|cnn_lstm|all)
GET  /api/predictions/regime   → regime + candlestick patterns, no model training
GET  /api/news/brief           → free RSS news + keyword sentiment
```

### Key State Variables (in-memory, resets on Flask restart)
```python
state = {
    "trade_suggestions": [...],  # expires after SUGGESTION_COOLDOWN_SECS
    "positions": [...],          # open paper trades
    "regime": "...",
    "status": "idle|scanning|market_closed"
}
```

### Background Threads
- `background_scanner` — runs every 30s, calls `scan_market()` + `_ensure_collector()`
- `_tick_monitor_loop` — runs every 1s, updates open position prices from tick cache
- `collect_ticks.py` — separate subprocess, auto-started by `_ensure_collector()`

---

## Live Price Cache

**File**: `/tmp/td_live_prices.json`

**Format**: `{"NIFTY-I": {"price": 22950.0, "ts": "2026-03-27T13:42:47.362923"}, ...}`

- Written every 1s by `collect_ticks.py`'s `_flush_price_cache` thread
- `ts` = **wall-clock time when tick was received** (NOT the tick's market timestamp)
- Flask reads this in `_tick_monitor_loop` and the SSE stream
- `_cache_prices_are_fresh(max_age_secs=90)` returns False if file >30s old or all ts >90s old → triggers collector restart

---

## Key Conventions

### Python
- All DB calls via `read_sql(sql, params)` / `write_df(df, table)` / `upsert_candles(df)` — never raw psycopg2
- `from dotenv import load_dotenv; load_dotenv()` at top of every script (not inside functions)
- Logger via `from utils.logger import get_logger; logger = get_logger("name")`
- Timestamps: DB stores UTC `TIMESTAMPTZ`, Python uses naive local IST for comparisons
- Option symbols: `NIFTY{YYMMDD}{STRIKE}{CE|PE}` e.g. `NIFTY26033022950PE`
- Expiry from `backtest.option_resolver.get_nearest_expiry(date)` — queries symbol_master table

### Frontend (Next.js / TypeScript)
- API calls via `dashboard/lib/api.ts` helper
- SSE stream consumed in `dashboard/app/live/page.tsx`
- All times displayed in IST
- Trade table columns include: Time, Symbol, Type, Strategy, Risk (LOW/MED/HIGH badge), SL ₹, Target ₹, Expiry, Score, ML%, Action

---

## What NOT To Do

- **Don't subscribe to `NIFTY 50` (spot) in `collect_ticks.py`** — this was a 2026-04-08 bug that corrupted every macro feature. Spot and futures prices differ by ~50pt, and when both were stored under `symbol='NIFTY-I'` in `tick_data`, the resulting minute candles had fake ~40pt high-low ranges that poisoned RSI/ATR/Bollinger/MACD and silently destroyed the macro_model retrains. Only subscribe to `NIFTY-I` futures. The `on_tick()` handler has a defensive drop for any leaking spot symbol.
- **Don't use `expiries[-1]` as a fallback in `get_nearest_expiry()`** — this was the 2026-04-08 bug that had the live collector subscribing to *yesterday's* expired contracts every morning. The only correct source for future expiries is AngelOne's instrument master (cached 1h — see `_fetch_live_expiries_from_angelone()`). DB-derived expiries are only correct for historical backtests on past dates. `get_nearest_expiry()` now splits these two paths.
- **Don't apply entry gates uniformly across strategies** — the 2026-04-08 first-pass fix broke MEDIUM from ₹+51k → ₹+37k because the previous-bar confirmation gate and micro-momentum gate were structurally wrong for reversal strategies (`bearish_momentum`, `mean_reversion`). A reversal signal *should* fire against recent momentum by design. Both gates now have a `CONTINUATION_STRATEGIES = {"vwap_momentum_breakout"}` check. If you add a new strategy, decide whether it's continuation or reversal before wiring gates.
- **Don't use tick-COUNT windows for option-premium confirmation** — "last 10 ticks" can span 20 minutes on illiquid strikes and produce meaningless slopes. The Fix D gate uses a 30-SECOND time window and fails open if <8 ticks exist in that window.
- **Don't check SL against `min(p_low_bid, self.sl)` after ratcheting SL within the same bar** — this was the 2026-04-08 trailing-stop giveback bug. Bars that spiked +30% then crashed to -5% activated trailing AND filled at -5% instead of at the trailing lock. Check SL against PRIOR sl, ratchet only after no exit, fill at `prior_sl` (not bar low).
- **Don't use `incremental_train.py --days N` where N > 3** — safety guard rejects it; use `main.py train` for full retrains
- **Don't retrain macro/micro models without a stable backtest baseline** — retraining changes XGBoost weights → different entry bars → RL_EXIT may stop firing → P&L collapses. Always restore from `backups/` if results degrade.
- **Don't use `generate_macro_labels(threshold > 0.002, forward_periods > 20)`** — higher thresholds yield <5% positive rate → model outputs near-0 for all bars → directional_prob for PUT ≈ 1.0 constant → no signal filtering. Keep `threshold=0.001, forward_periods=15`.
- **Don't convert IST timestamps to UTC when joining backtest CSVs to DB candles** — both store IST mislabeled as +00:00 (no actual offset). Strip tzinfo and compare naively.
- **Don't write candles with raw `write_df(..., if_exists='append')`** — use `upsert_candles()` which handles `ON CONFLICT DO NOTHING` on (timestamp, symbol). Bare append causes duplicate key errors.
- **Don't delete today's candles and re-insert inside `upsert_candles()`** — do the DELETE separately before calling upsert if you need a clean replace
- **Don't start `collect_ticks.py` as a plain `&` subprocess from a Bash shell** — it gets SIGHUP when the shell exits. Always use `nohup ... &`
- **Don't store the tick's market timestamp as `ts` in live_price_cache** — for illiquid options, the market timestamp can be hours old, making Flask think prices are stale and killing the collector. Use `datetime.now().isoformat()` (wall-clock receipt time)
- **Don't call `ws_start_streaming(callback)` multiple times with the same callback** — it deduplicates now, but prior reconnect loops created duplicate stream threads. The `_stream_loop` handles reconnection internally; only call `ws_start_streaming()` once at startup
- **AngelOne SNAP_QUOTE ticks are dicts, not the old TrueData array format** — fields like `last_traded_price` are paise-scaled (divide by 100). The token→symbol map is built in `ws_subscribe()` and used in `_parse_ws_tick()`.
- **Don't parse `minute_candles` timestamps as naive datetimes** — they're stored as UTC TIMESTAMPTZ; when comparing to `datetime.now()` (IST), the 5:30h offset matters
- **Don't add `print()` statements to Flask routes** — use `logger` (Flask stdout is not shown in production)
- **Don't run Flask in debug mode with the reloader** — the reloader forks processes and double-starts background threads (scanner + tick monitor run twice)
- **Don't re-enable `max_trades_day` gate until outcome models have AUC > 0.55** — currently disabled in both `backend/app.py` and `tick_replay_backtest.py` to collect more training data. The gate is commented with `# TEMP: disabled to collect more training data`

---

## Environment Variables (.env)

```env
# Required
DB_HOST=localhost
DB_PORT=5432
DB_NAME=trading
DB_USER=postgres
DB_PASSWORD=postgres

ANGEL_API_KEY=your_smartapi_key
ANGEL_CLIENT_CODE=your_client_code
ANGEL_PASSWORD_OR_PIN=your_trading_pin
ANGEL_TOTP_SECRET=your_base32_totp_secret

# mStock (Type A) — the broker for live order execution once TRADE_MODE
# leaves "paper". AngelOne above still powers all market data regardless.
MSTOCK_API_KEY=your_mstock_api_key
MSTOCK_USER_ID=your_mstock_user_id
MSTOCK_PASSWORD=your_mstock_password
MSTOCK_TOTP_SECRET=your_base32_totp_secret

TRADE_MODE=paper         # "paper" (default) | "angelone" | "mstock"

# Optional overrides
INITIAL_CAPITAL=50000
ATM_RANGE=3              # strikes ±N from ATM to subscribe (default 3 → 14 option contracts)
MAX_SYMBOLS=50           # AngelOne plan limit
SCORE_THRESHOLD=0.6
LOG_LEVEL=INFO
MODEL_DIR=models/saved
```

---

## Important Operational Notes

- **Market hours**: 9:15–15:30 IST weekdays. `_is_market_hours()` guards scanner and collector startup
- **NIFTY lot size**: 65 units (1 lot = 65 shares)
- **NIFTY weekly expiry**: Tuesdays by default, but individual weeks can be holiday-shifted (e.g. Apr 14 2026 → Apr 13 because Apr 14 is a national holiday). Never hard-code a weekday; always fetch from AngelOne's instrument master.
- **ATM strike gap**: 50 points for NIFTY
- **TickCollector buffer**: flushes to `tick_data` every 200 ticks
- **Candle aggregation**: done by `collect_ticks.py` in-memory per minute, written via `upsert_candles()`
- **Tick gap backfill**: `_backfill_ticks_if_stale()` runs every scan cycle during market hours and detects leading/internal/trailing gaps in today's `tick_data` — **dormant**: AngelOne's free tier has no historical tick endpoint, so it never finds anything to backfill. See the "Known gap" note above.
- **Model backups**: stored in `models/saved/backups/` with timestamp suffix before each retrain
- **Logs**: `logs/tick_collector_YYYYMMDD.log` (one file per day), `logs/trading.log`
- **Backtest results**: `backtest_results/*.json` and `*.csv`
- **Backtest mode**: `scripts/tick_replay_backtest.py` uses tick-level option premium data via `load_option_premiums_for_day()`. The function detects tick availability and falls back to minute candles only when <50 ticks exist. Exit logic (SL/target/trailing) walks individual ticks within each minute when in tick mode.
- **Entry gates** (in order, all in `backend/app.py:scan_market()` and `scripts/tick_replay_backtest.py`):
    1. Score threshold (≥ 0.75, strategy-specific raises: mean_reversion 0.80, vwap 0.78)
    2. Previous-bar direction confirmation — **continuation strategies only**
    3. Micro-momentum confirmation (tick_momentum) — **continuation strategies only**
    4. Option-premium confirmation (Fix D) — **all strategies**, 30s window, 0.8% drop threshold
- **Intra-trade regime exit**: `_tick_monitor_loop` has three tiers — 0.10% adverse NIFTY move (log), 0.20% (tighten SL to entry+2%), 0.30% (HARD EXIT if option is underwater). Check interval 10s.
