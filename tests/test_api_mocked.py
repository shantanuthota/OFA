"""Fast API schema checks with a fake forecaster; these tests do not download model weights."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from fastapi.testclient import TestClient

import main


class FakeChronosPipeline:
    def predict_df(self, df, future_df, id_column, timestamp_column, target,
                   prediction_length, quantile_levels, freq):
        targets = [target] if isinstance(target, str) else list(target)
        rows = []
        for col in targets:
            recent = float(pd.to_numeric(df[col], errors="coerce").dropna().iloc[-1])
            for i, ts in enumerate(future_df[timestamp_column]):
                median = max(0.0, recent * (1 + (i + 1) * 0.0005))
                rows.append({
                    id_column: future_df[id_column].iloc[i],
                    timestamp_column: ts,
                    "target_name": col,
                    "predictions": median,
                    "0.1": median * 0.95,
                    "0.5": median,
                    "0.9": median * 1.05,
                })
        return pd.DataFrame(rows)


def client_with_fake(monkeypatch):
    monkeypatch.setattr(main, "get_pipeline", lambda: FakeChronosPipeline())
    main._FORECAST_CACHE.clear()
    return TestClient(main.app)


def test_graph1_ap_retail(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get("/api/graphs/ap-retail?horizon_days=7")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["forecast"]) == 7
    assert body["forecast"][0]["date"] == "2026-10-16"
    assert {"p10_rs_kg", "predicted_price_rs_kg", "p90_rs_kg"}.issubset(body["forecast"][0])


def test_graph2_only_selected_states(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get("/api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale&horizon_days=14")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_states"] == ["AP", "MH"]
    assert {row["state"] for row in body["forecast"]} == {"AP", "MH"}
    assert {row["price_type"] for row in body["forecast"]} == {"retail", "wholesale"}
    assert len(body["forecast"]) == 14 * 4


def test_graph3_quantities_and_balance(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get("/api/graphs/supply-balance?states=AP&horizon_days=7")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["quantity_forecast"]) == 7 * 3
    assert len(body["balance_forecast"]) == 7
    assert "double-count" in " ".join(body["warnings"])


def test_invalid_horizon_rejected(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get("/api/graphs/ap-retail?horizon_days=10")
    assert response.status_code == 422


def test_graph1_both_ap_price_types_and_90_days(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get(
        "/api/graphs/ap-retail?horizon_days=90&history_days=1095&price_types=retail,wholesale"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected_price_types"] == ["RETAIL", "WHOLESALE"] or set(body["selected_price_types"]) == {"retail", "wholesale"}
    assert len(body["forecast"]) == 180
    assert body["forecast"][0]["date"] == "2026-10-16"
    assert body["model_context_days"] > 1000


def test_graph1_wholesale_only(monkeypatch):
    client = client_with_fake(monkeypatch)
    response = client.get("/api/graphs/ap-retail?horizon_days=7&price_types=wholesale")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(row["price_type"] for row in body["forecast"]) == {"wholesale"}
    assert len(body["forecast"]) == 7
