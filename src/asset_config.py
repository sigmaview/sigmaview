"""Configuración por ticker — única fuente de verdad para parámetros multi-asset.
Agregar un activo nuevo = agregar una entrada aquí, sin tocar lógica de pipeline."""

ASSET_CONFIG = {
    "BTC-USD": {
        "asset_label": "BTC/USD",
        "ticker_slug": "btc",
        "is_24h": True,
        "l1_start_date": "2014-01-01",
        "l3_candle_mode": "4h_resample",   # comportamiento actual, sin cambios
        "l3_candle_count": 360,
        "l2_candle_count": 180,
        "fill_sim_dias": 400,
    },
    "^GSPC": {
        "asset_label": "S&P 500",
        "ticker_slug": "spx",
        "is_24h": False,
        # post Black Monday (oct-1987): captura dot-com + GFC completos como ciclos de grado
        # mayor, sin arrastrar regímenes de mercado pre-flotación libre de décadas anteriores.
        "l1_start_date": "1990-01-01",
        # mercado abre ~6.5h/día — resample a 4h con bloques alineados a UTC quedaría mayormente
        # vacío, así que L3 usa velas 1h nativas directamente (sin resample).
        "l3_candle_mode": "1h_native",
        "l3_candle_count": 280,            # ≈43 días hábiles ≈ 61 días calendario (comparable a BTC)
        "l2_candle_count": 180,
        "fill_sim_dias": 286,              # ≈400 días calendario en barras hábiles (400/7×5)
    },
}


def get_config(ticker: str) -> dict:
    if ticker not in ASSET_CONFIG:
        raise ValueError(f"Ticker no configurado: {ticker}. Agrégalo a ASSET_CONFIG en asset_config.py.")
    return ASSET_CONFIG[ticker]
