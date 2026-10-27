# Implementation decisions

1. Use the supplied combined daily CSV first. It contains all four state series and is enough to wire Graphs 1–3 and exercise Chronos-2 locally.
2. Store canonical history in SQLite for a no-setup local MVP. The forecast endpoints always reload the current DB history, so an imported update is visible without restarting FastAPI.
3. Use timestamp-keyed upserts for future full model-ready CSV refreshes. Raw UPAg source JSON is not directly upserted because the exact source schema, state granularity, and unit mapping have to be verified first.
4. The UPAg client only performs documented authentication, allowed-source discovery, and configurable raw retrieval. It deliberately does not claim that the data feed is fully integrated until source access and response schema are verified.
5. Chronos-2 is loaded once per API process. Each series returns P10/P50/P90. There is no native scalar confidence score.
6. Graph 3 forecasts quantities through the same Chronos-2 service. Its balance proxy is explicitly labeled and warned; the business formula needs owner validation before being called a real surplus/shortfall.
7. Weather enrichment, event-specific El Niño/cyclone scenarios, calibration/backtesting, and a fully automatic source-specific UPAg poller follow once source access and business definitions are confirmed.
