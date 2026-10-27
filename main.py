"""FastAPI backend for the onion forecasting MVP (3 graph APIs + data operations).

Graph 1: Andhra Pradesh retail prices.
Graph 2: selected states' retail and mandi modal prices.
Graph 3: arrivals, production, consumption, and an explicitly provisional balance proxy.

Chronos-2 is loaded lazily so /health and the API docs remain accessible before the
model package/weights are installed. First genuine forecast may download weights.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from history_store import (
    CSV_PATH,
    DB_PATH,
    PRICE_COLUMNS,
    QUANTITY_COLUMNS,
    STATE_CODES,
    STATE_NAMES,
    ingestion_status,
    load_history,
    sync_model_ready_csv,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("onion-forecast-api")
MODEL_ID = os.getenv("CHRONOS_MODEL_ID", "amazon/chronos-2")
QUANTILES = [0.1, 0.5, 0.9]
ALLOWED_HORIZONS = (7, 14, 30, 60, 90)
# Graph 1 configuration
AP_MODEL_CONTEXT_YEARS = 3 
AP_DISPLAY_HISTORY_MONTHS = 3
CALENDAR_COLUMNS = ["month", "day_of_week", "day_of_year_sin", "day_of_year_cos"]

app = FastAPI(
    title="Onion Forecasting MVP API",
    version="2.0.0",
    description=(
        "Chronos-2 daily forecasts for retail/mandi prices and supply quantities. "
        "P10-P90 is a nominal prediction interval, not a scalar confidence score."
    ),
)

allowed_origins = [
    x.strip() for x in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173,http://localhost:8501",
    ).split(",") if x.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_data() -> pd.DataFrame:
    """Always read the current DB snapshot; new ingestion is visible without API restart."""
    try:
        return load_history()
    except Exception as exc:
        logger.exception("Could not load market history")
        raise HTTPException(status_code=500, detail=f"Could not load market history: {exc}") from exc


@lru_cache(maxsize=1)
def get_pipeline():
    """Load Chronos-2 once per API process."""
    try:
        import torch
        from chronos import Chronos2Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Chronos-2 dependencies are missing. Activate the project's virtual environment and run: "
            "pip install -r requirements.txt"
        ) from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading %s on %s; first load may download model weights", MODEL_ID, device)
    return Chronos2Pipeline.from_pretrained(MODEL_ID, device_map=device)


def _add_calendar_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    ts = pd.to_datetime(out["timestamp"])
    out["month"] = ts.dt.month.astype("int64")
    out["day_of_week"] = ts.dt.dayofweek.astype("int64")
    doy = ts.dt.dayofyear.astype(float)
    out["day_of_year_sin"] = np.sin(2.0 * np.pi * doy / 365.25)
    out["day_of_year_cos"] = np.cos(2.0 * np.pi * doy / 365.25)
    return out


def _validate_horizon(horizon_days: int) -> int:
    if horizon_days not in ALLOWED_HORIZONS:
        raise HTTPException(status_code=422, detail=f"horizon_days must be one of {ALLOWED_HORIZONS}.")
    return horizon_days


def _parse_list(raw: str, allowed: set[str], label: str) -> list[str]:
    values = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not values:
        raise HTTPException(status_code=422, detail=f"Select at least one {label}.")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported {label}: {unknown}. Allowed values: {sorted(allowed)}",
        )
    return list(dict.fromkeys(values))


def _future_frame(data: pd.DataFrame, horizon_days: int) -> pd.DataFrame:
    last_date = pd.Timestamp(data["timestamp"].max()).normalize()
    future_dates = pd.date_range(
        start=last_date + pd.Timedelta(days=1), periods=horizon_days, freq="D"
    )
    future = pd.DataFrame({
        "item_id": [str(data["item_id"].iloc[0])] * horizon_days,
        "timestamp": future_dates,
    })
    return _add_calendar_features(future)


# In-process forecast cache: invalidated automatically by a changed historical context hash.
_FORECAST_CACHE: dict[str, pd.DataFrame] = {}
_FORECAST_CACHE_MAX = 16


def _predict(data: pd.DataFrame, target: str | list[str], horizon_days: int) -> pd.DataFrame:
    horizon_days = _validate_horizon(horizon_days)
    context = data.copy()
    context["timestamp"] = pd.to_datetime(context["timestamp"])
    context = _add_calendar_features(context)
    future = _future_frame(data, horizon_days)
    targets = [target] if isinstance(target, str) else list(target)
    context_signature = hashlib.sha256(
        pd.util.hash_pandas_object(context, index=True).values.tobytes()
    ).hexdigest()[:20]
    cache_key = f"{context_signature}|{','.join(targets)}|{horizon_days}|{MODEL_ID}"
    if cache_key in _FORECAST_CACHE:
        return _FORECAST_CACHE[cache_key].copy(deep=True)

    try:
        pipeline = get_pipeline()
        result = pipeline.predict_df(
            df=context,
            future_df=future,
            id_column="item_id",
            timestamp_column="timestamp",
            target=target,
            prediction_length=horizon_days,
            quantile_levels=QUANTILES,
            freq="D",
        )
    except RuntimeError as exc:
        logger.exception("Chronos-2 is unavailable")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Chronos-2 forecast failed")
        raise HTTPException(
            status_code=500,
            detail=f"Chronos-2 forecast failed: {type(exc).__name__}: {exc}",
        ) from exc

    required_output = {"timestamp", "target_name", "0.1", "0.5", "0.9"}
    missing = required_output - set(result.columns)
    if missing:
        raise HTTPException(status_code=500, detail=f"Chronos-2 output missing columns: {sorted(missing)}")
    result["timestamp"] = pd.to_datetime(result["timestamp"])
    if len(_FORECAST_CACHE) >= _FORECAST_CACHE_MAX:
        _FORECAST_CACHE.pop(next(iter(_FORECAST_CACHE)))
    _FORECAST_CACHE[cache_key] = result.copy(deep=True)
    return result


def _series_point(row: pd.Series, value_key: str = "predicted_value") -> dict:
    # Quantiles can be slightly noisy; clip values at zero for prices/physical quantities.
    p10 = max(0.0, float(row["0.1"]))
    p50 = max(0.0, float(row["0.5"]))
    p90 = max(0.0, float(row["0.9"]))
    p10, p50, p90 = min(p10, p50), p50, max(p90, p50)
    width = p90 - p10
    return {
        "date": pd.Timestamp(row["timestamp"]).date().isoformat(),
        value_key: round(p50, 4),
        "p10": round(p10, 4),
        "p90": round(p90, 4),
        "prediction_interval_width": round(width, 4),
        "interval_width_pct_of_median": round((width / max(abs(p50), 1e-9)) * 100.0, 2),
    }


def _history_points(data: pd.DataFrame, target_col: str, history_days: int, value_key: str) -> list[dict]:
    cutoff = pd.Timestamp(data["timestamp"].max()).normalize() - pd.Timedelta(days=history_days - 1)
    recent = data.loc[data["timestamp"] >= cutoff, ["timestamp", target_col]]
    return [
        {"date": row.timestamp.date().isoformat(), value_key: round(float(getattr(row, target_col)), 4)}
        for row in recent.itertuples(index=False)
    ]


def _data_status(data: pd.DataFrame) -> dict:
    last_date = pd.Timestamp(data["timestamp"].max()).date()
    staleness = (date.today() - last_date).days
    warnings: list[str] = []
    if staleness > 1:
        warnings.append(
            f"Latest observation is {last_date.isoformat()} ({staleness} day(s) behind the API server date). "
            "The forecast starts after the last actual observation; ingest current observations before using it operationally."
        )
    if staleness < 0:
        warnings.append("The latest observation date is in the future relative to the server date; verify source timestamps.")
    return {
        "last_observation_date": last_date.isoformat(),
        "data_staleness_days": staleness,
        "forecast_start_date": (last_date + pd.Timedelta(days=1)).isoformat(),
        "warnings": warnings,
    }


def _metadata() -> dict:
    return {
        "model": MODEL_ID,
        "frequency": "daily",
        "price_unit": "INR/kg",
        "quantity_unit": "tonnes per reported period (source unit needs confirmation)",
        "quantiles": {"lower": 0.1, "median": 0.5, "upper": 0.9},
        "prediction_interval": {
            "nominal_coverage": 0.8,
            "description": "P10-P90 is a nominal central 80% prediction interval, not a guarantee of coverage.",
        },
        "confidence_score": {
            "available": False,
            "reason": "Chronos-2 returns forecast quantiles, not a native calibrated scalar confidence score.",
        },
        "uncertainty_indicator": "interval_width_pct_of_median is a spread indicator, not calibrated confidence.",
    }


def _selected_context(data: pd.DataFrame, states: list[str]) -> pd.DataFrame:
    cols = ["item_id", "timestamp"]
    for state in states:
        cols.extend(PRICE_COLUMNS[state].values())
        cols.extend(QUANTITY_COLUMNS[state].values())
    return data[list(dict.fromkeys(cols))].copy()


@app.get("/")
def root() -> dict:
    return {
        "service": "Onion Forecasting MVP API",
        "docs": "/docs",
        "graphs": {
            "graph_1_ap_retail": "/api/graphs/ap-retail?horizon_days=7",
            "graph_2_state_comparison": "/api/graphs/state-comparison?states=AP,MH&horizon_days=14",
            "graph_3_supply_balance": "/api/graphs/supply-balance?states=AP&horizon_days=7",
        },
        "ops": {"health": "/health", "ingestion_status": "/api/ingestion/status"},
    }


@app.get("/health")
def health() -> dict:
    data = get_data()
    return {
        "status": "ok",
        "model_loaded": get_pipeline.cache_info().currsize > 0,
        "model_id": MODEL_ID,
        "history_db": str(DB_PATH),
        "ingestion": ingestion_status(),
        **_data_status(data),
    }


@app.get("/api/data/status")
def data_status() -> dict:
    data = get_data()
    return {"ingestion": ingestion_status(), **_data_status(data)}


@app.get("/api/ingestion/status")
def ingestion_status_endpoint() -> dict:
    return ingestion_status()


@app.post("/api/ingestion/sync-csv")
def sync_csv_endpoint(
    filename: str = Query(..., description="CSV filename already present inside project data/ folder"),
    x_ingestion_key: str | None = Header(default=None),
) -> dict:
    """Upsert an already-downloaded combined model-ready CSV into SQLite.

    Intentionally does not fetch arbitrary UPAg endpoints. Source-specific schema and
    authentication must be verified first. Put the normalized CSV in data/ and call
    this endpoint with the ingestion key configured in .env.
    """
    configured_key = os.getenv("INGESTION_API_KEY", "").strip()
    if not configured_key or configured_key == "change-this-local-secret":
        raise HTTPException(status_code=503, detail="Set a unique INGESTION_API_KEY in .env before enabling ingestion.")
    if not x_ingestion_key or x_ingestion_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Ingestion-Key header.")
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="filename must be a CSV filename without directory paths.")
    candidate = (ROOT / "data" / safe_name).resolve()
    if candidate.parent != (ROOT / "data").resolve() or not candidate.exists():
        raise HTTPException(status_code=404, detail="CSV file not found inside the project data/ folder.")
    try:
        result = sync_model_ready_csv(candidate, source=f"csv_import:{safe_name}")
    except Exception as exc:
        logger.exception("CSV history sync failed")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _FORECAST_CACHE.clear()
    return {"status": "success", **result.as_dict(), "history": ingestion_status()}


@app.get("/api/graphs/ap-retail")

@app.get("/api/graphs/ap-retail")
def ap_retail_graph(
    horizon_days: int = Query(
        14,
        description="Forecast horizon: 7, 14, 30, 60, or 90 days",
    ),
    price_types: str = Query(
        "retail",
        description="Comma-separated values: retail, wholesale",
    ),
) -> dict:
    """
    Graph 1:
    - Chronos-2 always receives the latest 3 years of history.
    - The graph displays only the last 6 calendar months of actual prices.
    - Forecast horizon is independently selectable.
    """
    data = get_data()
    selected_types = [
        x.lower()
        for x in _parse_list(
            price_types,
            {"RETAIL", "WHOLESALE"},
            "price types",
        )
    ]

    # Ensure timestamps are consistently represented as datetimes.
    data = data.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"])
    data = data.sort_values("timestamp")

    latest_date = pd.Timestamp(data["timestamp"].max()).normalize()

    # 1. MODEL CONTEXT: latest 3 years.
    # This entire filtered dataset is passed into Chronos-2.
    model_cutoff = latest_date - pd.DateOffset(
        years=AP_MODEL_CONTEXT_YEARS
    )

    context = data.loc[
        data["timestamp"] >= model_cutoff
    ].copy()

    if context.empty:
        raise HTTPException(
            status_code=422,
            detail="No observations available for the 3-year model context.",
        )

    # 2. DISPLAY HISTORY: latest 6 calendar months.
    # This dataset is used only for the actual-price lines in the graph.
    display_cutoff = latest_date - pd.DateOffset(
        months=AP_DISPLAY_HISTORY_MONTHS
    )

    visible_history = data.loc[
        data["timestamp"] >= display_cutoff
    ].copy()

    # 3. Forecast using the full 3-year model context.
    _validate_horizon(horizon_days)

    targets = [
        PRICE_COLUMNS["AP"][price_type]
        for price_type in selected_types
    ]

    prediction = _predict(
        context,
        target=targets,
        horizon_days=horizon_days,
    )

    target_map = {
        PRICE_COLUMNS["AP"][price_type]: price_type
        for price_type in selected_types
    }

    forecast = []

    for _, row in prediction.iterrows():
        target_name = str(row["target_name"])

        if target_name not in target_map:
            continue

        price_type = target_map[target_name]
        point = _series_point(
            row,
            value_key="predicted_price_rs_kg",
        )

        forecast.append({
            "date": point["date"],
            "state": "AP",
            "price_type": price_type,
            "price_type_label": (
                "Retail price"
                if price_type == "retail"
                else "Mandi modal price"
            ),
            "predicted_price_rs_kg": point["predicted_price_rs_kg"],
            "p10_rs_kg": point["p10"],
            "p90_rs_kg": point["p90"],
            "prediction_interval_width_rs_kg": (
                point["prediction_interval_width"]
            ),
            "interval_width_pct_of_median": (
                point["interval_width_pct_of_median"]
            ),
        })

    # 4. Build actual historical chart points using only the
    # visible six-month window, not the full model context.
    historical = []

    for price_type in selected_types:
        target_col = PRICE_COLUMNS["AP"][price_type]

        for row in visible_history[
            ["timestamp", target_col]
        ].itertuples(index=False):

            value = getattr(row, target_col)

            if pd.notna(value):
                historical.append({
                    "date": row.timestamp.date().isoformat(),
                    "state": "AP",
                    "price_type": price_type,
                    "price_type_label": (
                        "Retail price"
                        if price_type == "retail"
                        else "Mandi modal price"
                    ),
                    "actual_price_rs_kg": round(float(value), 4),
                })

    status = _data_status(data)

    return {
        "graph": "ap_price_forecast",
        "title": "Andhra Pradesh Onion Retail and Mandi Modal Price Forecast",
        "state": "AP",
        "state_name": STATE_NAMES["AP"],
        "selected_price_types": selected_types,
        "unit": "INR/kg",
        "horizon_days": horizon_days,

        # Explicitly distinguish model history from chart history.
        "model_context_years": AP_MODEL_CONTEXT_YEARS,
        "model_context_days": len(context),
        "model_context_start_date": (
            context["timestamp"].min().date().isoformat()
        ),
        "model_context_end_date": (
            context["timestamp"].max().date().isoformat()
        ),
        "display_history_months": AP_DISPLAY_HISTORY_MONTHS,
        "display_history_start_date": (
            visible_history["timestamp"].min().date().isoformat()
            if not visible_history.empty
            else None
        ),

        "historical": historical,
        "forecast": forecast,
        "metadata": _metadata(),
        **status,
        "forecast_end_date": forecast[-1]["date"] if forecast else None,
    }



@app.get("/api/graphs/state-comparison")
def state_comparison_graph(
    states: str = Query("AP,KA,MH,MP", description="Comma-separated state codes: AP, KA, MH, MP"),
    horizon_days: int = Query(14, ge=7, le=90, description="Forecast horizon: 7, 14, 30, 60, or 90 days"),
    price_types: str = Query("retail,wholesale", description="Comma-separated values: retail, wholesale"),
    history_days: int = Query(90, ge=30, le=730, description="Number of recent actual days to return"),
) -> dict:
    """Graph 2: forecast only the selected states and retail/wholesale price types."""
    data = get_data()
    selected_states = _parse_list(states, set(STATE_CODES), "states")
    selected_types = [x.lower() for x in _parse_list(price_types, {"RETAIL", "WHOLESALE"}, "price types")]
    targets = [PRICE_COLUMNS[s][t] for s in selected_states for t in selected_types]
    context = _selected_context(data, selected_states)
    prediction = _predict(context, target=targets, horizon_days=horizon_days)
    target_map = {
        PRICE_COLUMNS[s][t]: (s, t)
        for s in selected_states for t in selected_types
    }
    forecast: list[dict] = []
    for _, row in prediction.iterrows():
        target_name = str(row["target_name"])
        if target_name not in target_map:
            continue
        state, price_type = target_map[target_name]
        point = _series_point(row, value_key="predicted_price_rs_kg")
        forecast.append({
            "date": point["date"],
            "state": state,
            "state_name": STATE_NAMES[state],
            "price_type": price_type,
            "predicted_price_rs_kg": point["predicted_price_rs_kg"],
            "p10_rs_kg": point["p10"],
            "p90_rs_kg": point["p90"],
            "interval_width_pct_of_median": point["interval_width_pct_of_median"],
        })

    cutoff = pd.Timestamp(data["timestamp"].max()).normalize() - pd.Timedelta(days=history_days - 1)
    historical: list[dict] = []
    for state in selected_states:
        for price_type in selected_types:
            col = PRICE_COLUMNS[state][price_type]
            for row in data.loc[data["timestamp"] >= cutoff, ["timestamp", col]].itertuples(index=False):
                historical.append({
                    "date": row.timestamp.date().isoformat(),
                    "state": state,
                    "state_name": STATE_NAMES[state],
                    "price_type": price_type,
                    "actual_price_rs_kg": round(float(getattr(row, col)), 4),
                })
    return {
        "graph": "state_price_comparison",
        "title": "Selected States — Onion Retail and Wholesale Price Forecasts",
        "selected_states": selected_states,
        "selected_state_names": [STATE_NAMES[x] for x in selected_states],
        "selected_price_types": selected_types,
        "horizon_days": horizon_days,
        "unit": "INR/kg",
        "historical": historical,
        "forecast": forecast,
        "metadata": _metadata(),
        **_data_status(data),
        "forecast_end_date": max((x["date"] for x in forecast), default=None),
    }


@app.get("/api/graphs/supply-balance")
def supply_balance_graph(
    states: str = Query("AP", description="Comma-separated state codes: AP, KA, MH, MP"),
    horizon_days: int = Query(14, ge=7, le=90, description="Forecast horizon: 7, 14, 30, 60, or 90 days"),
    history_days: int = Query(90, ge=30, le=730, description="Number of recent actual days to return"),
) -> dict:
    """Graph 3: quantity forecasts plus a provisional arrival + production - consumption proxy."""
    data = get_data()
    selected_states = _parse_list(states, set(STATE_CODES), "states")
    context = _selected_context(data, selected_states)
    targets = [QUANTITY_COLUMNS[s][m] for s in selected_states for m in ("arrival", "production", "consumption")]
    prediction = _predict(context, target=targets, horizon_days=horizon_days)
    target_map = {
        QUANTITY_COLUMNS[s][m]: (s, m)
        for s in selected_states for m in ("arrival", "production", "consumption")
    }
    quantity_forecasts: list[dict] = []
    indexed: dict[tuple[str, str], dict[str, dict]] = {}
    for _, row in prediction.iterrows():
        target_name = str(row["target_name"])
        if target_name not in target_map:
            continue
        state, metric = target_map[target_name]
        point = _series_point(row, value_key="predicted_quantity_t")
        item = {
            "date": point["date"],
            "state": state,
            "state_name": STATE_NAMES[state],
            "metric": metric,
            "predicted_quantity_t": point["predicted_quantity_t"],
            "p10_t": point["p10"],
            "p90_t": point["p90"],
            "interval_width_pct_of_median": point["interval_width_pct_of_median"],
        }
        quantity_forecasts.append(item)
        indexed.setdefault((state, point["date"]), {})[metric] = item

    cutoff = pd.Timestamp(data["timestamp"].max()).normalize() - pd.Timedelta(days=history_days - 1)
    historical: list[dict] = []
    for state in selected_states:
        for metric in ("arrival", "production", "consumption"):
            col = QUANTITY_COLUMNS[state][metric]
            for row in data.loc[data["timestamp"] >= cutoff, ["timestamp", col]].itertuples(index=False):
                historical.append({
                    "date": row.timestamp.date().isoformat(),
                    "state": state,
                    "state_name": STATE_NAMES[state],
                    "metric": metric,
                    "actual_quantity_t": round(float(getattr(row, col)), 4),
                })

    balance: list[dict] = []
    for (state, date_str), items in sorted(indexed.items()):
        if not all(k in items for k in ("arrival", "production", "consumption")):
            continue
        arr, prod, cons = items["arrival"], items["production"], items["consumption"]
        p10 = arr["p10_t"] + prod["p10_t"] - cons["p90_t"]
        p50 = arr["predicted_quantity_t"] + prod["predicted_quantity_t"] - cons["predicted_quantity_t"]
        p90 = arr["p90_t"] + prod["p90_t"] - cons["p10_t"]
        balance.append({
            "date": date_str,
            "state": state,
            "state_name": STATE_NAMES[state],
            "balance_proxy_p10_t": round(p10, 4),
            "balance_proxy_p50_t": round(p50, 4),
            "balance_proxy_p90_t": round(p90, 4),
            "classification": "potential_surplus" if p50 > 0 else ("potential_shortfall" if p50 < 0 else "near_balance"),
        })

    warnings = [
        "Balance is provisional: balance proxy = arrivals + production - consumption.",
        "Confirm units, geographic scope, and reporting frequencies with the data owner before using this as a true surplus/shortfall.",
        "If arrivals are part of the same production flow, adding arrivals and production can double-count supply.",
        "Production and consumption in the supplied CSV repeat across many daily rows; daily forecasts may reflect repeated periodic estimates rather than genuine daily measurements.",
        "Balance bounds combine component quantiles conservatively and are not a calibrated joint prediction interval.",
    ]
    status = _data_status(data)
    all_warnings = warnings + status.get("warnings", [])
    status_without_warnings = {k: v for k, v in status.items() if k != "warnings"}
    return {
        "graph": "quantity_forecast_and_balance_proxy",
        "title": "Onion Quantity Forecasts and Indicative Supply-Demand Balance",
        "selected_states": selected_states,
        "selected_state_names": [STATE_NAMES[x] for x in selected_states],
        "horizon_days": horizon_days,
        "quantity_unit": "tonnes; confirm the reporting period for each source field",
        "historical": historical,
        "quantity_forecast": quantity_forecasts,
        "balance_forecast": balance,
        "balance_formula": "arrival + production - consumption",
        "warnings": all_warnings,
        "metadata": _metadata(),
        **status_without_warnings,
        "forecast_end_date": max((x["date"] for x in quantity_forecasts), default=None),
    }
