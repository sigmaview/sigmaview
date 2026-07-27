"""Re-corre L1 (semanal) y L2 (diario) acotados a una fecha de cierre específica (asof) y
escribe el resultado en los archivos de PRODUCCIÓN reales (data/l1_btc_latest.json,
data/l2_btc_latest.json) — pensado para refrescar manualmente con la última vela cerrada
sin esperar al lunes (L1) o a la corrida automática del día (L2).

ADVERTENCIA: sobreescribe los archivos de producción reales. Pensado para un refresh puntual,
no para uso recurrente.

Costo: L1 Opus ~$0.55, L2 Sonnet ~$0.04.
Uso: ANTHROPIC_API_KEY="sk-ant-..." PYTHONPATH=src python3 tests/refresh_l1l2_asof.py 2026-06-21
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import analyzer_weekly as l1m
import monitor_daily as l2m

ASSET = "BTC/USD"
TICKER = "BTC-USD"
START_DATE = "2014-01-01"

def refrescar_l1(asof: str) -> dict:
    print(f"[L1] Bajando datos semanales hasta {asof}...", end=" ", flush=True)
    df = l1m.fetch_weekly_df(TICKER, asof=asof, start_date=START_DATE)
    price_csv = l1m.df_to_csv(df)
    print(f"OK ({len(df)} velas)")

    prompt = l1m.PROMPT_PATH.read_text().format(
        price_data=price_csv, date=asof, asset=ASSET, start_date=START_DATE)
    print(f"[L1] Llamando a {l1m.MODEL}...", end=" ", flush=True)
    result, usage = l1m.call_model(prompt)
    cost = (usage.input_tokens * 15 + usage.output_tokens * 75) / 1_000_000
    print(f"OK (${cost:.3f})")

    techo = result.get("techo_operativo", {})
    fib = l1m.compute_operative_levels(df, float(techo.get("precio", 0) or 0),
                                        techo.get("fecha", ""), techo.get("tipo", ""))
    if fib:
        result.setdefault("niveles_para_l2", {}).update({
            "low_operativo": fib["extremo_opuesto"], "retroceso_382": fib["retroceso_382"],
            "retroceso_50": fib["retroceso_50"], "retroceso_618": fib["retroceso_618"],
            "_fib_calculado_por": "python",
        })
    result["_meta"] = {"generado": asof, "modelo": l1m.MODEL}
    l1m.OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[L1] techo_operativo=${techo.get('precio'):,.0f} ({techo.get('fecha')}) "
          f"low_operativo=${result.get('niveles_para_l2', {}).get('low_operativo'):,.0f}")
    print(f"[L1] Guardado en {l1m.OUTPUT_FILE}")
    return result

def refrescar_l2(asof: str, l1: dict) -> dict:
    print(f"[L2] Bajando datos diarios hasta {asof}...", end=" ", flush=True)
    price_csv = l2m.fetch_daily_data(l2m.CANDLE_COUNT, asof=asof, ticker=TICKER)
    print(f"OK ({len(price_csv.splitlines())-1} velas)")

    prompt = l2m.build_prompt(l2m.PROMPT_PATH.read_text(), l1, price_csv, asof, asset=ASSET)
    print(f"[L2] Llamando a {l2m.MODEL}...", end=" ", flush=True)
    result, usage = l2m.call_model(prompt)
    cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
    print(f"OK (${cost:.3f})")

    result["_meta"] = {"generado": asof, "modelo": l2m.MODEL, "l1_fecha": l1.get("fecha_analisis")}
    l2m.OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[L2] nivel_alerta={result.get('nivel_alerta')} score={result.get('score_santos')}")
    print(f"[L2] Guardado en {l2m.OUTPUT_FILE}")
    return result

def main() -> None:
    asof = sys.argv[1] if len(sys.argv) > 1 else sys.exit("Uso: refresh_l1l2_asof.py YYYY-MM-DD")
    l1 = refrescar_l1(asof)
    l2 = refrescar_l2(asof, l1)
    print(f"\n{'='*60}\nListo — L1 y L2 refrescados con datos hasta {asof}.\n{'='*60}")

if __name__ == "__main__":
    main()
