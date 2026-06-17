import anthropic
import yfinance as yf
import pandas as pd
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

MODEL = "claude-opus-4-8"
PROMPT_PATH = Path(__file__).parent / "prompts" / "level1_weekly.txt"
OUTPUT_DIR  = Path(__file__).parent.parent / "data"
OUTPUT_FILE = OUTPUT_DIR / "l1_btc_latest.json"

# ── API key ───────────────────────────────────────────────────────────────────

def clean_api_key() -> str:
    """Extrae solo el token sk-ant-... aunque venga con etiquetas o saltos de línea."""
    raw = os.environ.get("ANTHROPIC_API_KEY", "")
    match = re.search(r"sk-ant-[A-Za-z0-9_\-]+", raw)
    if not match:
        sys.exit("ERROR: no se encontró una API key válida (sk-ant-...) en ANTHROPIC_API_KEY")
    return match.group(0)

# ── Datos ─────────────────────────────────────────────────────────────────────

def fetch_weekly_df(ticker: str = "BTC-USD", asof: str | None = None) -> pd.DataFrame:
    """Devuelve el DataFrame semanal con índice datetime (para cálculos).
    Construye las semanas desde DIARIAS filtradas al asof, para que la última semana
    (parcial) NO incluya datos posteriores al asof — sin fuga de futuro en backtesting."""
    d = yf.Ticker(ticker).history(period="max", interval="1d")
    d.index = d.index.tz_localize(None) if d.index.tz else d.index
    if asof:
        d = d[d.index <= pd.Timestamp(asof)]
    d = d[d.index >= "2014-01-01"]
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    wk = d.resample("W-MON", label="left", closed="left").agg(agg).dropna()
    return wk[["Open", "High", "Low", "Close", "Volume"]]

def df_to_csv(df: pd.DataFrame) -> str:
    """Convierte a CSV con fechas legibles (para el prompt)."""
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%d")
    return out.to_csv()

def fetch_weekly_data(ticker: str = "BTC-USD") -> str:
    """Compatibilidad: CSV directo (usado por el test de consistencia)."""
    return df_to_csv(fetch_weekly_df(ticker))

# ── Fibonacci determinista ──────────────────────────────────────────────────────

