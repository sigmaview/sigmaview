"""Corre L3 con Sonnet y con Opus sobre los MISMOS datos y compara la lectura estructural.
Uso: ANTHROPIC_API_KEY="sk-ant-..." python3 tests/test_l3_sonnet_vs_opus.py
"""
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import signal_generator as l3

MODELS = ["claude-sonnet-4-6", "claude-opus-4-8"]

def main() -> None:
    date = datetime.now().strftime("%Y-%m-%d")
    print("Cargando contexto L1/L2 y bajando datos 4h (una vez)...", end=" ", flush=True)
    prompt, _, _ = l3.build_l3_prompt(date)
    print("OK\n")

    rows = []
    for model in MODELS:
        print(f"Llamando a {model}...", end=" ", flush=True)
        result, usage = l3.call_model(prompt, model)
        r_in, r_out = l3.RATES.get(model, (15, 75))
        cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
        le = result.get("lectura_estructural", {})
        rows.append({
            "model": model,
            "sub_onda": le.get("sub_onda_actual", ""),
            "grado": le.get("grado", ""),
            "estado": le.get("estado", ""),
            "que_esperar": le.get("que_esperar", ""),
            "direccion": result.get("direccion", ""),
            "veredicto": result.get("veredicto", ""),
            "cost": cost,
        })
        print("OK")

    print(f"\n{'='*78}")
    print(f"  L3 — SONNET vs OPUS (mismos datos)")
    print(f"{'='*78}")
    for r in rows:
        print(f"\n  [{r['model']}]  (${r['cost']:.4f})")
        print(f"    Sub-onda:    {r['sub_onda']}")
        print(f"    Grado:       {r['grado']}")
        print(f"    Estado:      {r['estado']}")
        print(f"    Qué esperar: {r['que_esperar']}")
        print(f"    Dirección:   {r['direccion']}  |  Veredicto: {r['veredicto']}")

    print(f"\n{'='*78}")
    print("  ¿Coinciden Sonnet y Opus en la sub-onda actual? Júzgalo arriba.")
    print(f"  Ahorro usando Sonnet: ${rows[1]['cost'] - rows[0]['cost']:.4f} por llamada")

if __name__ == "__main__":
    main()
