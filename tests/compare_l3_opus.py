"""Compara la lectura L3 de hoy entre Sonnet (ya corrida, en disco) y Opus (nueva llamada).
Reutiliza el contexto L1/L2 ya generado por la corrida de producción — solo paga la
llamada extra a Opus. NO escribe a ningún archivo de producción ni a la DB.

Uso: ANTHROPIC_API_KEY="sk-ant-..." PYTHONPATH=src python3 tests/compare_l3_opus.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import signal_generator as sg

OPUS_MODEL = "claude-opus-4-8"


def main() -> None:
    date = datetime.now().strftime("%Y-%m-%d")

    sonnet_result = json.loads(sg.OUTPUT_FILE.read_text())

    print("Construyendo prompt L3 (reutiliza L1/L2 ya en disco)...", end=" ", flush=True)
    prompt, l1, l2 = sg.build_l3_prompt(date)
    print("OK")

    print(f"Llamando a {OPUS_MODEL}...", end=" ", flush=True)
    opus_result, usage = sg.call_model(prompt, OPUS_MODEL)
    print("OK")

    l1_levels = {
        "techo": l1.get("techo_operativo", {}).get("precio"),
        "operativo": l1.get("niveles_para_l2", {}).get("low_operativo"),
    }
    decision = sg.decidir_veredicto(opus_result, l1_levels)
    opus_result["veredicto"] = decision["veredicto"]
    opus_result["calidad_señal"] = decision["calidad"]
    if decision["modo"]:
        opus_result["modo_entrada"] = decision["modo"]
    trade = decision["trade"]

    r_in, r_out = sg.RATES.get(OPUS_MODEL, (15, 75))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000

    print(f"\n{'='*70}")
    print(f"  COMPARACIÓN L3 — {date}")
    print(f"{'='*70}")
    print(f"\n  {'SONNET (producción)':35} {'OPUS (test)':35}")
    print(f"  {'-'*33} {'-'*33}")
    print(f"  Veredicto: {sonnet_result.get('veredicto',''):24} Veredicto: {decision['veredicto']}")
    print(f"  Modo:      {sonnet_result.get('modo_entrada',''):24} Modo:      {opus_result.get('modo_entrada','')}")
    print(f"  Calidad:   {sonnet_result.get('calidad_señal',''):24} Calidad:   {decision['calidad']}")
    print(f"  Checklist: {sonnet_result.get('checklist',{}).get('cumplidas','')}/3"
          f"{'':22} Checklist: {opus_result.get('checklist',{}).get('cumplidas','')}/3")

    print(f"\n  --- Sub-onda SONNET ---")
    print(f"  {sonnet_result.get('lectura_estructural',{}).get('sub_onda_actual','')}")
    print(f"\n  --- Sub-onda OPUS ---")
    print(f"  {opus_result.get('lectura_estructural',{}).get('sub_onda_actual','')}")

    print(f"\n  --- Razón SONNET ---")
    print(f"  {sonnet_result.get('razon','')}")
    print(f"\n  --- Razón OPUS ---")
    print(f"  {opus_result.get('razon','')}")

    if trade:
        print(f"\n  --- PLAN DE TRADE (OPUS) ---")
        print(f"  Entrada: ${trade['entrada']:,}  Stop: ${trade['stop']:,}")
        print(f"  O1: ${trade['O1']:,} ({trade['R:R']['O1']}x)  "
              f"O2: ${trade['O2']:,} ({trade['R:R']['O2']}x)  "
              f"O3: ${trade['O3']:,} ({trade['R:R']['O3']}x)")

    print(f"\n  Tokens Opus: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")

    out_file = Path(__file__).parent.parent / "data" / "compare_l3_opus_result.json"
    out_file.write_text(json.dumps({"sonnet": sonnet_result, "opus": opus_result, "trade_opus": trade},
                                    ensure_ascii=False, indent=2))
    print(f"\n  Guardado en: {out_file}")


if __name__ == "__main__":
    main()
