"""Piloto: ¿aporta valor bajar a velas 1h para leer la sub-estructura del rebote
desde el mínimo, y así resolver la ambigüedad Escenario A vs B?
NO se integra al pipeline de producción — es un experimento aislado.

Uso: ANTHROPIC_API_KEY=sk-ant-... PYTHONPATH=src python3 src/pilot_microstructure.py
"""
import json
import os
import re
import sys
from pathlib import Path

import anthropic
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import database

MODEL       = "claude-opus-4-8"
TICKER      = "BTC-USD"
DATA_DIR    = Path(__file__).parent.parent / "data"
PROMPT_PATH = Path(__file__).parent / "prompts" / "pilot_microstructure.txt"

LOW_PRICE = 59096
LOW_DATE  = "2026-06-05"
FETCH_FROM = "2026-06-05 18:00"   # exactamente la vela del mínimo — evita que el modelo tome
                                   # un pivote previo (parte de la caída) como si fuera "onda 1"


def clean_api_key() -> str:
    raw = os.environ.get("ANTHROPIC_API_KEY", "")
    match = re.search(r"sk-ant-[A-Za-z0-9_\-]+", raw)
    if not match:
        sys.exit("ERROR: no se encontró una API key válida (sk-ant-...) en ANTHROPIC_API_KEY")
    return match.group(0)


def fetch_1h_csv(ticker: str, since: str) -> tuple[str, int]:
    conn = database._conn()
    df = pd.read_sql_query(
        "SELECT ts,open,high,low,close FROM ohlcv WHERE ticker=? AND ts >= ? ORDER BY ts",
        conn, params=[ticker, since],
    )
    conn.close()
    return df.to_csv(index=False), len(df)


def call_model(prompt: str) -> tuple[dict, object]:
    client = anthropic.Anthropic(api_key=clean_api_key(), base_url="https://api.anthropic.com", max_retries=8)
    response = client.messages.create(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    start, end = text.find("{"), text.rfind("}") + 1
    return json.loads(text[start:end]), response.usage


def run() -> dict:
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")

    l1 = json.loads((DATA_DIR / "l1_btc_latest.json").read_text())
    l3 = json.loads((DATA_DIR / "l3_btc_latest.json").read_text())

    contexto_l1 = (
        f"Techo operativo: ${l1['techo_operativo']['precio']:,} ({l1['techo_operativo']['fecha']})\n"
        f"Acuerdo: {l1['acuerdo']['sesgo_cercano']}\n"
        f"Divergencia: {l1['divergencia']['pregunta_abierta']}"
    )
    contexto_l3 = (
        f"Veredicto: {l3.get('veredicto')} | Dirección: {l3.get('direccion')} | "
        f"Sub-onda: {l3.get('lectura_estructural', {}).get('sub_onda_actual')}\n"
        f"Plan de trade: entrada {l3.get('plan_trade', {}).get('entrada')}, "
        f"stop {l3.get('plan_trade', {}).get('stop')}"
    )

    price_csv, n = fetch_1h_csv(TICKER, FETCH_FROM)
    print(f"Velas 1h desde {FETCH_FROM}: {n}")

    template = PROMPT_PATH.read_text()
    prompt = template.format(
        contexto_l1=contexto_l1,
        contexto_l3=contexto_l3,
        precio_minimo=f"${LOW_PRICE:,}",
        fecha_minimo=LOW_DATE,
        price_data=price_csv,
        date=date,
    )

    print(f"Llamando a {MODEL}...", end=" ", flush=True)
    result, usage = call_model(prompt)
    print("OK")

    r_in, r_out = 15, 75
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
    print(f"\nTokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    out_file = DATA_DIR / "pilot_microstructure_result.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nGuardado en: {out_file}")

    return result


if __name__ == "__main__":
    run()
