"""Testea si la calidad del breakout en 1h (impulsivo vs débil/choppy) predice
el resultado de los trades Modo C ya simulados en el walk-forward.
100% determinístico sobre los datos 1h ya acumulados en la DB — sin LLM.

Uso: PYTHONPATH=src python3 tests/microstructure_breakout_test.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import database

RESULTS_FILE  = Path(__file__).parent.parent / "data" / "walkforward_results.json"
L3_DIR        = Path(__file__).parent.parent / "data" / "walkforward_l3"
TICKER        = "BTC-USD"
WINDOW_HOURS  = 48     # velas 1h de seguimiento tras el toque del nivel
MFE_THRESHOLD = 0.3    # R mínimo de avance favorable para considerar "impulsivo"
WHIPSAW_TOL   = 0.05   # R que puede devolver sin contar como whipsaw


def load_1h(ticker: str, since: str) -> pd.DataFrame:
    conn = database._conn()
    df = pd.read_sql_query(
        "SELECT ts,open,high,low,close FROM ohlcv WHERE ticker=? AND ts >= ? ORDER BY ts",
        conn, params=[ticker, since],
    )
    conn.close()
    return df


def analizar_breakout(direccion: str, entrada: float, stop: float, asof: str) -> dict | None:
    df = load_1h(TICKER, asof)
    if df.empty:
        return None
    long = direccion.upper() == "LONG"
    riesgo = abs(entrada - stop)

    touch_idx = None
    for i, row in df.iterrows():
        if row["low"] <= entrada <= row["high"]:
            touch_idx = i
            break
    if touch_idx is None:
        return None

    window = df.iloc[touch_idx: touch_idx + WINDOW_HOURS + 1]
    if len(window) < 2:
        return None

    if long:
        mfe = (window["high"].max() - entrada) / riesgo
        whipsaw = window["low"].min() < entrada - WHIPSAW_TOL * riesgo
    else:
        mfe = (entrada - window["low"].min()) / riesgo
        whipsaw = window["high"].max() > entrada + WHIPSAW_TOL * riesgo

    calidad = "impulsivo" if (mfe >= MFE_THRESHOLD and not whipsaw) else "debil_choppy"

    # R real si se hubiera salido al cierre de la última vela de la ventana
    cierre_ventana = float(window.iloc[-1]["close"])
    r_si_sale_en_ventana = ((cierre_ventana - entrada) / riesgo if long
                            else (entrada - cierre_ventana) / riesgo)

    return {
        "mfe_window": round(float(mfe), 2), "whipsaw": bool(whipsaw), "calidad": calidad,
        "r_si_sale_en_ventana": round(float(r_si_sale_en_ventana), 2),
    }


def main() -> None:
    results = json.loads(RESULTS_FILE.read_text())
    trades = [(d, r) for d, r in results.items() if r.get("modo") == "C_breakout" and r.get("filled")]

    rows = []
    for asof, r in sorted(trades):
        l3_path = L3_DIR / f"{asof}_l3.json"
        if not l3_path.exists():
            continue
        l3data = json.loads(l3_path.read_text())
        trade = l3data.get("trade") or {}
        entrada, stop = trade.get("entrada"), trade.get("stop")
        if entrada is None or stop is None:
            continue
        breakout = analizar_breakout(r["direccion"], entrada, stop, asof)
        if breakout is None:
            print(f"{asof}: sin datos 1h suficientes, omitido")
            continue
        rows.append({
            "fecha": asof, "direccion": r["direccion"],
            "r_resultado": r["r"], "resultado": r["resultado"],
            **breakout,
        })

    print(f"\n{'Fecha':12} {'Dir':6} {'Calidad breakout':16} {'MFE 12h':>8} {'Whipsaw':>8} {'R resultado':>12}")
    print("-" * 70)
    for row in rows:
        print(f"{row['fecha']:12} {row['direccion']:6} {row['calidad']:16} "
              f"{row['mfe_window']:>8.2f} {str(row['whipsaw']):>8} {row['r_resultado']:>+12.2f}")

    impulsivos = [row for row in rows if row["calidad"] == "impulsivo"]
    choppy     = [row for row in rows if row["calidad"] == "debil_choppy"]

    def stats(grupo, nombre):
        if not grupo:
            print(f"\n{nombre}: sin casos")
            return
        n = len(grupo)
        winners = [row for row in grupo if row["r_resultado"] > 0]
        r_total = sum(row["r_resultado"] for row in grupo)
        print(f"\n{nombre}: {n} trades | win rate {100*len(winners)/n:.0f}% | "
              f"R total {r_total:+.2f} | R/trade {r_total/n:+.2f}")

    print(f"\n{'='*70}")
    print("RESULTADOS POR CALIDAD DE BREAKOUT")
    print(f"{'='*70}")
    stats(impulsivos, "IMPULSIVO (mfe>=0.3R en 12h, sin whipsaw)")
    stats(choppy, "DEBIL/CHOPPY (mfe<0.3R o con whipsaw)")

    out_file = Path(__file__).parent.parent / "data" / "microstructure_breakout_test.json"
    out_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\nGuardado en: {out_file}")


if __name__ == "__main__":
    main()
