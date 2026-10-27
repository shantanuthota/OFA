"""SQLite history store for the onion forecasting MVP.

The model-ready CSV seeds an empty database once. Later imports are upserted by
observation date so repeated rows do not inflate the time series. This is a local
MVP store; it can later be migrated to PostgreSQL without changing forecast logic.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DEFAULT_CSV = ROOT / "data" / "Onion_Model_Ready_Dataset_Chronos2.csv"

STATE_CODES = ("AP", "KA", "MH", "MP")
STATE_NAMES = {
    "AP": "Andhra Pradesh",
    "KA": "Karnataka",
    "MH": "Maharashtra",
    "MP": "Madhya Pradesh",
}
PRICE_COLUMNS = {
    state: {
        "retail": f"{state}_retail_rs_kg",
        "wholesale": f"{state}_mandi_modal_rs_kg",
    }
    for state in STATE_CODES
}
QUANTITY_COLUMNS = {
    state: {
        "arrival": f"{state}_arrival_t",
        "production": f"{state}_production_t_day",
        "consumption": f"{state}_consumption_t_day",
    }
    for state in STATE_CODES
}
EXPECTED_COLUMNS = [
    "item_id", "timestamp",
    *[PRICE_COLUMNS[s][t] for s in STATE_CODES for t in ("retail", "wholesale")],
    *[QUANTITY_COLUMNS[s][t] for s in STATE_CODES for t in ("arrival", "production", "consumption")],
]
NUMERIC_COLUMNS = [c for c in EXPECTED_COLUMNS if c not in ("item_id", "timestamp")]


def _resolve_path(env_name: str, fallback: Path) -> Path:
    raw = os.getenv(env_name)
    if not raw:
        return fallback
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


CSV_PATH = _resolve_path("ONION_DATA_PATH", DEFAULT_CSV)
DB_PATH = _resolve_path("ONION_DB_PATH", ROOT / "data" / "onion_history.sqlite3")


@dataclass
class SyncResult:
    source: str
    rows_seen: int
    inserted: int
    updated: int
    unchanged: int
    latest_observation_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "rows_seen": self.rows_seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "latest_observation_date": self.latest_observation_date,
        }


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    columns_sql = [
        '"timestamp" TEXT PRIMARY KEY',
        '"item_id" TEXT NOT NULL',
        *[f'"{c}" REAL NOT NULL' for c in NUMERIC_COLUMNS],
        '"source" TEXT NOT NULL',
        '"ingested_at" TEXT NOT NULL',
    ]
    conn.execute(f"CREATE TABLE IF NOT EXISTS observations ({', '.join(columns_sql)})")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            latest_observation_date TEXT,
            message TEXT
        )"""
    )


