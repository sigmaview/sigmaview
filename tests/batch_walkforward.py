"""Walk-forward semanal sobre toda la ventana de datos 4h disponible.
Fechas sistemáticas cada 7 días desde jul-2024 hasta hoy — sin cherry-picking.

Guarda resultados en data/walkforward_results.json después de CADA fecha.
Si se interrumpe (créditos agotados, error de red, etc.), re-arranca
automáticamente desde donde quedó: las fechas ya completadas no se repiten.

Uso:
    ANTHROPIC_API_KEY="sk-ant-..." caffeinate python3 tests/batch_walkforward.py
"""
from datetime import date, timedelta
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backtest as bt
import asset_config

START_DATES  = {"BTC-USD": date(2024, 7, 1), "^GSPC": date(2023, 8, 1)}  # ventana 1h yfinance (~730d)
STEP_DAYS    = 7                   # semanal
MODELO_L3    = "claude-opus-4-8"  # Opus en todo L3 para calidad uniforme

# ── Generación de fechas ───────────────────────────────────────────────────────

def generate_dates(start_date: date) -> list[str]:
    today = date.today()
    dates, d = [], start_date
    while d <= today:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=STEP_DAYS)
    return dates

# ── Persistencia ──────────────────────────────────────────────────────────────

def load_results(results_file: Path) -> dict:
    if results_file.exists():
        return json.loads(results_file.read_text())
    return {}

def save_results(results: dict, results_file: Path) -> None:
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(results, ensure_ascii=False, indent=2))

# ── Main ──────────────────────────────────────────────────────────────────────

