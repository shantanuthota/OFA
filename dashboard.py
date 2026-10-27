"""Small Streamlit dashboard for Graphs 1-3. Run after starting the FastAPI backend."""
from __future__ import annotations

import os
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative
import requests
import streamlit as st
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

st.set_page_config(page_title="Onion Forecasting MVP", page_icon="🧅", layout="wide")
st.title("🧅 Onion Price & Supply Forecasting")
st.caption("Chronos-2 · Andhra Pradesh, Karnataka, Maharashtra and Madhya Pradesh")

with st.sidebar:
    st.header("Connection")
    api_base = st.text_input("FastAPI base URL", value=os.getenv("API_BASE_URL", "http://127.0.0.1:8000")).strip().rstrip("/")
    st.markdown(f"[Open API docs]({api_base}/docs)")
    if st.button("Check API health", use_container_width=True):
        try:
            response = requests.get(f"{api_base}/health", timeout=10)
            response.raise_for_status()
            health = response.json()
            st.success(f"API is running · Latest data: {health.get('last_observation_date', 'unknown')}")
            st.json({k: health.get(k) for k in ("model_loaded", "last_observation_date", "data_staleness_days")})
        except Exception as exc:
            st.error(f"Could not reach API: {exc}")
    st.divider()
    st.caption("Forecasts begin the day after the latest observation. P10–P90 is a nominal 80% prediction interval, not a calibrated confidence score.")

HORIZONS = [7, 14, 30, 60, 90]
STATE_LABELS = {
    "AP": "Andhra Pradesh",
    "KA": "Karnataka",
    "MH": "Maharashtra",
    "MP": "Madhya Pradesh",
}


@st.cache_data(ttl=300, show_spinner=False)
def api_get(url: str, params: tuple[tuple[str, Any], ...]) -> dict:
    response = requests.get(url, params=dict(params), timeout=900)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:1000]}
    if not response.ok:
        raise RuntimeError(body.get("detail", f"API HTTP {response.status_code}"))
    return body


def fetch_graph(endpoint: str, params: dict) -> dict:
    normalized_params = tuple(sorted((k, v) for k, v in params.items()))
    return api_get(f"{api_base}{endpoint}", normalized_params)


def show_freshness(payload: dict) -> None:
    last_date = payload.get("last_observation_date")
    if last_date:
        st.caption(f"Latest actual observation: {last_date} · Forecast: {payload.get('forecast_start_date')} to {payload.get('forecast_end_date')}")
    for warning in payload.get("warnings", []):
        st.warning(warning)


def interval_band(fig: go.Figure, x, lower, upper, name: str, color: str) -> None:
    fig.add_trace(go.Scatter(
        x=x, y=upper, mode="lines", line={"width": 0, "color": color},
        showlegend=False, hoverinfo="skip", name=f"{name} upper bound",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=lower, mode="lines", line={"width": 0, "color": color},
        fill="tonexty", fillcolor=color.replace("1.0", "0.16"),
        name=f"{name} P10–P90 band", hovertemplate="Date: %{x}<br>Lower: ₹%{y:.2f}/kg<extra></extra>",
    ))


def hex_to_rgba(color: str, alpha: float = 0.15) -> str:
    value = color.lstrip("#")
    if len(value) != 6:
        return f"rgba(99, 110, 250, {alpha})"
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha})"


tab1, tab2, tab3 = st.tabs([
    "Graph 1 · AP retail forecast",
    "Graph 2 · State comparison",
    "Graph 3 · Quantity & balance",
])

