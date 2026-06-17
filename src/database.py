"""Base de datos SQLite para SigmaView.
Acumula historia de precios (1h candles) y resultados del pipeline.
"""
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "sigmaview.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT NOT NULL,
            ts     TEXT NOT NULL,
            open   REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (ticker, ts)
        );
        CREATE TABLE IF NOT EXISTS analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha           TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            precio_actual   REAL,
            veredicto       TEXT,
            modo_entrada    TEXT,
            sub_onda        TEXT,
            sesgo_macro     TEXT,
            fase_impulso    TEXT,
            calidad_senal   TEXT,
            l2_alerta       TEXT,
            modelo          TEXT,
            UNIQUE(fecha, ticker)
        );
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha       TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            modo        TEXT,
            direccion   TEXT,
            calidad     TEXT,
            entrada     REAL,
            stop        REAL,
            o1 REAL, o2 REAL, o3 REAL,
            rr_o1 REAL, rr_o2 REAL, rr_o3 REAL,
            resultado_r REAL,
            estado      TEXT DEFAULT 'ABIERTO',
            UNIQUE(fecha, ticker)
        );
        CREATE TABLE IF NOT EXISTS alerts_fired (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at      TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            alert_id      TEXT,
            tipo          TEXT,
            nivel         REAL,
            precio_actual REAL,
            mensaje       TEXT
        );
        """)


def upsert_ohlcv(ticker: str, df: pd.DataFrame) -> int:
    """Inserta candles 1h en la DB (INSERT OR IGNORE). Retorna n filas nuevas."""
    init_db()
    rows = []
    for ts, row in df.iterrows():
        ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
        rows.append((
            ticker, ts_str,
            float(row.get("Open") or 0),
            float(row.get("High") or 0),
            float(row.get("Low") or 0),
            float(row.get("Close") or 0),
            float(row.get("Volume") or 0),
        ))
    inserted = 0
    with _conn() as conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                "INSERT OR IGNORE INTO ohlcv (ticker,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                r,
            )
            inserted += cur.rowcount
    return inserted


def fetch_4h_from_db(ticker: str, n_candles: int, asof: str | None = None) -> str | None:
    """Devuelve CSV de velas 4h resampleadas desde la DB. None si no hay suficientes datos."""
    init_db()
    if asof:
        query = ("SELECT ts,open,high,low,close,volume FROM ohlcv "
                 "WHERE ticker=? AND ts < ? ORDER BY ts")
        params = [ticker, f"{asof} 23:59"]
    else:
        query = "SELECT ts,open,high,low,close,volume FROM ohlcv WHERE ticker=? ORDER BY ts"
        params = [ticker]

    with _conn() as c:
        df = pd.read_sql_query(query, c, params=params, parse_dates=["ts"], index_col="ts")

    if df.empty or len(df) < n_candles * 4:
        return None

    df.columns = [col.capitalize() for col in df.columns]
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df4 = df.resample("4h").agg(agg).dropna().tail(n_candles)
    df4.index = df4.index.strftime("%Y-%m-%d %H:%M")
    return df4.to_csv()


def log_analysis(fecha: str, ticker: str, result: dict, l2_alerta: str = "") -> None:
    init_db()
    le = result.get("lectura_estructural", {})
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO analysis
            (fecha, ticker, precio_actual, veredicto, modo_entrada, sub_onda,
             sesgo_macro, fase_impulso, calidad_senal, l2_alerta, modelo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fecha, ticker,
            result.get("precio_actual"),
            result.get("veredicto"),
            result.get("modo_entrada"),
            le.get("sub_onda_actual"),
            result.get("sesgo_macro"),
            result.get("fase_impulso_macro"),
            result.get("calidad_señal"),
            l2_alerta,
            result.get("_meta", {}).get("modelo"),
        ))


def log_signal(fecha: str, ticker: str, result: dict) -> None:
    pt = result.get("plan_trade")
    if not pt:
        return
    entry = pt.get("entrada")
    stop = pt.get("stop")
    risk = abs(entry - stop) if entry and stop else None

    def rr(target):
        if risk and risk > 0 and target:
            return round(abs(target - entry) / risk, 2)
        return None

    init_db()
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO signals
            (fecha, ticker, modo, direccion, calidad, entrada, stop,
             o1, o2, o3, rr_o1, rr_o2, rr_o3)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fecha, ticker,
            result.get("modo_entrada"),
            result.get("direccion"),
            result.get("calidad_señal"),
            entry, stop,
            pt.get("O1"), pt.get("O2"), pt.get("O3"),
            rr(pt.get("O1")), rr(pt.get("O2")), rr(pt.get("O3")),
        ))


def log_alert_fired(ticker: str, alert: dict, precio: float) -> None:
    init_db()
    with _conn() as c:
        c.execute("""
            INSERT INTO alerts_fired (fired_at, ticker, alert_id, tipo, nivel, precio_actual, mensaje)
            VALUES (?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            ticker,
            alert.get("id"),
            alert.get("tipo"),
            alert.get("nivel"),
            precio,
            alert.get("mensaje"),
        ))
