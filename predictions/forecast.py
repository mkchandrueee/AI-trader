"""
predictions/forecast.py
─────────────────────────
Thin wrapper around the vendored NSE-Neuron models, adapted from its
src/backend/services/nse_service.py (which was written for a FastAPI job
queue). This drops the FastAPI/job-queue plumbing and jugaad-data-ifies the
data source; the model/training/forecast logic itself is unchanged.

Call `forecast_symbol()` directly from Flask — no need to run NSE-Neuron's
own FastAPI server or its React frontend.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import predictions.nn_config as config
from predictions.utils.data_fetcher import _load_or_fetch, DEFAULT_LOOKBACK_DAYS
from predictions.utils.preprocessor import preprocess_nse_df
from predictions.utils.regime_detector import detect_regime, apply_regime_confidence
from predictions.utils.pattern_detector import detect_patterns, combine_regime_patterns

from predictions.models.lstm import lstm
from predictions.models.bilstm import bilstm
from predictions.models.gru import gru
from predictions.models.cnn_lstm import cnn_lstm
from predictions.models.classifiers.lstm import lstm_classifier
from predictions.models.classifiers.bilstm import bilstm_classifier
from predictions.models.classifiers.gru import gru_classifier
from predictions.models.classifiers.cnn_lstm import cnn_lstm_classifier

from utils.logger import get_logger

logger = get_logger("predictions")

ALGO_FUNCS = {"lstm": lstm, "bilstm": bilstm, "gru": gru, "cnn_lstm": cnn_lstm}
CLASSIFIER_FUNCS = {
    "lstm": lstm_classifier, "bilstm": bilstm_classifier,
    "gru": gru_classifier, "cnn_lstm": cnn_lstm_classifier,
}
ALGO_DISPLAY = {"lstm": "LSTM", "bilstm": "BiLSTM", "gru": "GRU", "cnn_lstm": "CNN-LSTM"}


def _fetch_data(symbol: str):
    """Fetch + preprocess daily bars for `symbol`. Returns (df, hist_df)."""
    from datetime import date, timedelta

    to_date = date.today()
    from_date = to_date - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    raw = _load_or_fetch(symbol, from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y"))
    df = preprocess_nse_df(raw)
    # Snapshot immediately — see the race-condition note in the original
    # nse_service.py: config.HISTORIC_DATA is a module global, and a
    # concurrent forecast for another symbol could overwrite it later.
    hist_df = config.HISTORIC_DATA
    return df, hist_df


def _regime_to_dict(regime: dict) -> dict:
    return {
        "regime": regime.get("regime", "UNKNOWN"),
        "recommended_model": regime.get("recommended_model", "LSTM"),
        "sufficient_data": regime.get("sufficient_data", False),
        "rows": regime.get("rows", 0),
        "sma_fast": regime.get("sma_fast"),
        "sma_slow": regime.get("sma_slow"),
        "description": regime.get("description", ""),
    }


def _historical_ohlc(hist_df: pd.DataFrame, n: int = 120) -> list:
    if hist_df is None:
        return []
    mandatory = [c for c in ["date", "high", "low", "close"] if c in hist_df.columns]
    optional = [c for c in ["open"] if c in hist_df.columns]
    tail = hist_df[mandatory + optional].tail(n).copy()
    tail = tail.dropna(subset=mandatory)
    if "date" in tail.columns:
        tail = tail.drop_duplicates(subset=["date"], keep="last")

    records = []
    for _, row in tail.iterrows():
        rec = {}
        for k in mandatory:
            rec[k] = str(row[k]) if k == "date" else (float(row[k]) if pd.notna(row[k]) else None)
        for k in optional:
            rec[k] = float(row[k]) if pd.notna(row[k]) else None
        records.append(rec)
    return records


def _build_forecast_days(pred: np.ndarray, signals: list | None, hist_df: pd.DataFrame) -> list:
    last_date = pd.to_datetime(hist_df["Date"]).max()
    future_dates = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=config.FORECAST_DAYS)
    result = []
    for i in range(config.FORECAST_DAYS):
        item = {
            "date": future_dates[i].strftime("%Y-%m-%d"),
            "high": round(float(pred[i][0]), 2),
            "low": round(float(pred[i][1]), 2),
            "close": round(float(pred[i][2]), 2),
            "prev_close": round(float(pred[i][3]), 2),
            "signal": None,
        }
        if signals and i < len(signals):
            sig = signals[i]
            item["signal"] = {
                "label": sig.get("label"),
                "confidence": sig.get("confidence"),
                "regime_adjusted": sig.get("regime_adjusted", False),
                "regime_direction": sig.get("regime_direction", "—"),
            }
        result.append(item)
    return result


def forecast_symbol(symbol: str, algorithm: str = "lstm", force_retrain: bool = False) -> dict:
    """
    Train/load one model, run its classifier, apply regime confidence.
    Returns a JSON-serializable dict — same shape NSE-Neuron's own FastAPI
    route returned, minus the job-progress fields.
    """
    if algorithm not in ALGO_FUNCS:
        raise ValueError(f"Unknown algorithm: {algorithm}. Choose from {list(ALGO_FUNCS)}")

    df, hist_df = _fetch_data(symbol)

    pred, rmse, model_obj = ALGO_FUNCS[algorithm](df, symbol=symbol, force_retrain=force_retrain, return_model=True)
    rmse_val = rmse["close"] if isinstance(rmse, dict) else float(rmse)
    cache_status = getattr(model_obj, "cache_status", "miss")

    signals = CLASSIFIER_FUNCS[algorithm](df, pred, symbol=symbol, force_retrain=force_retrain)

    regime = detect_regime(df)
    if regime["sufficient_data"] and signals:
        signals = apply_regime_confidence(signals, regime)

    return {
        "symbol": symbol,
        "algorithm": algorithm,
        "display_name": ALGO_DISPLAY[algorithm],
        "forecast": _build_forecast_days(pred, signals, hist_df),
        "rmse": round(rmse_val, 6),
        "regime": _regime_to_dict(regime),
        "historical": _historical_ohlc(hist_df),
        "cache_status": cache_status,
    }


def forecast_all(symbol: str, force_retrain: bool = False) -> dict:
    """Train all four models, compare RMSE, return the best. No signals in "all" mode."""
    df, hist_df = _fetch_data(symbol)

    all_preds, all_rmse, all_cache = {}, {}, {}
    for algo, func in ALGO_FUNCS.items():
        pred, rmse, model_obj = func(df, symbol=symbol, force_retrain=force_retrain, return_model=True)
        all_preds[algo] = pred.tolist()
        all_rmse[algo] = round(rmse["close"] if isinstance(rmse, dict) else float(rmse), 6)
        all_cache[algo] = getattr(model_obj, "cache_status", "miss")

    regime = detect_regime(df)
    last_date = pd.to_datetime(hist_df["Date"]).max()
    future_dates = pd.bdate_range(start=last_date + pd.Timedelta(days=1), periods=config.FORECAST_DAYS)

    algo_forecasts = {}
    for algo, pred_list in all_preds.items():
        pred_arr = np.array(pred_list)
        algo_forecasts[algo] = [
            {
                "date": future_dates[i].strftime("%Y-%m-%d"),
                "high": round(float(pred_arr[i][0]), 2),
                "low": round(float(pred_arr[i][1]), 2),
                "close": round(float(pred_arr[i][2]), 2),
                "prev_close": round(float(pred_arr[i][3]), 2),
            }
            for i in range(config.FORECAST_DAYS)
        ]

    best_algo = min(all_rmse, key=all_rmse.get)
    return {
        "symbol": symbol,
        "algorithm": "all",
        "algo_forecasts": algo_forecasts,
        "all_rmse": all_rmse,
        "best_algo": best_algo,
        "regime": _regime_to_dict(regime),
        "historical": _historical_ohlc(hist_df),
        "cache_per_algo": all_cache,
    }


def regime_analysis(symbol: str) -> dict:
    """Fetch data, detect regime + candlestick patterns, no model training."""
    df, hist_df = _fetch_data(symbol)
    regime = detect_regime(df)
    pattern_df, active_pats = detect_patterns(hist_df)
    patterns = [
        {"name": pat.replace("_", " "), "value": val, "direction": "Bullish" if val > 0 else "Bearish", "date": date_str}
        for pat, val, date_str in active_pats
    ]
    return {
        "symbol": symbol,
        "regime": _regime_to_dict(regime),
        "patterns": patterns,
        "insight": combine_regime_patterns(regime, active_pats),
        "historical": _historical_ohlc(hist_df),
    }
