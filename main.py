"""
AI Trading System - Main Entry Point

Supports:

  1. mock          - Generate mock data, build features (no DB required)
  2. train         - Train ML models from DB data (requires backend/app.py's
                     collector + scripts/backfill_today.py to have populated it)
  3. backtest      - Run backtest on mock data with full pipeline
  4. backtest-real - Run backtest on real historical DB data with a trained model

Usage:
  python main.py mock
  python main.py train
  python main.py backtest
  python main.py backtest-real

Note: this used to also have `ingest`/`live` modes built on a separate,
older execution/data-layer pipeline (Zerodha WebSocket client + a standalone
OrderManager under execution/). That pipeline was superseded by
backend/app.py + broker/order_manager.py + data/market_data_adapter.py (the
documented, actually-run system — see CLAUDE.md) and has been removed rather
than migrated twice.
"""

import sys

# Must run before utils.logger is imported — see utils/console.py.
from utils.console import fix_windows_console_encoding
fix_windows_console_encoding()

from config.settings import SYMBOLS
from utils.logger import get_logger

logger = get_logger("main")


def run_mock():
    """
    Generate mock data and run the feature pipeline end-to-end.
    No database required – operates entirely in-memory.
    """
    from data.mock_data import generate_all_mock_data
    from data.aggregator import AggregationEngine
    from features.indicators import compute_all_macro_indicators
    from features.micro_features import compute_micro_features

    logger.info("=" * 60)
    logger.info("MODE: MOCK DATA – generating synthetic dataset")
    logger.info("=" * 60)

    # 1. Generate mock data
    mock = generate_all_mock_data()
    minute_bars = mock["minute_bars"]
    ticks = mock["ticks"]
    option_chain = mock["option_chain"]

    logger.info(
        f"Mock data generated: "
        f"{len(minute_bars)} minute bars, "
        f"{len(ticks)} ticks, "
        f"{len(option_chain)} option contracts."
    )

    # 2. Demonstrate aggregation from ticks
    agg = AggregationEngine()
    for symbol in SYMBOLS:
        sym_ticks = ticks[ticks["symbol"] == symbol]
        candles = agg.aggregate_ticks_df(sym_ticks, symbol)
        for tf, df in candles.items():
            logger.info(f"  {symbol} {tf} candles: {len(df)} rows")

    # 3. Build macro features (from minute bars)
    for symbol in SYMBOLS:
        sym_minutes = minute_bars[minute_bars["symbol"] == symbol].copy()
        sym_options = option_chain[option_chain["symbol"] == symbol]

        macro_df = compute_all_macro_indicators(sym_minutes, sym_options)
        logger.info(
            f"  {symbol} macro features: {len(macro_df)} rows, "
            f"columns: {[c for c in macro_df.columns if c not in ['timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume']]}"
        )

    # 4. Build micro features (from ticks)
    for symbol in SYMBOLS:
        sym_ticks = ticks[ticks["symbol"] == symbol].copy()
        micro_df = compute_micro_features(sym_ticks)
        logger.info(
            f"  {symbol} micro features: {len(micro_df)} rows, "
            f"columns: {[c for c in micro_df.columns if c not in ['timestamp', 'symbol']]}"
        )

    logger.info("=" * 60)
    logger.info("Mock pipeline complete. All layers functional.")
    logger.info("=" * 60)


def run_train():
    """
    Train ML models from data in the database.
    Requires: TimescaleDB running + data populated via backend/app.py's live collector or scripts/backfill_today.py.

    NOTE: Only call this with real AngelOne-sourced data, never with mock data.
    """
    from features.feature_engine import build_macro_features, build_micro_features
    from models.train_model import train_all_models

    logger.info("=" * 60)
    logger.info("MODE: TRAIN - training ML models from DB data")
    logger.info("=" * 60)

    for symbol in SYMBOLS:
        logger.info(f"Building features for {symbol}...")
        macro_df = build_macro_features(symbol)
        micro_df = build_micro_features(symbol)

        if macro_df.empty:
            logger.warning(f"No macro features for {symbol}. Run scripts/backfill_today.py first.")
            continue

        logger.info(f"Training models for {symbol}...")
        results = train_all_models(macro_df, micro_df)

        for model_type, metrics in results.items():
            logger.info(f"  {symbol} {model_type}: {metrics}")

    logger.info("Training complete.")


