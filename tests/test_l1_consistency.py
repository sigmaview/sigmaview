"""Corre L1 N veces y compara la estabilidad del conteo entre corridas.
Uso: ANTHROPIC_API_KEY="sk-ant-..." python3 tests/test_l1_consistency.py [N]
"""
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import analyzer_weekly as l1

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

def run_once(template: str, price_csv: str, date: str) -> dict:
    prompt = template.format(price_data=price_csv, date=date)
    result, usage = l1.call_model(prompt)
    cost = (usage.input_tokens * 15 + usage.output_tokens * 75) / 1_000_000
    return {
        "techo_operativo": result.get("techo_operativo", {}).get("precio"),
        "n_escenarios": len(result.get("escenarios", [])),
        "sesgo_acuerdo": result.get("acuerdo", {}).get("sesgo_cercano", "")[:45],
        "extremo": result.get("niveles_para_l2", {}).get("extremo_impulso", {}).get("precio"),
        "retroceso_382": result.get("niveles_para_l2", {}).get("retroceso_382"),
        "cost": cost,
    }

def main() -> None:
    date = datetime.now().strftime("%Y-%m-%d")
    print(f"Bajando datos BTC semanales...", end=" ", flush=True)
    price_csv = l1.fetch_weekly_data()
    template = l1.PROMPT_PATH.read_text()
    print("OK")

    runs = []
    for i in range(N):
        print(f"Corrida {i+1}/{N}...", end=" ", flush=True)
        runs.append(run_once(template, price_csv, date))
        print("OK")

    print(f"\n{'='*78}")
    print(f"  CONSISTENCIA L1 — {N} corridas")
    print(f"{'='*78}")
    cols = ["techo_operativo", "extremo", "retroceso_382", "sesgo_acuerdo", "n_escenarios"]
    for col in cols:
        vals = [str(r[col]) for r in runs]
        estable = "✅ ESTABLE" if len(set(vals)) == 1 else "⚠ VARÍA"
        print(f"\n  {col}:  {estable}")
        for i, v in enumerate(vals):
            print(f"    corrida {i+1}: {v}")

    total_cost = sum(r["cost"] for r in runs)
    print(f"\n{'='*78}")
    print(f"  Costo total {N} corridas: ${total_cost:.4f}")

if __name__ == "__main__":
    main()
