"""Backtest prospectivo amplio: corre el pipeline + simulación de fill sobre muchas fechas
y reporta estadísticas agregadas (win rate, R total, R promedio).

Una sola corrida, secuencial. L1/L2 se cachean por fecha (re-corridas baratas).
Uso: ANTHROPIC_API_KEY="sk-ant-..." caffeinate python3 tests/batch_backtest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backtest as bt

# Fechas distribuidas en la ventana con datos 4h (2024-06 → 2026-06)
# Mezcla: fondos, techos, chop, mitad de tendencia, eventos conocidos
FECHAS = [
    "2024-08-05",  # V-bottom $49k
    "2024-11-11",  # rally/breakout
    "2024-12-17",  # techo $108k
    "2025-02-26",  # caída
    "2025-04-01",  # pre-fondo abril (prospectivo)
    "2025-07-01",  # mitad rally
    "2025-10-06",  # ATH $126k (techo)
    "2025-11-04",  # SHORT
    "2026-01-10",  # chop
    "2026-02-06",  # capitulación
]

def main() -> None:
    MODELO_L3 = "claude-opus-4-8"   # Opus en todo L3 para medir calidad real (sin ruido de modelo)
    resultados = []
    for i, asof in enumerate(FECHAS):
        print(f"\n[{i+1}/{len(FECHAS)}] {asof}...", flush=True)
        try:
            r = bt.run_pipeline(asof, modelo_l3=MODELO_L3)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        l3 = r["l3"]
        sim = r.get("sim")
        resultados.append({
            "fecha": asof,
            "veredicto": l3.get("veredicto"),
            "direccion": l3.get("direccion"),
            "calidad": l3.get("calidad_señal"),
            "filled": sim.get("filled") if sim else None,
            "r": sim.get("r") if sim and sim.get("filled") else (0.0 if sim else None),
            "resultado": sim.get("resultado") if sim else "—",
        })

    # ── Tabla y estadísticas ──
    print(f"\n\n{'='*78}\n  RESUMEN — {len(resultados)} fechas\n{'='*78}")
    print(f"  {'Fecha':12} {'Veredicto':9} {'Dir':6} {'Calidad':11} {'Fill':6} {'R':>7}  Resultado")
    print(f"  {'-'*74}")
    for x in resultados:
        r_str = f"{x['r']:+.2f}" if x["r"] is not None else "—"
        fill = {True: "sí", False: "no", None: "—"}[x["filled"]]
        print(f"  {x['fecha']:12} {str(x['veredicto']):9} {str(x['direccion'] or '—'):6} "
              f"{str(x['calidad'] or '—'):11} {fill:6} {r_str:>7}  {x['resultado']}")

    trades = [x for x in resultados if x["filled"]]
    señales = [x for x in resultados if x["veredicto"] == "SEÑAL"]
    no_fill = [x for x in resultados if x["veredicto"] == "SEÑAL" and not x["filled"]]
    ganadores = [x for x in trades if (x["r"] or 0) > 0]
    r_total = sum(x["r"] or 0 for x in trades)

    print(f"\n  {'='*74}")
    print(f"  Señales generadas:     {len(señales)}")
    print(f"  Trades llenados:       {len(trades)}")
    print(f"  Señales sin fill:      {len(no_fill)}")
    print(f"  Esperar (sin señal):   {len(resultados)-len(señales)}")
    if trades:
        print(f"  Win rate:              {len(ganadores)}/{len(trades)} = {100*len(ganadores)/len(trades):.0f}%")
        print(f"  R total:               {r_total:+.2f}R")
        print(f"  R promedio por trade:  {r_total/len(trades):+.2f}R")

if __name__ == "__main__":
    main()
