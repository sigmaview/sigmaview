"""Test de validación: ¿refrescar L1 en el momento en que se detecta un salto de grado
arregla el cálculo de Modo B? Simula sobre el caso real del 2026-06-21 (la señal falsa).

No modifica nada en producción — solo imprime una comparación. Requiere una llamada real
a Opus para recalcular L1 con asof=hoy (~$0.30-0.50 para BTC).

Uso: ANTHROPIC_API_KEY="sk-ant-..." python3 tests/test_salto_de_grado.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))
import analyzer_weekly as l1m
import signal_generator as sg

ASOF = "2026-06-21"  # la fecha del caso real con salto de grado detectado

def main() -> None:
    l3 = json.loads((Path(__file__).parent.parent / "data" / "l3_btc_latest.json").read_text())
    l1_viejo = json.loads((Path(__file__).parent.parent / "data" / "l1_btc_latest.json").read_text())

    print(f"{'='*70}\nL1 VIEJO (cacheado, última corrida {l1_viejo.get('fecha_analisis')})\n{'='*70}")
    print(f"  techo_operativo: ${l1_viejo['techo_operativo']['precio']:,.0f} "
          f"({l1_viejo['techo_operativo']['fecha']})")
    print(f"  low_operativo:   ${l1_viejo['niveles_para_l2']['low_operativo']:,.0f}")

    modelo_a_inicio = (l3.get("pivotes") or {}).get("abc_a_inicio")
    print(f"\n  L3 de hoy narra abc_a_inicio=${modelo_a_inicio:,.0f} — "
          f"discrepancia: {abs(l1_viejo['techo_operativo']['precio']-modelo_a_inicio)/modelo_a_inicio:.0%}")

    print(f"\n{'='*70}\nRefrescando L1 con asof={ASOF} (llamada real a Opus)...\n{'='*70}")
    df = l1m.fetch_weekly_df("BTC-USD", asof=ASOF, start_date="2014-01-01")
    price_csv = l1m.df_to_csv(df)
    prompt = l1m.PROMPT_PATH.read_text().format(
        price_data=price_csv, date=ASOF, asset="BTC/USD", start_date="2014-01-01")
    l1_nuevo, usage = l1m.call_model(prompt)
    techo = l1_nuevo.get("techo_operativo", {})
    fib = l1m.compute_operative_levels(df, float(techo.get("precio", 0) or 0),
                                        techo.get("fecha", ""), techo.get("tipo", ""))
    if fib:
        l1_nuevo.setdefault("niveles_para_l2", {}).update({
            "low_operativo": fib["extremo_opuesto"],
            "retroceso_382": fib["retroceso_382"], "retroceso_50": fib["retroceso_50"],
            "retroceso_618": fib["retroceso_618"], "_fib_calculado_por": "python",
        })
    cost = (usage.input_tokens * 15 + usage.output_tokens * 75) / 1_000_000
    print(f"  Costo: ${cost:.4f} USD ({usage.input_tokens} in / {usage.output_tokens} out)")

    print(f"\n{'='*70}\nL1 NUEVO (asof={ASOF})\n{'='*70}")
    print(f"  techo_operativo: ${l1_nuevo['techo_operativo']['precio']:,.0f} "
          f"({l1_nuevo['techo_operativo']['fecha']})")
    print(f"  low_operativo:   ${l1_nuevo['niveles_para_l2']['low_operativo']:,.0f}")
    disc_nuevo = abs(l1_nuevo['techo_operativo']['precio'] - modelo_a_inicio) / modelo_a_inicio
    print(f"  Discrepancia vs abc_a_inicio narrado por L3 ({modelo_a_inicio:,.0f}): {disc_nuevo:.0%}")

    print(f"\n{'='*70}\nRe-evaluando Modo B con L1 refrescado\n{'='*70}")
    l1_levels_nuevo = {
        "techo": l1_nuevo["techo_operativo"]["precio"],
        "operativo": l1_nuevo["niveles_para_l2"]["low_operativo"],
    }
    mb = sg.evaluar_modo_b(l3, l1_levels_nuevo)
    print(f"  dispara={mb['dispara']}")
    print(f"  motivo={mb['motivo']}")
    if mb.get("trade"):
        t = mb["trade"]
        print(f"  trade: entrada=${t['entrada']:,.0f} stop=${t['stop']:,.0f} "
              f"O1=${t['O1']:,.0f} O2=${t['O2']:,.0f} O3=${t['O3']:,.0f}")

    print(f"\n{'='*70}\nCOMPARACIÓN FINAL\n{'='*70}")
    print(f"  Con L1 viejo (stale):     discrepancia 52%  -> bloqueado por chequeo de grado")
    print(f"  Con L1 refrescado ahora:  discrepancia {disc_nuevo:.0%}  -> dispara={mb['dispara']}")

if __name__ == "__main__":
    main()
