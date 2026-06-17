"""Recolecta datos de precio y los guarda en sigmaview.db.

Uso normal (corre en el workflow diario):
    python3 src/data_collector.py

Backfill inicial (una sola vez, llena la historia disponible en yfinance):
    python3 src/data_collector.py --backfill
"""
import sys
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
import database

TICKERS = ["BTC-USD"]


def collect_recent(ticker: str, days: int = 7) -> int:
    df = yf.Ticker(ticker).history(period=f"{days}d", interval="1h")
    if df.empty:
        return 0
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    return database.upsert_ohlcv(ticker, df)


def backfill(ticker: str) -> int:
    """Llena hasta 730 días de candles 1h (límite de yfinance para 1h)."""
    print(f"  Descargando 730d × 1h para {ticker}...", end=" ", flush=True)
    df = yf.Ticker(ticker).history(period="730d", interval="1h")
    if df.empty:
        print("sin datos")
        return 0
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    n = database.upsert_ohlcv(ticker, df)
    print(f"{n} candles nuevos ({len(df)} descargados)")
    return n


if __name__ == "__main__":
    mode = "backfill" if "--backfill" in sys.argv else "recent"
    for ticker in TICKERS:
        if mode == "backfill":
            backfill(ticker)
        else:
            n = collect_recent(ticker, days=7)
            print(f"  {ticker}: {n} nuevos candles 1h guardados en DB")