with tab1:
    st.subheader("Andhra Pradesh retail price forecast")
    c1, c2 = st.columns([1, 2])
    with c1:
        horizon1 = st.selectbox("Forecast horizon", HORIZONS, index=1, key="g1_horizon", format_func=lambda n: f"{n} days")
        history_options = {"1 year (365 days)": 365, "3 years (1,095 days)": 1095, "5 years / all available data": 1826}
        history_label = st.selectbox("Historical window AND model context", list(history_options), index=0, key="g1_history")
        history1 = history_options[history_label]
        selected_ap_types = st.multiselect("AP price series", ["retail", "wholesale"], default=["retail"], format_func=lambda x: "Retail price" if x == "retail" else "Mandi modal price", key="g1_types")
        run1 = st.button("Generate AP forecast", type="primary", key="g1_run")
    with c2:
        st.markdown("Forecast line: **P50** · Shaded band: **P10–P90 nominal 80% prediction interval**")
        st.caption("The selected historical window is used both for the visible history and as Chronos-2's input context. Five years means all available history if the dataset contains fewer than five years.")
    if run1:
        with st.spinner("Running Chronos-2 for AP retail prices. First run may download model weights..."):
            try:
                if not selected_ap_types:
                    raise ValueError("Select at least one AP price series.")
                st.session_state["graph1_payload"] = fetch_graph(
                    "/api/graphs/ap-retail", {"horizon_days": horizon1, "history_days": history1, "price_types": ",".join(selected_ap_types)}
                )
                st.session_state["graph1_error"] = None
            except Exception as exc:
                st.session_state["graph1_error"] = str(exc)
    if st.session_state.get("graph1_error"):
        st.error(st.session_state["graph1_error"])
    payload = st.session_state.get("graph1_payload")
    if payload:
        show_freshness(payload)
        hist = pd.DataFrame(payload.get("historical", []))
        pred = pd.DataFrame(payload.get("forecast", []))
        if not hist.empty and not pred.empty:
            fig = go.Figure()
            colors = {"retail": "#2463A6", "wholesale": "#D97706"}
            labels = {"retail": "Retail price", "wholesale": "Mandi modal price"}
            for price_type in payload.get("selected_price_types", []):
                hline = hist[hist["price_type"] == price_type]
                pline = pred[pred["price_type"] == price_type]
                color = colors[price_type]
                label = labels[price_type]
                if not hline.empty:
                    fig.add_trace(go.Scatter(x=hline["date"], y=hline["actual_price_rs_kg"], mode="lines", name=f"Actual {label.lower()}", line={"color": color, "width": 2.2}))
                if not pline.empty:
                    fig.add_trace(go.Scatter(x=pline["date"], y=pline["p90_rs_kg"], mode="lines", line={"width": 0, "color": color}, showlegend=False, hoverinfo="skip"))
                    fig.add_trace(go.Scatter(x=pline["date"], y=pline["p10_rs_kg"], mode="lines", line={"width": 0, "color": color}, fill="tonexty", fillcolor=hex_to_rgba(color, 0.15), name=f"{label} P10–P90", hovertemplate="Date: %{x}<br>Lower: ₹%{y:.2f}/kg<extra></extra>"))
                    fig.add_trace(go.Scatter(x=pline["date"], y=pline["predicted_price_rs_kg"], mode="lines", name=f"{label} forecast P50", line={"color": color, "width": 2.5, "dash": "dash"}, hovertemplate="Date: %{x}<br>Forecast: ₹%{y:.2f}/kg<extra></extra>"))
            fig.update_layout(height=520, xaxis_title="Date", yaxis_title="Price (₹/kg)", hovermode="x unified", legend={"orientation": "h", "y": -0.2})
            st.plotly_chart(fig, use_container_width=True)
            metric_type = "retail" if "retail" in payload.get("selected_price_types", []) else "wholesale"
            latest = pred[pred["price_type"] == metric_type].iloc[0]
            metric_label = labels[metric_type]
            met1, met2, met3 = st.columns(3)
            met1.metric(f"First {metric_label} forecast (P50)", f"₹{latest['predicted_price_rs_kg']:.2f}/kg")
            met2.metric("P10 lower bound", f"₹{latest['p10_rs_kg']:.2f}/kg")
            met3.metric("P90 upper bound", f"₹{latest['p90_rs_kg']:.2f}/kg")
            st.caption(f"Chronos-2 context: {payload.get('model_context_days')} daily observations available in the selected historical window.")
            with st.expander("Forecast rows"):
                st.dataframe(pred, use_container_width=True)
            st.download_button("Download forecast CSV", pred.to_csv(index=False), file_name=f"ap_price_forecast_{payload['horizon_days']}d.csv", mime="text/csv")

