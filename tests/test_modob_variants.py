"""Compara las variantes de prompt de L3 (base / Modo B fuerte / Modo B filtro)
sobre el mismo checkpoint histórico. Corre L1+L2 una vez y L3 con cada variante (Opus).

Uso: ANTHROPIC_API_KEY="sk-ant-..." python3 tests/test_modob_variants.py 2025-04-07
"""
import sys
import json
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))
import analyzer_weekly as l1m
import monitor_daily as l2m
import signal_generator as l3m

L3_MODEL = "claude-opus-4-8"          # mismo modelo para aislar el efecto del prompt
VARIANTS = {
    "base":   SRC / "prompts" / "level3_signal.txt",
    "fuerte": SRC / "prompts" / "level3_modoB_fuerte.txt",
    "filtro": SRC / "prompts" / "level3_modoB_filtro.txt",
}

def upstream(asof: str) -> tuple[dict, dict, str]:
    df = l1m.fetch_weekly_df(asof=asof)
    p1 = l1m.PROMPT_PATH.read_text().format(price_data=l1m.df_to_csv(df), date=asof)
    l1res, _ = l1m.call_model(p1)
    techo = l1res.get("techo_operativo", {})
    fib = l1m.compute_operative_levels(df, float(techo.get("precio", 0) or 0),
                                       techo.get("fecha", ""), techo.get("tipo", ""))
    if fib:
        l1res.setdefault("niveles_para_l2", {}).update({
            "low_operativo": fib["extremo_opuesto"], "retroceso_382": fib["retroceso_382"],
            "retroceso_50": fib["retroceso_50"], "retroceso_618": fib["retroceso_618"],
        })
    dcsv = l2m.fetch_daily_data(l2m.CANDLE_COUNT, asof=asof)
    p2 = l2m.build_prompt(l2m.PROMPT_PATH.read_text(), l1res, dcsv, asof)
    l2res, _ = l2m.call_model(p2)
    c4 = l3m.fetch_4h_data(l3m.CANDLE_COUNT, asof=asof)
    return l1res, l2res, c4

def run_variant(template_path: Path, l1, l2, c4, asof) -> dict:
    ctx_l1 = json.dumps({k: l1.get(k) for k in ("techo_operativo", "escenarios", "acuerdo",
                                                "divergencia", "niveles_para_l2")}, ensure_ascii=False)
    ctx_l2 = json.dumps({k: l2.get(k) for k in ("escenario_favorecido", "resolutorios_cruzados",
                                                "score_santos", "resumen")}, ensure_ascii=False)
    prompt = template_path.read_text().format(
        asset=l3m.ASSET, date=asof,
        fecha_l1=l1.get("fecha_analisis", "?"), contexto_l1=ctx_l1,
        fecha_l2=l2.get("fecha", "?"), contexto_l2=ctx_l2,
        candle_count=l3m.CANDLE_COUNT, price_data=c4,
    )
    res, _ = l3m.call_model(prompt, L3_MODEL)
    trade = None
    if res.get("veredicto") == "SEÑAL":
        try:
            trade = l3m.compute_trade(res["direccion"], res["modo_entrada"], res["pivotes"])
        except (KeyError, ValueError, TypeError):
            pass
    return {"res": res, "trade": trade}

def main() -> None:
    asof = sys.argv[1] if len(sys.argv) > 1 else "2025-04-07"
    print(f"Corriendo L1+L2 para {asof}...", end=" ", flush=True)
    l1, l2, c4 = upstream(asof)
    print("OK")
    print(f"L2: alerta={l2.get('nivel_alerta')} score={l2.get('score_santos')} fav={l2.get('escenario_favorecido')}")

    out = {}
    for name, path in VARIANTS.items():
        print(f"L3 variante '{name}'...", end=" ", flush=True)
        out[name] = run_variant(path, l1, l2, c4, asof)
        print("OK")

    print(f"\n{'='*74}\n  COMPARACIÓN L3 — {asof}\n{'='*74}")
    for name, o in out.items():
        r, t = o["res"], o["trade"]
        mb = r.get("modo_b_check", {})
        print(f"\n  [{name}]  {r.get('veredicto','')} — {r.get('direccion','')} ({r.get('modo_entrada','')})")
        if mb:
            print(f"     modo_b: c/a={mb.get('c_sobre_a')} rr_o3={mb.get('rr_o3_estimado')} "
                  f"macro={mb.get('filtro_macro_ok','-')} dispara={mb.get('dispara')}")
        if t:
            print(f"     trade: entrada ${t['entrada']:,} stop ${t['stop']:,} O2 ${t['O2']:,} R:R(O3) {t['R:R']['O3']}x")
        print(f"     razón: {r.get('razon','')[:160]}")

if __name__ == "__main__":
    main()