def validate_model_ready_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a combined, aligned, daily, model-ready four-state CSV."""
    missing = [c for c in EXPECTED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(
            "Input must be the combined model-ready dataset. Missing columns: " + ", ".join(missing)
        )
    out = frame[EXPECTED_COLUMNS].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    if out["timestamp"].isna().any():
        raise ValueError("Input contains invalid or blank timestamps.")
    out["timestamp"] = out["timestamp"].dt.normalize()
    if out["timestamp"].duplicated().any():
        examples = out.loc[out["timestamp"].duplicated(keep=False), "timestamp"].astype(str).head(4).tolist()
        raise ValueError(f"Input contains duplicate dates, e.g. {examples}. Aggregate or deduplicate intentionally first.")
    for c in NUMERIC_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    bad_cols = [c for c in NUMERIC_COLUMNS if out[c].isna().any()]
    if bad_cols:
        raise ValueError("Input has missing/non-numeric values in: " + ", ".join(bad_cols))
    if not out["item_id"].astype(str).nunique() == 1:
        raise ValueError("Combined dataset must contain one item_id and one aligned row per date.")
    out["item_id"] = out["item_id"].astype(str)
    out = out.sort_values("timestamp").reset_index(drop=True)
    expected_dates = pd.date_range(out["timestamp"].min(), out["timestamp"].max(), freq="D")
    if not pd.DatetimeIndex(out["timestamp"]).equals(expected_dates):
        raise ValueError(
            "The combined model-ready CSV must have a continuous daily date index. "
            "Do not fill missing observations with fabricated values; reconcile the source or use a documented missing-data policy."
        )
    return out


def _log_sync(conn: sqlite3.Connection, result: SyncResult, status: str = "success", message: str | None = None) -> None:
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO ingestion_log
        (checked_at, source, status, rows_seen, inserted, updated, unchanged, latest_observation_date, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (checked, result.source, status, result.rows_seen, result.inserted, result.updated,
         result.unchanged, result.latest_observation_date, message),
    )


def _frame_to_db_tuple(row: pd.Series, source: str, ingested_at: str) -> tuple[Any, ...]:
    timestamp = pd.Timestamp(row["timestamp"]).strftime("%Y-%m-%d")
    return (timestamp, str(row["item_id"]), *[float(row[c]) for c in NUMERIC_COLUMNS], source, ingested_at)


def upsert_frame(frame: pd.DataFrame, source: str = "model_ready_csv") -> SyncResult:
    out = validate_model_ready_frame(frame)
    inserted = updated = unchanged = 0
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert_columns = ["timestamp", "item_id", *NUMERIC_COLUMNS, "source", "ingested_at"]
    placeholders = ", ".join("?" for _ in insert_columns)
    update_sql = ", ".join(
        [f'"{c}" = excluded."{c}"' for c in ["item_id", *NUMERIC_COLUMNS, "source", "ingested_at"]]
    )
    quoted_insert_columns = ", ".join('"' + c + '"' for c in insert_columns)
    compare_columns = ["item_id", *NUMERIC_COLUMNS]
    quoted_compare_columns = ", ".join('"' + c + '"' for c in compare_columns)
    sql = (
        f"INSERT INTO observations ({quoted_insert_columns}) "
        f"VALUES ({placeholders}) ON CONFLICT(timestamp) DO UPDATE SET {update_sql}"
    )

    with connect() as conn:
        _create_schema(conn)
        try:
            for _, row in out.iterrows():
                values = _frame_to_db_tuple(row, source, ingested_at)
                key = values[0]
                existing = conn.execute(
                    f"SELECT {quoted_compare_columns} FROM observations WHERE timestamp = ?",
                    (key,),
                ).fetchone()
                if existing is None:
                    inserted += 1
                    conn.execute(sql, values)
                else:
                    new_values = values[1:2 + len(NUMERIC_COLUMNS)]
                    old_values = tuple(existing[c] for c in ["item_id", *NUMERIC_COLUMNS])
                    if all(abs(float(a) - float(b)) < 1e-9 for a, b in zip(new_values[1:], old_values[1:])) and new_values[0] == old_values[0]:
                        unchanged += 1
                    else:
                        updated += 1
                        conn.execute(sql, values)
            latest = out["timestamp"].max().strftime("%Y-%m-%d") if not out.empty else None
            result = SyncResult(source, len(out), inserted, updated, unchanged, latest)
            _log_sync(conn, result)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed = SyncResult(source, len(out), inserted, updated, unchanged,
                                out["timestamp"].max().strftime("%Y-%m-%d") if not out.empty else None)
            _log_sync(conn, failed, status="error", message=str(exc))
            conn.commit()
            raise
    return result


def sync_model_ready_csv(path: str | Path, source: str = "model_ready_csv") -> SyncResult:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")
    frame = pd.read_csv(path)
    return upsert_frame(frame, source=source)


def ensure_store_ready() -> None:
    with connect() as conn:
        _create_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
    if count == 0:
        if not CSV_PATH.exists():
            raise FileNotFoundError(
                f"No historical database records and seed CSV not found at {CSV_PATH}. "
                "Place the supplied combined CSV in data/ or set ONION_DATA_PATH."
            )
        frame = pd.read_csv(CSV_PATH)
        result = upsert_frame(frame, source="initial_csv_seed")
        print(f"Initialized SQLite history from {CSV_PATH}: {result.as_dict()}")


def load_history() -> pd.DataFrame:
    ensure_store_ready()
    quoted_expected_columns = ", ".join('"' + c + '"' for c in EXPECTED_COLUMNS)
    with connect() as conn:
        frame = pd.read_sql_query(
            f"SELECT {quoted_expected_columns} FROM observations ORDER BY timestamp",
            conn,
        )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def ingestion_status() -> dict[str, Any]:
    ensure_store_ready()
    with connect() as conn:
        latest_log = conn.execute("SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 1").fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
        first_last = conn.execute("SELECT MIN(timestamp) AS first_date, MAX(timestamp) AS last_date FROM observations").fetchone()
    return {
        "database_path": str(DB_PATH),
        "observation_rows": int(count),
        "first_observation_date": first_last["first_date"],
        "last_observation_date": first_last["last_date"],
        "last_ingestion": dict(latest_log) if latest_log else None,
    }