def run_backtest():
    """
    Run backtest on mock data through the full pipeline:
      Mock data -> Features -> Strategy signals -> Scoring -> SL/Target sim -> Metrics

    This demonstrates the entire system end-to-end without needing real data or DB.
    No ML models are trained on mock data - uses default 0.5 probability.
    Results are automatically exported to backtest_results/ directory.
    """
    from data.mock_data import generate_mock_minute_bars
    from features.indicators import compute_all_macro_indicators
    from backtest.backtest_engine import BacktestEngine
    from datetime import datetime

    logger.info("=" * 60)
    logger.info("MODE: BACKTEST - running strategy backtest on mock data")
    logger.info("=" * 60)
    logger.info("NOTE: ML probability fixed at 0.5 (no model trained on mock data)")

    engine = BacktestEngine()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for symbol in SYMBOLS:
        logger.info(f"\nBacktesting {symbol}...")

        # Generate mock minute bars
        minute_df = generate_mock_minute_bars(symbol, trading_days=125)

        # Compute features
        featured_df = compute_all_macro_indicators(minute_df)

        # Run backtest (no predictor = uses default 0.5 ML prob)
        result = engine.run(featured_df, symbol=symbol, predictor=None)

        logger.info(f"{symbol} backtest: {result.total_trades} trades, "
                     f"win_rate={result.win_rate:.1%}, PnL={result.gross_pnl:,.0f}")

        # Export results to files
        base_name = f"{symbol}_{timestamp}"
        result.export_all(base_name=base_name, output_dir="backtest_results")

    logger.info("\nBacktest complete. Results exported to backtest_results/ directory.")


def run_backtest_real():
    """
    Run backtest on REAL historical data from TimescaleDB with trained ML model.

    Pipeline:
      DB minute candles → Option chain enrichment → Feature computation
      → Strategy signals → ML scoring via Predictor → SL/Target sim → Metrics

    Requires: TimescaleDB with data + trained ML models.
    Results exported to backtest_results/ directory.
    """
    from features.feature_engine import build_macro_features
    from models.predict import Predictor
    from backtest.backtest_engine import BacktestEngine
    from config.settings import ANGEL_INDEX_FUTURES_SYMBOLS
    from datetime import datetime

    logger.info("=" * 60)
    logger.info("MODE: BACKTEST-REAL - backtesting on historical DB data with ML")
    logger.info("=" * 60)

    # Load trained ML model
    predictor = Predictor()
    predictor.load()
    if predictor.is_loaded:
        logger.info("ML Predictor loaded — using trained model for scoring.")
    else:
        logger.warning("No ML model found. Using default 0.5 probability.")
        predictor = None

    engine = BacktestEngine()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for symbol in SYMBOLS:
        index_sym = ANGEL_INDEX_FUTURES_SYMBOLS.get(symbol, symbol)
        logger.info(f"\nBacktesting {symbol} (index: {index_sym}) on real data...")

        # Build features from DB (includes option chain enrichment)
        featured_df = build_macro_features(index_sym)

        if featured_df.empty:
            logger.warning(f"No data for {index_sym}. Run scripts/backfill_today.py first.")
            continue

        logger.info(f"  Features: {featured_df.shape[0]} rows, {featured_df.shape[1]} cols")

        # Run backtest with trained ML model
        result = engine.run(featured_df, symbol=index_sym, predictor=predictor)

        logger.info(f"  {symbol} backtest: {result.total_trades} trades, "
                     f"win_rate={result.win_rate:.1%}, PnL=₹{result.gross_pnl:,.0f}")

        # Export results
        base_name = f"{symbol}_real_{timestamp}"
        result.export_all(base_name=base_name, output_dir="backtest_results")

    logger.info("\nReal-data backtest complete. Results in backtest_results/")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mock"

    modes = {
        "mock": run_mock,
        "train": run_train,
        "backtest": run_backtest,
        "backtest-real": run_backtest_real,
    }

    if mode in modes:
        modes[mode]()
    else:
        logger.error(f"Unknown mode: {mode}. Use: {' | '.join(modes.keys())}")
        sys.exit(1)


if __name__ == "__main__":
    main()