def compute_operative_levels(df: pd.DataFrame, techo_precio: float,
                             techo_fecha: str, tipo: str) -> dict:
    """Calcula los niveles Fibonacci del tramo más reciente desde el techo operativo.
    Opus identifica los pivotes; Python hace la aritmética (determinista)."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", techo_fecha or "")
    if not m:
        return {}
    after = df[df.index > pd.Timestamp(m.group(0))]
    if after.empty:
        return {}

    es_techo = "mín" not in (tipo or "").lower()  # default: techo = máximo
    if es_techo:
        opp = float(after["Low"].min())            # mínimo operativo tras el techo
        signo = 1                                   # retrocesos (rebotes) hacia arriba
    else:
        opp = float(after["High"].max())           # máximo operativo tras el suelo
        signo = -1                                  # retrocesos hacia abajo

    rango = abs(techo_precio - opp)
    fib = {pct: round(opp + signo * rango * pct, 2) for pct in (0.382, 0.5, 0.618)}
    return {
        "extremo_opuesto": round(opp, 2),
        "rango": round(rango, 2),
        "retroceso_382": fib[0.382],
        "retroceso_50": fib[0.5],
        "retroceso_618": fib[0.618],
    }

# ── API ───────────────────────────────────────────────────────────────────────

def call_model(prompt: str) -> tuple[dict, object]:
    client = anthropic.Anthropic(api_key=clean_api_key(), base_url="https://api.anthropic.com", max_retries=8)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    return json.loads(text[start:end]), response.usage

# ── Reporte ───────────────────────────────────────────────────────────────────

def print_report(result: dict, usage) -> None:
    techo = result.get("techo_operativo", {})
    escenarios = result.get("escenarios", [])
    acuerdo = result.get("acuerdo", {})
    div = result.get("divergencia", {})
    l2 = result.get("niveles_para_l2", {})
    rates = {"claude-opus-4-8": (15, 75), "claude-sonnet-4-6": (3, 15)}
    r_in, r_out = rates.get(MODEL, (3, 15))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000

    print(f"\n{'='*64}")
    print(f"  SIGMAVIEW L1 — {result.get('fecha_analisis', '')}")
    print(f"{'='*64}")
    print(f"  Techo operativo:  ${techo.get('precio', ''):,} ({techo.get('fecha', '')}) — {techo.get('tipo', '')}")

    print(f"\n  ESCENARIOS VÁLIDOS ({len(escenarios)}):")
    for e in escenarios:
        print(f"\n  [{e.get('id', '')}] {e.get('etiqueta_macro', '')}")
        print(f"      Sesgo CP:    {e.get('sesgo_corto_plazo', '')}  |  Objetivo: {e.get('objetivo', '')}")
        print(f"      Posición:    {e.get('posicion_hoy', '')}")
        print(f"      Confirma si: {e.get('se_confirma_si', '')}")
        print(f"      Invalida si: {e.get('se_invalida_si', '')}")

    elim = result.get("escenarios_eliminados", [])
    if elim:
        print(f"\n  ELIMINADOS POR REGLA ({len(elim)}):")
        for x in elim:
            print(f"    ✗ {x.get('descripcion', '')} — viola {x.get('regla_violada', '')}")

    print(f"\n  ACUERDO (accionable hoy):")
    print(f"    Sesgo cercano: {acuerdo.get('sesgo_cercano', '')}")
    print(f"    Niveles clave: {acuerdo.get('niveles_compartidos', '')}")
    print(f"    Cómo operar:   {acuerdo.get('como_operar_hoy', '')}")

    print(f"\n  DIVERGENCIA:")
    print(f"    Pregunta:    {div.get('pregunta_abierta', '')}")
    print(f"    Se resuelve: {div.get('se_resuelve_si', '')}")

    fib_src = l2.get("_fib_calculado_por", "modelo")
    print(f"\n  Niveles para L2 (tramo techo→mínimo reciente, Fib por {fib_src}):")
    print(f"    Techo:            ${techo.get('precio', ''):,}")
    print(f"    Mínimo operativo: ${l2.get('low_operativo', ''):,}")
    print(f"    Retroceso 38.2%:  ${l2.get('retroceso_382', ''):,}")
    print(f"    Retroceso 50.0%:  ${l2.get('retroceso_50', ''):,}")
    print(f"    Retroceso 61.8%:  ${l2.get('retroceso_618', ''):,}")
    print(f"    Resolutorios:     {l2.get('niveles_resolutorios', '')}")

    print(f"\n  {result.get('resumen', '')}")
    print(f"\n  Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")

# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> dict:
    OUTPUT_DIR.mkdir(exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")

    print("Bajando datos BTC semanales...", end=" ", flush=True)
    df = fetch_weekly_df()
    price_csv = df_to_csv(df)
    print(f"OK ({len(df)} velas)")

    template = PROMPT_PATH.read_text()
    prompt   = template.format(price_data=price_csv, date=date)

    print(f"Llamando a {MODEL} (L1)...", end=" ", flush=True)
    result, usage = call_model(prompt)
    print("OK")

    # Fibonacci determinista: Opus da los pivotes, Python hace la aritmética
    techo = result.get("techo_operativo", {})
    fib = compute_operative_levels(
        df, float(techo.get("precio", 0)), techo.get("fecha", ""), techo.get("tipo", "")
    )
    if fib:
        l2 = result.setdefault("niveles_para_l2", {})
        l2.update({
            "low_operativo": fib["extremo_opuesto"],
            "retroceso_382": fib["retroceso_382"],
            "retroceso_50": fib["retroceso_50"],
            "retroceso_618": fib["retroceso_618"],
            "_fib_calculado_por": "python",
        })

    print_report(result, usage)

    result["_meta"] = {"generado": date, "modelo": MODEL}
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n  Guardado en: {OUTPUT_FILE}")

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import database
        database.log_l1(date, "BTC-USD", result)
        print("  L1 guardado en DB.")
    except Exception as e:
        print(f"  (db log_l1 error: {e})")

    return result

if __name__ == "__main__":
    run()
