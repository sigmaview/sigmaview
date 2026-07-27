"""Backtest del pipeline completo L1→L2→L3 en una fecha histórica.
Usa solo datos hasta la fecha 'asof' y compara contra la señal conocida.

Uso: ANTHROPIC_API_KEY="sk-ant-..." python3 tests/backtest.py 2025-04-07
     (sin fecha corre los checkpoints predefinidos)
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import analyzer_weekly as l1m
import monitor_daily as l2m
import signal_generator as l3m
import fill_simulator
import asset_config

CACHE_DIR = Path(__file__).parent.parent / "data" / "backtest_cache"

def cost_of(usage, model: str) -> float:
    r_in, r_out = l3m.RATES.get(model, (15, 75))
    return (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000

def cached(asof: str, nivel: str, ticker_slug: str, fn):
    """Cachea el resultado de L1/L2 por fecha+ticker. Re-corridas solo rehacen L3.
    fn() debe devolver (resultado, costo_usd) — costo es 0.0 si viene de cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{asof}_{ticker_slug}_{nivel}.json"
    if path.exists():
        print(f"  ({nivel} desde cache)", end=" ", flush=True)
        return json.loads(path.read_text()), 0.0
    res, cost = fn()
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    return res, cost

# Checkpoints conocidos del backtest documentado
CHECKPOINTS = [
    {"asof": "2025-04-07", "esperado": "LONG ~$75k (fin corrección ABC, c=1.618a) → +56%"},
    {"asof": "2025-11-04", "esperado": "SHORT ~$101k (2/3 Santos, rotura mínimo W1) → +25%"},
    {"asof": "2026-02-06", "esperado": "Gestión + esperar (suelo de onda, no nueva entrada)"},
]

def compute_l1(asof: str, ticker: str = "BTC-USD", cfg: dict | None = None) -> tuple[dict, float]:
    cfg = cfg or asset_config.get_config(ticker)
    df = l1m.fetch_weekly_df(ticker, asof=asof, start_date=cfg["l1_start_date"])
    p1 = l1m.PROMPT_PATH.read_text().format(price_data=l1m.df_to_csv(df), date=asof,
                                             asset=cfg["asset_label"], start_date=cfg["l1_start_date"])
    l1res, usage = l1m.call_model(p1)
    techo = l1res.get("techo_operativo", {})
    fib = l1m.compute_operative_levels(df, float(techo.get("precio", 0) or 0),
                                       techo.get("fecha", ""), techo.get("tipo", ""))
    if fib:
        l1res.setdefault("niveles_para_l2", {}).update({
            "low_operativo": fib["extremo_opuesto"], "retroceso_382": fib["retroceso_382"],
            "retroceso_50": fib["retroceso_50"], "retroceso_618": fib["retroceso_618"],
            "_fib_calculado_por": "python",
        })
    return l1res, cost_of(usage, l1m.MODEL)

def compute_l2(asof: str, l1res: dict, ticker: str = "BTC-USD", cfg: dict | None = None) -> tuple[dict, float]:
    cfg = cfg or asset_config.get_config(ticker)
    dcsv = l2m.fetch_daily_data(cfg["l2_candle_count"], asof=asof, ticker=ticker)
    p2 = l2m.build_prompt(l2m.PROMPT_PATH.read_text(), l1res, dcsv, asof,
                           asset=cfg["asset_label"], candle_count=cfg["l2_candle_count"])
    l2res, usage = l2m.call_model(p2)
    return l2res, cost_of(usage, l2m.MODEL)

