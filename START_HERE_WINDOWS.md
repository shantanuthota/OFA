# Start here (Windows + Antigravity)

Do not begin with UPAg credentials. First start the local API and confirm Graphs 1–3 work on the supplied CSV; the UPAg connection can then be inspected in a second terminal while keeping the demo moving.

## Terminal 1 — install and start API

```powershell
Copy-Item .env.example .env
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open:
- API health: http://127.0.0.1:8000/health
- Swagger: http://127.0.0.1:8000/docs

Try Graph 1 first because it forecasts only one series:
`/api/graphs/ap-retail?horizon_days=7`

## Terminal 2 — start dashboard

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

Open the URL printed by Streamlit (normally http://localhost:8501).

## Then inspect UPAg access

Never send credentials to chat. Enter them only into `.env` on your machine, then run:

```powershell
.\.venv\Scripts\python.exe upag_client.py --list-sources
```

Share the returned `allowedSources` names and, after choosing the source, a response sample with personal/authentication information removed. The source-to-model adapter depends on the real response field names, so don't invent them.

## If something fails

Copy the full terminal traceback and the endpoint URL/status (redact tokens/passwords/cookies). Do not share `.env`, browser cookies, Authorization headers, or access tokens.


## Recent Graph 1 controls

Graph 1 supports Retail price, Mandi modal price, or both. Historical window choices are 1 year (365 days), 3 years (1,095 days), and a 5-year maximum (1,826 days); this selection controls both the visible historical chart and the rows passed as Chronos-2 context. Forecast horizons are 7, 14, 30, 60, and 90 days. The uploaded dataset currently contains less than five full years, so the 5-year option means all available history.
