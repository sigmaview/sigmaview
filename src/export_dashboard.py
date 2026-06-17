"""Exporta datos de sigmaview.db a JSON para el dashboard de GitHub Pages.
Corre al final del workflow diario.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import database

DB_PATH  = database.DB_PATH
OUT_DIR  = Path(__file__).parent.parent / "docs" / "data"


def _conn():
    return sqlite3.connect(DB_PATH)


def export():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = _conn()

    # ── Análisis diario ────────────────────────────────────────────────────────
    analysis = pd.read_sql(
        "SELECT * FROM analysis ORDER BY fecha DESC LIMIT 200", conn
    )

    # ── Señales ────────────────────────────────────────────────────────────────
    signals = pd.read_sql(
        "SELECT * FROM signals ORDER BY fecha DESC", conn
    )

    # ── Alertas disparadas ─────────────────────────────────────────────────────
    alerts = pd.read_sql(
        "SELECT * FROM alerts_fired ORDER BY fired_at DESC LIMIT 50", conn
    )

    # ── Precios diarios (últimos 180d desde ohlcv 1h) ─────────────────────────
    prices_1h = pd.read_sql(
        "SELECT ts, open, high, low, close FROM ohlcv WHERE ticker='BTC-USD' ORDER BY ts DESC LIMIT 4320",
        conn, parse_dates=["ts"],
    )
    prices_1h = prices_1h.set_index("ts").sort_index()
    prices_daily = prices_1h.resample("D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    prices_daily["ts"] = prices_daily["ts"].dt.strftime("%Y-%m-%d")

    conn.close()

    # ── Summary stats ──────────────────────────────────────────────────────────
    n_analysis  = len(analysis)
    n_señales   = len(signals)
    n_cerrados  = len(signals[signals["estado"] != "ABIERTO"])
    n_ganadores = len(signals[signals["resultado_r"].notna() & (signals["resultado_r"] > 0)])
    total_r     = float(signals["resultado_r"].dropna().sum())
    win_rate    = round(n_ganadores / n_cerrados * 100, 1) if n_cerrados > 0 else None

    ultimo = analysis.iloc[0] if n_analysis > 0 else {}
    summary = {
        "total_analisis":   n_analysis,
        "total_señales":    n_señales,
        "win_rate":         win_rate,
        "total_r":          round(total_r, 2),
        "r_por_trade":      round(total_r / n_señales, 2) if n_señales > 0 else None,
        "ultimo_veredicto": ultimo.get("veredicto"),
        "ultima_fecha":     ultimo.get("fecha"),
        "ultimo_precio":    ultimo.get("precio_actual"),
        "ultimo_sesgo":     ultimo.get("sesgo_macro"),
        "ultima_fase":      ultimo.get("fase_impulso"),
    }

    # ── Escribir JSON ──────────────────────────────────────────────────────────
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, default=str)
    )
    (OUT_DIR / "analysis.json").write_text(
        analysis.to_json(orient="records", date_format="iso", force_ascii=False)
    )
    (OUT_DIR / "signals.json").write_text(
        signals.to_json(orient="records", date_format="iso", force_ascii=False)
    )
    (OUT_DIR / "alerts.json").write_text(
        alerts.to_json(orient="records", date_format="iso", force_ascii=False)
    )
    (OUT_DIR / "prices.json").write_text(
        prices_daily.to_json(orient="records", force_ascii=False)
    )

    print(f"Dashboard exportado: {n_analysis} análisis | {n_señales} señales | "
          f"{len(prices_daily)} días de precio")


if __name__ == "__main__":
    export()