def run_pipeline(asof: str, ticker: str = "BTC-USD", modelo_l3: str | None = None) -> dict:
    cfg = asset_config.get_config(ticker)

    # L1 y L2 se cachean por fecha+ticker; las re-corridas solo rehacen L3
    l1res, cost_l1 = cached(asof, "l1", cfg["ticker_slug"], lambda: compute_l1(asof, ticker, cfg))
    l2res, cost_l2 = cached(asof, "l2", cfg["ticker_slug"], lambda: compute_l2(asof, l1res, ticker, cfg))

    # ── L3 ── modelo_l3 fuerza un modelo (backtest riguroso); None = tiered según L2
    model = modelo_l3 or ("claude-opus-4-8" if l2res.get("nivel_alerta") == "SEÑAL" else "claude-sonnet-4-6")
    price_csv = l3m.fetch_price_data(cfg["l3_candle_count"], asof=asof, ticker=ticker,
                                      modo=cfg["l3_candle_mode"])
    timeframe_label = "1 HORA" if cfg["l3_candle_mode"] == "1h_native" else "4 HORAS"
    p3 = l3m.build_l3_prompt_from(l1res, l2res, price_csv, asof, asset=cfg["asset_label"],
                                   candle_count=cfg["l3_candle_count"], timeframe_label=timeframe_label)
    l3res, usage3 = l3m.call_model(p3, model)
    cost_l3 = cost_of(usage3, model)
    cost_total = cost_l1 + cost_l2 + cost_l3

    # Decisión de disparo determinista en Python (extremos operativos de L1 → Modo B reproducible)
    l1_levels = {
        "techo": l1res.get("techo_operativo", {}).get("precio"),
        "operativo": l1res.get("niveles_para_l2", {}).get("low_operativo"),
    }
    decision = l3m.decidir_veredicto(l3res, l1_levels)
    l3res["veredicto"] = decision["veredicto"]
    l3res["calidad_señal"] = decision["calidad"]
    if decision["modo"]:
        l3res["modo_entrada"] = decision["modo"]
    l3res["_decision_python"] = decision["motivo"]
    trade = decision["trade"]

    # Simulación de ejecución: orden armada en 'asof', ¿se llena? ¿P&L en R?
    sim = None
    if trade:
        sim = fill_simulator.simular(
            l3res.get("direccion", "LONG"), trade["entrada"], trade["stop"],
            trade["O1"], trade["O2"], trade["O3"], asof,
            dias=cfg["fill_sim_dias"], ticker=ticker)

    return {"l1": l1res, "l2": l2res, "l3": l3res, "trade": trade, "sim": sim,
            "modelo_l3": model, "cost": cost_total}

def print_result(asof: str, esperado: str, r: dict) -> None:
    l2, l3, trade = r["l2"], r["l3"], r["trade"]
    le = l3.get("lectura_estructural", {})
    print(f"\n{'='*70}")
    print(f"  BACKTEST {asof}")
    print(f"  Esperado: {esperado}")
    print(f"{'='*70}")
    print(f"  L1 techo operativo: ${r['l1'].get('techo_operativo',{}).get('precio','')}")
    print(f"  L2 alerta: {l2.get('nivel_alerta','')}  (score {l2.get('score_santos','')}/3)  → {l2.get('escenario_favorecido','')}")
    print(f"  L3 [{r['modelo_l3']}] sub-onda: {le.get('sub_onda_actual','')}")
    print(f"  L3 veredicto: {l3.get('veredicto','')}  —  {l3.get('direccion','')} ({l3.get('calidad_señal','')})")
    sim = r.get("sim")
    if trade:
        print(f"\n  ── TICKET DE TRADE ({l3.get('direccion','')}) ──")
        fill = sim.get("fill_dia") if sim and sim.get("filled") else "no llenó"
        print(f"     ENTRADA:  ${trade['entrada']:>12,.2f}   (ejecutada: {fill})")
        print(f"     STOP:     ${trade['stop']:>12,.2f}   (R:R a cada objetivo abajo)")
        print(f"     O1:       ${trade['O1']:>12,.2f}   R:R {trade['R:R']['O1']}x  → {trade['O1_accion']}")
        print(f"     O2:       ${trade['O2']:>12,.2f}   R:R {trade['R:R']['O2']}x  → {trade['O2_accion']}")
        print(f"     O3:       ${trade['O3']:>12,.2f}   R:R {trade['R:R']['O3']}x  → {trade['O3_accion']}")
    if sim:
        if sim.get("filled"):
            print(f"\n  📊 SIMULACIÓN: {sim['resultado']}  ({sim['r']:+.2f}R)")
            for e in sim.get("eventos", []):
                print(f"       {e}")
        else:
            print(f"\n  📊 SIMULACIÓN: {sim.get('motivo','no se llenó')} (0R)")
    print(f"\n  Razón: {l3.get('razon','')}")
    print(f"  Python gate: {l3.get('_decision_python','')}")
    ch = l3.get("checklist") or {}
    print(f"  Checklist raw: s1={ch.get('s1_retroceso')} s2={ch.get('s2_estructura')} s3={ch.get('s3_linea24')} cumplidas={ch.get('cumplidas')}")

def main() -> None:
    # Segundo argumento opcional: forzar modelo L3 (ej. claude-opus-4-8) para reproducir el batch
    modelo_l3 = sys.argv[2] if len(sys.argv) > 2 else None
    if len(sys.argv) > 1:
        asof = sys.argv[1]
        known = next((c for c in CHECKPOINTS if c["asof"] == asof), None)
        cps = [known] if known else [{"asof": asof, "esperado": "(sin referencia conocida)"}]
    else:
        cps = CHECKPOINTS
    for cp in cps:
        print(f"\nCorriendo pipeline para {cp['asof']}...")
        r = run_pipeline(cp["asof"], modelo_l3=modelo_l3)
        print_result(cp["asof"], cp["esperado"], r)

if __name__ == "__main__":
    main()