with tab2:
    st.subheader("Selected-state retail and wholesale comparison")
    c1, c2, c3 = st.columns([1.5, 1, 1])
    with c1:
        selected_states = st.multiselect(
            "States to compare", options=list(STATE_LABELS), default=["AP", "MH"],
            format_func=lambda code: f"{code} — {STATE_LABELS[code]}", key="g2_states",
        )
    with c2:
        horizon2 = st.selectbox("Forecast horizon", HORIZONS, index=1, key="g2_horizon", format_func=lambda n: f"{n} days")
    with c3:
        selected_price_types = st.multiselect("Price type", ["retail", "wholesale"], default=["retail", "wholesale"], key="g2_types")
    run2 = st.button("Generate selected-state forecasts", type="primary", key="g2_run")
    if run2:
        if not selected_states or not selected_price_types:
            st.session_state["graph2_error"] = "Select at least one state and one price type."
        else:
            with st.spinner("Running Chronos-2 for the selected state series..."):
                try:
                    st.session_state["graph2_payload"] = fetch_graph(
                        "/api/graphs/state-comparison",
                        {"states": ",".join(selected_states), "price_types": ",".join(selected_price_types), "horizon_days": horizon2, "history_days": 90},
                    )
                    st.session_state["graph2_error"] = None
                except Exception as exc:
                    st.session_state["graph2_error"] = str(exc)
    if st.session_state.get("graph2_error"):
        st.error(st.session_state["graph2_error"])
    payload2 = st.session_state.get("graph2_payload")
    if payload2:
        show_freshness(payload2)
        hdf = pd.DataFrame(payload2.get("historical", []))
        fdf = pd.DataFrame(payload2.get("forecast", []))
        fig2 = go.Figure()
        all_lines = []
        line_index = 0
        palette = qualitative.Plotly
        for state in payload2.get("selected_states", []):
            for price_type in payload2.get("selected_price_types", []):
                label = f"{state} {price_type}"
                color = palette[line_index % len(palette)]
                rgba = hex_to_rgba(color, 0.13)
                hist_line = hdf[(hdf["state"] == state) & (hdf["price_type"] == price_type)] if not hdf.empty else pd.DataFrame()
                pred_line = fdf[(fdf["state"] == state) & (fdf["price_type"] == price_type)] if not fdf.empty else pd.DataFrame()
                if not hist_line.empty:
                    fig2.add_trace(go.Scatter(x=hist_line["date"], y=hist_line["actual_price_rs_kg"], mode="lines", name=f"{label} actual", legendgroup=label, line={"color": color, "width": 2}))
                if not pred_line.empty:
                    fig2.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["p90_rs_kg"], mode="lines", line={"width": 0, "color": color}, showlegend=False, hoverinfo="skip", legendgroup=label))
                    fig2.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["p10_rs_kg"], mode="lines", line={"width": 0, "color": color}, fill="tonexty", fillcolor=rgba, name=f"{label} P10–P90", legendgroup=label, hovertemplate="Date: %{x}<br>Lower bound: ₹%{y:.2f}/kg<extra></extra>"))
                    fig2.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["predicted_price_rs_kg"], mode="lines", name=f"{label} forecast P50", legendgroup=label, line={"color": color, "dash": "dash", "width": 2.5}, hovertemplate="Date: %{x}<br>Forecast: ₹%{y:.2f}/kg<extra></extra>"))
                    all_lines.append(pred_line)
                line_index += 1
        fig2.update_layout(height=560, xaxis_title="Date", yaxis_title="Price (₹/kg)", hovermode="x unified", legend={"orientation": "h", "y": -0.24})
        st.plotly_chart(fig2, use_container_width=True)
        st.caption("Only the states and price types selected above are returned by the API. The dashed lines are forecasts; solid lines are actual historical observations.")
        if not fdf.empty:
            st.download_button("Download comparison forecast CSV", fdf.to_csv(index=False), file_name="selected_state_price_forecasts.csv", mime="text/csv")

