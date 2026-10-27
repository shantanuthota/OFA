# Onion Forecasting MVP — Chronos-2 + FastAPI + Streamlit

This project is a local, runnable MVP built around the supplied combined CSV for Andhra Pradesh (AP), Karnataka (KA), Maharashtra (MH), and Madhya Pradesh (MP).

## What is included

- **Graph 1 API/UI:** AP retail and/or mandi-modal price forecasts for 7, 14, 30, 60, or 90 days, with P50 and P10–P90 prediction bounds. The Graph 1 historical-window selector also controls the actual Chronos-2 context: 1 year, 3 years, or all available data up to a nominal 5-year window.
- **Graph 2 API/UI:** Select any subset of states and compare retail and wholesale/mandi-modal prices. Only selected states and price types are returned.
- **Graph 3 API/UI:** Chronos-2 forecasts arrivals, production, and consumption; calculates the explicitly provisional `arrival + production - consumption` balance proxy.
- **SQLite history store:** Seeds itself from the model-ready CSV on first start. Subsequent full model-ready CSV imports use timestamp-based upserts (insert new dates, update corrected dates, skip unchanged rows).
- **UPAg discovery utility:** Uses credentials from local `.env`, logs into the official UPAg Open API, lists the source names permitted for that account, and can save raw JSON responses when a source-specific request payload is provided.
- **Mocked API tests:** Check request/response structure without downloading Chronos-2 model weights.

## Important honesty/limitations

1. Graph 1 supports retail-only, mandi-modal-only, or both price series. Its selected historical window is used both for chart history and the Chronos-2 input context. The 5-year option uses all available rows if fewer than five years exist. The actual Chronos-2 model has to be installed and its weights downloaded on your machine. The mocked tests do **not** claim real inference was executed here.
2. The supplied model-ready CSV ends on **2026-10-15**. The forecast horizon selector now supports 7, 14, 30, 60 and 90 days. Forecasts start on **2026-10-16**, the day after the last observation. The API shows a freshness warning because that history is not current as of 2026-10-27.
3. The supplied CSV does not contain weather. This MVP does not yet claim to forecast under El Niño/cyclone scenarios.
4. The production and consumption fields repeat for long daily runs. Validate their real reporting frequency and units before interpreting daily forecasts.
5. The Graph 3 balance calculation is provisional. If arrivals form part of the same production flow, `arrival + production - consumption` may double-count supply. Its bounds are a conservative combination of individual quantiles, not a calibrated joint interval.
6. UPAg credentials alone do not prove that a source provides the fields required by the model. First list the sources authorized to the account and inspect the exact response schema. The included UPAg utility fetches **raw data only**; it does not silently map unknown source columns into the model-ready dataset.

## Recommended environment

Use **64-bit Python 3.11** for the MVP. The first real forecast requires internet access to install dependencies and download the public Chronos-2 model weights. CPU inference works but can be slower than a supported CUDA GPU.

## Windows / Antigravity IDE setup

1. Extract the ZIP into a separate working folder, for example `C:\work\onion_forecasting_mvp`, and open that folder in Antigravity IDE.
2. In Antigravity's terminal, run these PowerShell commands from the project root:

```powershell
Copy-Item .env.example .env
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, use Command Prompt and run `.venv\Scripts\activate.bat`, or execute `.venv\Scripts\python.exe` directly.

3. Start the API in terminal 1:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

4. Open `http://127.0.0.1:8000/health` to verify the API and data store. Then open `http://127.0.0.1:8000/docs`.
5. Start the dashboard in terminal 2:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

6. Open the local Streamlit address printed by the terminal, usually `http://localhost:8501`.

**The first Graph 1/2/3 request loads Chronos-2 weights and may take noticeably longer.** Do not repeatedly click Generate while the first model load is running.

## Direct API checks

```text
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=7
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=14
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=30
GET http://127.0.0.1:8000/api/graphs/ap-retail?horizon_days=60

GET http://127.0.0.1:8000/api/graphs/state-comparison?states=AP,MH&price_types=retail,wholesale&horizon_days=14
GET http://127.0.0.1:8000/api/graphs/state-comparison?states=KA,MP&price_types=retail,wholesale&horizon_days=30

GET http://127.0.0.1:8000/api/graphs/supply-balance?states=AP&horizon_days=7
GET http://127.0.0.1:8000/api/ingestion/status
```

## History store and CSV updates

The first API start seeds `data/onion_history.sqlite3` from `data/Onion_Model_Ready_Dataset_Chronos2.csv`. The database is the live read path after seeding. The SQLite file is ignored by Git and is created locally.

If you receive a newer **full combined model-ready** CSV, put it inside `data/` and import it:

```powershell
.\.venv\Scripts\python.exe sync_model_ready_csv.py data\Onion_Model_Ready_Dataset_Chronos2_updated.csv
```

It is an upsert keyed by date; it will not duplicate dates. Supply the full aligned history, not a file containing only the latest row. A row must contain the expected AP/KA/MH/MP fields so the combined date can be validated. After the import, the API reads the new database history and forecast cache keys change automatically.

There is also a secured local admin endpoint for a CSV already placed inside `data/`:

1. Change `INGESTION_API_KEY` in `.env` to a long, unique secret.
2. Place the normalized combined CSV under `data/`.
3. Call `POST /api/ingestion/sync-csv?filename=your_file.csv` with request header `X-Ingestion-Key: <your-secret>`.

Do not expose the local ingestion endpoint to the public internet.

## UPAg Open API discovery

Official docs: https://data.upag.gov.in/docs and https://data.upag.gov.in/redoc

The documented API uses a login at `/v1/upag/api-data-share/login`, then lists account-authorized data sources at `/v1/upag/api-data-share/sources/user-allowed-sources`, and fetches a source with `POST /v1/upag/api-data-share/sources/{source_name}`. Source request objects differ by source.

1. Put the account's authorized credentials in `.env` (`UPAG_USERNAME`, `UPAG_PASSWORD`, plus client ID/secret only if your account documentation requires them). Do not share passwords, tokens, cookies, or `.env`.
2. Run:

```powershell
.\.venv\Scripts\python.exe upag_client.py --list-sources
```

3. Confirm whether `agmarknet` and/or `doca` appear in `allowedSources`.
4. To retrieve one source, create a JSON payload using the exact source-specific request schema from the official docs and run:

```powershell
.\.venv\Scripts\python.exe upag_client.py --source agmarknet --payload-file config\agmarknet_request.json
```

The raw response will be saved under `data/upag_raw_agmarknet.json`. The payload cannot be guessed: confirm field names/filters from Swagger and examine the response keys.

Before turning polling on, the next implementation step is a source adapter that maps the real UPAg response into the canonical daily dataset (date × state, retail price, mandi price, arrivals, and validated quantity series). Retail and mandi/arrival data may come from different sources and different geographic granularities; do not merge them by date alone until the state, unit, crop and market dimensions are verified. Share only the **source names, redacted request payload, and a few redacted response rows** so the adapter can be tailored. Never share account credentials.

## Chronos-2 output interpretation

- P50 is the median forecast.
- P10–P90 is a nominal 80% prediction interval.
- `interval_width_pct_of_median` is a forecast spread indicator, **not a calibrated confidence score**.
- A reliability score requires rolling-origin backtests with actual held-out values for each horizon/state/price type.

## Run tests without model downloads

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

These tests monkeypatch Chronos-2 to verify the three API response shapes. To confirm real forecasting, run the service and request one graph endpoint with the actual model installed.
