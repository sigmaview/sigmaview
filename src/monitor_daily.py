import anthropic
import yfinance as yf
import pandas as pd
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ASSET = "BTC/USD"
TICKER = "BTC-USD"
CANDLE_COUNT = 180
MODEL = "claude-sonnet-4-6"
PROMPT_PATH = Path(__file__).parent / "prompts" / "level2_daily.txt"
DATA_DIR = Path(__file__).parent.parent / "data"
L1_FILE = DATA_DIR / "l1_btc_latest.json"
OUTPUT_FILE = DATA_DIR / "l2_btc_latest.json"

# ── API key ───────────────────────────────────────────────────────────────────

def clean_api_key() -> str:
    raw = os.environ.get("ANTHROPIC_API_KEY", "")
    match = re.search(r"sk-ant-[A-Za-z0-9_\-]+", raw)
    if not match:
        sys.exit("ERROR: no se encontró una API key válida (sk-ant-...) en ANTHROPIC_API_KEY")
    return match.group(0)

# ── Datos ─────────────────────────────────────────────────────────────────────

def fetch_daily_data(candles: int, asof: str | None = None) -> str:
    df = yf.Ticker(TICKER).history(period="max", interval="1d")
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    if asof:
        df = df[df.index <= pd.Timestamp(asof)]
    df = df.tail(candles)[["Open", "High", "Low", "Close", "Volume"]]
    df.index = df.index.strftime("%Y-%m-%d")
    return df.to_csv()

def load_l1() -> dict:
    if not L1_FILE.exists():
        sys.exit(f"ERROR: no existe {L1_FILE}. Corre analyzer_weekly.py primero.")
    return json.loads(L1_FILE.read_text())

# ── Prompt ────────────────────────────────────────────────────────────────────

def format_escenarios(escenarios: list) -> str:
    out = []
    for e in escenarios:
        out.append(
            f"[{e.get('id','')}] {e.get('etiqueta_macro','')} | sesgo CP: {e.get('sesgo_corto_plazo','')}\n"
            f"    confirma si: {e.get('se_confirma_si','')}\n"
            f"    invalida si: {e.get('se_invalida_si','')}"
        )
    return "\n".join(out)

def build_prompt(template: str, l1: dict, price_csv: str, date: str) -> str:
    techo = l1.get("techo_operativo", {})
    l2n = l1.get("niveles_para_l2", {})
    acuerdo = l1.get("acuerdo", {})
    div = l1.get("divergencia", {})
    return template.format(
        asset=ASSET,
        date=date,
        fecha_l1=l1.get("fecha_analisis", "?"),
        techo=f"${techo.get('precio','')} ({techo.get('fecha','')})",
        minimo=f"${l2n.get('low_operativo','')}",
        escenarios=format_escenarios(l1.get("escenarios", [])),
        acuerdo=acuerdo.get("sesgo_cercano", "") + " — " + acuerdo.get("como_operar_hoy", ""),
        divergencia=div.get("pregunta_abierta", "") + " Se resuelve: " + div.get("se_resuelve_si", ""),
        retroceso_382=l2n.get("retroceso_382", ""),
        retroceso_50=l2n.get("retroceso_50", ""),
        retroceso_618=l2n.get("retroceso_618", ""),
        resolutorios=l2n.get("niveles_resolutorios", ""),
        candle_count=CANDLE_COUNT,
        price_data=price_csv,
    )

# ── API ───────────────────────────────────────────────────────────────────────

def call_model(prompt: str) -> tuple[dict, object]:
    client = anthropic.Anthropic(api_key=clean_api_key(), base_url="https://api.anthropic.com", max_retries=8)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    return json.loads(text[start:end]), response.usage

# ── Reporte ───────────────────────────────────────────────────────────────────

def print_report(r: dict, usage) -> None:
    cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
    nivel = r.get("nivel_alerta", "?")
    icon = {"NINGUNA": "✅", "VIGILAR": "🟡", "SEÑAL": "🔴"}.get(nivel, "•")

    print(f"\n{'='*60}")
    print(f"  L2 {r.get('activo','')} — {r.get('fecha','')}  |  ${r.get('precio_actual',''):,}")
    print(f"  {icon} {nivel}  (score Santos: {r.get('score_santos','')}/3.0)")
    print(f"{'='*60}")
    print(f"  S1 Retroceso:  {r.get('señal_1_retroceso',''):10} ({r.get('señal_1_score','')})  {r.get('señal_1_detalle','')}")
    print(f"  S2 Estructura: {r.get('señal_2_estructura',''):10} ({r.get('señal_2_score','')})  {r.get('señal_2_detalle','')}")
    print(f"  S3 Línea 2-4:  {r.get('señal_3_linea24',''):10} ({r.get('señal_3_score','')})  {r.get('señal_3_detalle','')}")

    cruzados = [x for x in r.get("resolutorios_cruzados", []) if x.get("cruzado")]
    if cruzados:
        print(f"\n  NIVELES RESOLUTORIOS CRUZADOS:")
        for x in cruzados:
            print(f"    ⚡ ${x.get('nivel','')} ({x.get('direccion','')}) → {x.get('implica','')}")

    print(f"\n  Escenario favorecido: {r.get('escenario_favorecido','')}")
    print(f"  {r.get('resumen','')}")
    print(f"\n  Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")

# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")

    l1 = load_l1()
    print(f"Mapa L1 cargado ({l1.get('fecha_analisis','?')})")

    print("Bajando datos BTC diarios...", end=" ", flush=True)
    price_csv = fetch_daily_data(CANDLE_COUNT)
    print(f"OK ({len(price_csv.splitlines())-1} velas)")

    template = PROMPT_PATH.read_text()
    prompt = build_prompt(template, l1, price_csv, date)

    print(f"Llamando a {MODEL} (L2)...", end=" ", flush=True)
    result, usage = call_model(prompt)
    print("OK")

    print_report(result, usage)

    result["_meta"] = {"generado": date, "modelo": MODEL, "l1_fecha": l1.get("fecha_analisis")}
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n  Guardado en: {OUTPUT_FILE}")

    if result.get("nivel_alerta") == "SEÑAL":
        print(f"\n{'!'*60}\n  🔴 SEÑAL — considera correr Nivel 3\n{'!'*60}")

    return result

if __name__ == "__main__":
    run()
