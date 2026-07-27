"""Indicadores técnicos calculados (NO estimados a ojo) para el bloque {indicadores} del prompt L3.

Regla dura (Felipe, 2026-07-04): ADX/MACD se COMPUTAN, no se aproximan visualmente desde las velas.
Este módulo es la única fuente de verdad — lo usan tanto las corridas manuales como, cuando se
relance el daemon, signal_generator.py para poblar el placeholder {indicadores}.

- ADX(14) con suavizado de Wilder (RMA sembrada con SMA), +DI / -DI incluidos.
- MACD(12,26,9): línea, señal y histograma con EMA (adjust=False, convención estándar).

Sin dependencias de librerías TA (no hay ta/pandas_ta/talib en el entorno): solo pandas/numpy.
"""
from __future__ import annotations

import io
import pandas as pd


def _wilder_rma(serie: pd.Series, periodo: int) -> pd.Series:
    """Suavizado de Wilder (Running Moving Average): primer valor = SMA de `periodo` muestras,
    luego RMA_t = (RMA_{t-1}*(periodo-1) + valor_t) / periodo. Es lo que usan TradingView/ta
    para ADX/DI — distinto de una EMA simple y distinto de una SMA."""
    rma = serie.copy().astype(float)
    rma.iloc[:] = pd.NA
    if len(serie) < periodo:
        return rma
    # Semilla: SMA de las primeras `periodo` observaciones
    semilla = serie.iloc[:periodo].mean()
    rma.iloc[periodo - 1] = semilla
    factor = periodo - 1
    for i in range(periodo, len(serie)):
        rma.iloc[i] = (rma.iloc[i - 1] * factor + serie.iloc[i]) / periodo
    return rma


def compute_adx(df: pd.DataFrame, periodo: int = 14) -> pd.DataFrame:
    """Devuelve DataFrame con columnas ADX, +DI, -DI (mismo índice que df).
    df debe tener columnas High, Low, Close."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Movimiento direccional
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0)

    atr = _wilder_rma(tr, periodo)
    plus_di = 100 * _wilder_rma(plus_dm, periodo) / atr
    minus_di = 100 * _wilder_rma(minus_dm, periodo) / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
    adx = _wilder_rma(dx, periodo)

    return pd.DataFrame({"ADX": adx, "+DI": plus_di, "-DI": minus_di}, index=df.index)


def compute_macd(df: pd.DataFrame, rapida: int = 12, lenta: int = 26, senal: int = 9) -> pd.DataFrame:
    """Devuelve DataFrame con MACD, señal, histograma. df debe tener columna Close."""
    close = df["Close"]
    ema_rapida = close.ewm(span=rapida, adjust=False).mean()
    ema_lenta = close.ewm(span=lenta, adjust=False).mean()
    macd = ema_rapida - ema_lenta
    linea_senal = macd.ewm(span=senal, adjust=False).mean()
    return pd.DataFrame({
        "MACD": macd,
        "señal": linea_senal,
        "histograma": macd - linea_senal,
    }, index=df.index)


def compute_rsi(df: pd.DataFrame, periodo: int = 14) -> pd.Series:
    """RSI de Wilder (14 por defecto), el que usa Santos en las monografías.
    Usa el MISMO suavizado RMA que el ADX — no una EMA ni una SMA, que darían valores distintos.
    Niveles de referencia de Santos: >70 sobrecompra, <30 sobreventa. OJO (Monografía 2, p.38):
    esos niveles NO son señal de giro por sí solos; solo la DIVERGENCIA lo es."""
    delta = df["Close"].diff()
    ganancia = delta.clip(lower=0)
    perdida = (-delta).clip(lower=0)
    avg_g = _wilder_rma(ganancia, periodo)
    avg_p = _wilder_rma(perdida, periodo)
    rs = avg_g / avg_p.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    # perdida media 0 => RSI 100 (subida ininterrumpida); ganancia media 0 => RSI 0
    rsi = rsi.where(avg_p != 0, 100.0).where(~((avg_g == 0) & (avg_p != 0)), 0.0)
    return rsi.rename("RSI14")


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """ADX(14)+DI/-DI + MACD(12,26,9) + RSI(14) Wilder, alineados al índice de df."""
    adx = compute_adx(df, 14)
    macd = compute_macd(df, 12, 26, 9)
    rsi = compute_rsi(df, 14)
    return pd.concat([adx, macd, rsi], axis=1)


def _parse_price_csv(price_csv: str) -> pd.DataFrame:
    """Parsea el CSV que produce signal_generator.fetch_price_data (índice datetime, OHLCV)."""
    df = pd.read_csv(io.StringIO(price_csv), index_col=0)
    df.index.name = "Fecha"
    return df


def indicadores_block(price_csv: str, n: int = 40) -> str:
    """Bloque de texto para el placeholder {indicadores} del prompt L3.
    Toma el MISMO CSV de precios que ya se inyecta en {price_data}, calcula los indicadores sobre la
    serie COMPLETA (para que el suavizado de Wilder tenga historia) y devuelve las últimas `n` filas.
    Formato: Fecha,ADX14,+DI,-DI,MACD,señal,histograma,RSI14 — coincide con el encabezado del prompt.
    """
    df = _parse_price_csv(price_csv)
    faltan = [c for c in ("High", "Low", "Close") if c not in df.columns]
    if faltan:
        return "NO_DISPONIBLE"
    ind = compute_indicators(df).tail(n).round(2)
    ind.columns = ["ADX14", "+DI", "-DI", "MACD", "señal", "histograma", "RSI14"]
    return ind.to_csv()


if __name__ == "__main__":
    # Uso manual: python3 src/indicadores.py [TICKER] [asof YYYY-MM-DD] [candles]
    # Fetch 4h resampleado (misma lógica que signal_generator) y muestra el bloque {indicadores}.
    import sys
    import yfinance as yf

    ticker = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
    asof = sys.argv[2] if len(sys.argv) > 2 else None
    candles = int(sys.argv[3]) if len(sys.argv) > 3 else 360

    raw = yf.Ticker(ticker).history(period="730d", interval="1h")
    raw.index = raw.index.tz_localize(None) if raw.index.tz else raw.index
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df4 = raw.resample("4h").agg(agg).dropna()
    if asof:
        df4 = df4[df4.index < pd.Timestamp(asof) + pd.Timedelta(days=1)]
    df4 = df4.tail(candles)
    df4.index = df4.index.strftime("%Y-%m-%d %H:%M")
    print(indicadores_block(df4.to_csv(), n=40))