def main(ticker: str = "BTC-USD", limit: int | None = None) -> None:
    cfg = asset_config.get_config(ticker)
    data_dir     = Path(__file__).parent.parent / "data"
    results_file = data_dir / (f"walkforward_results_{cfg['ticker_slug']}.json"
                                if ticker != "BTC-USD" else "walkforward_results.json")
    l3_dir       = data_dir / (f"walkforward_l3_{cfg['ticker_slug']}"
                                if ticker != "BTC-USD" else "walkforward_l3")
    start_date   = START_DATES[ticker]

    dates   = generate_dates(start_date)
    results = load_results(results_file)

    pending  = [d for d in dates if d not in results]
    done     = [d for d in dates if d in results]
    if limit:
        pending = pending[:limit]

    print(f"Walk-forward semanal {cfg['asset_label']}")
    print(f"  Ventana:    {dates[0]} → {dates[-1]}")
    print(f"  Total:      {len(dates)} fechas")
    print(f"  Completadas:{len(done)}  |  Por correr ahora: {len(pending)}"
          + (f" (limitado con --limit {limit})" if limit else ""))
    if not pending:
        print("  Nada nuevo que correr — mostrando resumen del archivo guardado.")

    costo_acumulado = 0.0
    for i, asof in enumerate(pending):
        print(f"\n[{i+1}/{len(pending)}] {asof}...", flush=True)
        try:
            r = bt.run_pipeline(asof, ticker=ticker, modelo_l3=MODELO_L3)
        except Exception as e:
            print(f"  ERROR: {e}")
            results[asof] = {"error": str(e)}
            save_results(results, results_file)
            continue

        l3   = r["l3"]
        sim  = r.get("sim")
        cost = r.get("cost", 0.0)
        costo_acumulado += cost
        results[asof] = {
            "veredicto":   l3.get("veredicto"),
            "direccion":   l3.get("direccion"),
            "calidad":     l3.get("calidad_señal"),
            "modo":        l3.get("modo_entrada"),
            "sesgo_macro": l3.get("sesgo_macro"),
            "fase_macro":  l3.get("fase_impulso_macro"),
            "sub_onda":    (l3.get("lectura_estructural") or {}).get("sub_onda_actual"),
            "filled":      sim.get("filled") if sim else None,
            "r":           sim.get("r") if sim and sim.get("filled") else (0.0 if sim else None),
            "resultado":   sim.get("resultado") if sim else "—",
            "razon":       (l3.get("razon") or "")[:300],
            "cost":        round(cost, 4),
        }
        save_results(results, results_file)   # ← resumen guardado inmediato tras cada fecha

        # JSON completo de L3 (razonamiento, pivotes, plan de trade) para auditoría
        l3_dir.mkdir(parents=True, exist_ok=True)
        (l3_dir / f"{asof}_l3.json").write_text(
            json.dumps({"l3": r["l3"], "trade": r.get("trade"), "sim": r.get("sim")},
                       ensure_ascii=False, indent=2)
        )

        v     = results[asof]["veredicto"]
        r_val = results[asof]["r"]
        r_str = f"{r_val:+.2f}R" if r_val is not None else "—"
        print(f"  → {v} {results[asof]['direccion'] or '—'} | "
              f"{results[asof]['calidad'] or '—'} | {r_str}  "
              f"(${cost:.3f} esta fecha — ${costo_acumulado:.2f} acumulado en esta corrida)")

    # ── Tabla y estadísticas ─────────────────────────────────────────────────
    all_r   = [v for v in results.values() if "error" not in v]
    señales = [r for r in all_r if r["veredicto"] == "SEÑAL"]
    trades  = [r for r in all_r if r.get("filled")]
    winners = [r for r in trades if (r.get("r") or 0) > 0]
    r_total = sum(r.get("r") or 0 for r in trades)

    print(f"\n\n{'='*72}")
    print(f"  WALK-FORWARD — {len(all_r)} fechas completadas")
    print(f"{'='*72}")
    print(f"  {'Fecha':12} {'Vered.':9} {'Dir':6} {'Calidad':12} {'Fill':5} {'R':>7}  Resultado")
    print(f"  {'-'*68}")
    for asof in sorted(results):
        x = results[asof]
        if "error" in x:
            print(f"  {asof:12} ERROR: {x['error'][:50]}")
            continue
        r_s  = f"{x['r']:+.2f}" if x["r"] is not None else "—"
        fill = {True: "sí", False: "no", None: "—"}[x.get("filled")]
        print(f"  {asof:12} {str(x['veredicto']):9} {str(x['direccion'] or '—'):6} "
              f"{str(x['calidad'] or '—'):12} {fill:5} {r_s:>7}  {x['resultado']}")

    print(f"\n  {'='*68}")
    print(f"  Señales generadas:    {len(señales)} / {len(all_r)} fechas  "
          f"({100*len(señales)/len(all_r):.0f}% señal rate)")
    print(f"  Trades llenados:      {len(trades)}")
    print(f"  Señales sin fill:     {len([s for s in señales if not s.get('filled')])}")
    print(f"  Esperar:              {len(all_r)-len(señales)}")
    if trades:
        print(f"  Win rate:             {len(winners)}/{len(trades)} = "
              f"{100*len(winners)/len(trades):.0f}%")
        print(f"  R total:              {r_total:+.2f}R")
        print(f"  R promedio/trade:     {r_total/len(trades):+.2f}R")
        print(f"  Expectancy:           {r_total/len(all_r):+.3f}R/fecha")
    costo_total_archivo = sum(x.get("cost") or 0.0 for x in all_r)
    print(f"  Costo API acumulado (fechas con dato de costo): ${costo_total_archivo:.2f}")
    print(f"  Costo API de esta corrida:                       ${costo_acumulado:.2f}")
    print(f"\n  Guardado en: {results_file}")

if __name__ == "__main__":
    arg_ticker = "BTC-USD"
    arg_limit  = None
    args = sys.argv[1:]
    if "--ticker" in args:
        arg_ticker = args[args.index("--ticker") + 1]
    if "--limit" in args:
        arg_limit = int(args[args.index("--limit") + 1])
    main(arg_ticker, limit=arg_limit)