with tab3:
    st.subheader("Arrival, production, consumption and indicative balance")
    st.warning("The balance is provisional: arrivals + production − consumption. Confirm data units and ensure arrivals are not counted twice before treating this as a real surplus/shortfall.")
    c1, c2 = st.columns([1.5, 1])
    with c1:
        qty_states = st.multiselect("States", options=list(STATE_LABELS), default=["AP"], format_func=lambda code: f"{code} — {STATE_LABELS[code]}", key="g3_states")
    with c2:
        horizon3 = st.selectbox("Forecast horizon", HORIZONS, index=1, key="g3_horizon", format_func=lambda n: f"{n} days")
    run3 = st.button("Generate quantity & balance forecast", type="primary", key="g3_run")
    if run3:
        if not qty_states:
            st.session_state["graph3_error"] = "Select at least one state."
        else:
            with st.spinner("Running Chronos-2 for arrival, production and consumption series..."):
                try:
                    st.session_state["graph3_payload"] = fetch_graph(
                        "/api/graphs/supply-balance",
                        {"states": ",".join(qty_states), "horizon_days": horizon3, "history_days": 90},
                    )
                    st.session_state["graph3_error"] = None
                except Exception as exc:
                    st.session_state["graph3_error"] = str(exc)
    if st.session_state.get("graph3_error"):
        st.error(st.session_state["graph3_error"])
    payload3 = st.session_state.get("graph3_payload")
    if payload3:
        show_freshness(payload3)
        qhist = pd.DataFrame(payload3.get("historical", []))
        qpred = pd.DataFrame(payload3.get("quantity_forecast", []))
        bpred = pd.DataFrame(payload3.get("balance_forecast", []))
        if not qhist.empty and not qpred.empty:
            fig3 = go.Figure()
            line_index = 0
            palette = qualitative.Plotly
            for state in payload3.get("selected_states", []):
                for metric in ["arrival", "production", "consumption"]:
                    label = f"{state} {metric}"
                    color = palette[line_index % len(palette)]
                    rgba = hex_to_rgba(color, 0.12)
                    hist_line = qhist[(qhist["state"] == state) & (qhist["metric"] == metric)]
                    pred_line = qpred[(qpred["state"] == state) & (qpred["metric"] == metric)]
                    if not hist_line.empty:
                        fig3.add_trace(go.Scatter(x=hist_line["date"], y=hist_line["actual_quantity_t"], mode="lines", name=f"{label} actual", legendgroup=label, line={"color": color, "width": 2}))
                    if not pred_line.empty:
                        fig3.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["p90_t"], mode="lines", line={"width": 0, "color": color}, showlegend=False, hoverinfo="skip", legendgroup=label))
                        fig3.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["p10_t"], mode="lines", line={"width": 0, "color": color}, fill="tonexty", fillcolor=rgba, name=f"{label} P10–P90", legendgroup=label, hovertemplate="Date: %{x}<br>Lower quantity: %{y:.2f} t<extra></extra>"))
                        fig3.add_trace(go.Scatter(x=pred_line["date"], y=pred_line["predicted_quantity_t"], mode="lines", name=f"{label} forecast P50", line={"color": color, "dash": "dash", "width": 2.5}, legendgroup=label))
                    line_index += 1
            fig3.update_layout(height=510, xaxis_title="Date", yaxis_title="Quantity (tonnes; source period requires validation)", hovermode="x unified", legend={"orientation": "h", "y": -0.28})
            st.plotly_chart(fig3, use_container_width=True)
        if not bpred.empty:
            st.markdown("#### Indicative supply-demand balance proxy")
            figb = go.Figure()
            for state in payload3.get("selected_states", []):
                line = bpred[bpred["state"] == state]
                if line.empty:
                    continue
                figb.add_trace(go.Scatter(x=line["date"], y=line["balance_proxy_p90_t"], mode="lines", line={"width": 0}, showlegend=False, hoverinfo="skip"))
                figb.add_trace(go.Scatter(x=line["date"], y=line["balance_proxy_p10_t"], mode="lines", line={"width": 0}, fill="tonexty", name=f"{state} balance P10–P90 envelope"))
                figb.add_trace(go.Scatter(x=line["date"], y=line["balance_proxy_p50_t"], mode="lines+markers", name=f"{state} balance proxy P50"))
            figb.add_hline(y=0, line_dash="dash", line_color="gray", annotation_text="0 = balance threshold")
            figb.update_layout(height=420, xaxis_title="Date", yaxis_title="Balance proxy (tonnes; units to validate)", hovermode="x unified", legend={"orientation": "h", "y": -0.25})
            st.plotly_chart(figb, use_container_width=True)
            st.dataframe(bpred, use_container_width=True)
            with st.expander("Calculation warnings / assumptions"):
                for warning in payload3.get("warnings", []):
                    st.write(f"- {warning}")
            st.download_button("Download quantity and balance forecast CSV", qpred.to_csv(index=False), file_name="quantity_forecasts.csv", mime="text/csv")

st.divider()
st.caption("MVP note: this version runs forecasts from the currently stored history. The UPAg source-discovery utility is separate until authorized source access and its data schema are confirmed.")
