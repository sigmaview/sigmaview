"""Validación real (con llamadas a Opus) del rediseño de Modo B: el modelo narra la FECHA
del inicio de la corrección (abc_a_inicio_fecha) además del precio; Python verifica esa
fecha contra los datos reales (ventana amplia, no limitada a las 360 velas del prompt) y
deriva el fin de C como el extremo opuesto posterior — sin tocar L1 en ningún momento.

No modifica nada en producción: usa una copia en memoria del prompt (no escribe el archivo
real) y NO usa los pivotes/precio cacheados — fuerza una lectura fresca de Opus por fecha,
exactamente como pasaría en producción con el prompt actualizado.

Costo estimado: ~9 llamadas a Opus, ~$0.30-0.50 cada una (~$3-5 total).
Uso: ANTHROPIC_API_KEY="sk-ant-..." PYTHONPATH=src python3 tests/test_abc_redesign_v2.py
"""
import json
import re
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import signal_generator as sg

ROOT = Path(__file__).parent.parent
CACHE_DIR = ROOT / "data" / "backtest_cache"
DATA_DIR = ROOT / "data"

CASOS = [
    "2024-07-08", "2024-08-05", "2024-12-23", "2024-12-30",
    "2025-03-03", "2025-06-23", "2025-09-08",
    "2026-06-08", "2026-06-21",
]

# ── Prompt modificado (solo en memoria — no se escribe el archivo real) ────────────────
PROMPT_BASE = (ROOT / "src" / "prompts" / "level3_modoB_fuerte.txt").read_text()
PROMPT_TEST = PROMPT_BASE.replace(
    'b) Calcula el ratio c/a con los precios (muéstralo).',
    'b) Calcula el ratio c/a con los precios (muéstralo).\n'
    'b.1) Indica la FECHA exacta (YYYY-MM-DD) de la vela donde se originó la onda A '
    '(el techo/suelo desde el que arranca la corrección ABC). Python verificará ese '
    'pivote contra los datos reales — sé preciso, no aproximes.'
).replace(
    '"abc_a_inicio": <precio o null>, "abc_c_fin": <precio o null>,',
    '"abc_a_inicio": <precio o null>, "abc_a_inicio_fecha": "<YYYY-MM-DD o null>",\n'
    '    "abc_c_fin": <precio o null>,'
)
assert PROMPT_TEST != PROMPT_BASE, "el patch del prompt no encontró el texto esperado"
assert 'abc_a_inicio_fecha' in PROMPT_TEST, "el campo de fecha no quedó en el schema del prompt"


def build_prompt_test(l1: dict, l2: dict, price_csv: str, date: str) -> str:
    """Igual que sg.build_l3_prompt_from(), pero usando PROMPT_TEST en vez del archivo real."""
    ctx_l1 = json.dumps({k: l1.get(k) for k in ("techo_operativo", "escenarios", "acuerdo",
                                                 "divergencia", "niveles_para_l2")}, ensure_ascii=False)
    ctx_l2 = json.dumps({k: l2.get(k) for k in ("escenario_favorecido", "resolutorios_cruzados",
                                                 "score_santos", "resumen")}, ensure_ascii=False)
    return PROMPT_TEST.format(
        asset=sg.ASSET, date=date,
        fecha_l1=l1.get("fecha_analisis", "?"), contexto_l1=ctx_l1,
        fecha_l2=l2.get("fecha", "?"), contexto_l2=ctx_l2,
        candle_count=sg.CANDLE_COUNT, price_data=price_csv,
        timeframe_label="4 HORAS",
    )


def cargar_l1l2(fecha: str) -> tuple[dict, dict]:
    if fecha == "2026-06-21":
        l1 = json.loads((DATA_DIR / "l1_btc_latest.json").read_text())
        l2 = json.loads((DATA_DIR / "l2_btc_latest.json").read_text())
        return l1, l2
    l1 = json.loads((CACHE_DIR / f"{fecha}_btc_l1.json").read_text())
    l2 = json.loads((CACHE_DIR / f"{fecha}_btc_l2.json").read_text())
    return l1, l2


def fetch_df_amplio(asof: str) -> pd.DataFrame:
    """Ventana de verificación AMPLIA (2 años de velas diarias) — independiente de las 360
    velas 4h que ve el modelo en el prompt. Evita el problema del 2024-08-05: un pivote real
    pero anterior a la ventana corta del prompt."""
    df = yf.Ticker("BTC-USD").history(period="2y" if asof >= "2024-01-01" else "max", interval="1d")
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    df = df[df.index < pd.Timestamp(asof) + pd.Timedelta(days=1)]
    return df


def verificar_pivote(df_amplio: pd.DataFrame, direccion: str, fecha_narrada: str | None,
                      precio_narrado: float | None, tol: float = 0.05):
    if not fecha_narrada or not precio_narrado:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", fecha_narrada)
    if not m:
        return None
    fecha = pd.Timestamp(m.group(0))
    es_long = direccion.upper() == "LONG"
    ventana = df_amplio[(df_amplio.index >= fecha - pd.Timedelta(days=3)) &
                         (df_amplio.index <= fecha + pd.Timedelta(days=3))]
    if ventana.empty:
        return None
    real = float(ventana["High"].max()) if es_long else float(ventana["Low"].min())
    if abs(real - float(precio_narrado)) / float(precio_narrado) > tol:
        return {"ok": False, "real": real, "razon": f"precio narrado ${precio_narrado:,.0f} vs "
                f"extremo real ${real:,.0f} cerca de {fecha.date()} — no corresponde"}
    despues = df_amplio[df_amplio.index > fecha]
    if despues.empty:
        return None
    c_fin = float(despues["Low"].min()) if es_long else float(despues["High"].max())
    return {"ok": True, "abc_a_inicio": round(real, 2), "abc_c_fin": round(c_fin, 2)}


def evaluar_v2(res: dict, df_amplio: pd.DataFrame) -> dict:
    mb = res.get("modo_b_check", {})
    if not mb.get("abc_detectada"):
        return {"dispara": False, "motivo": "no hay ABC detectada"}
    c_a = mb.get("c_sobre_a")
    if not sg.ca_en_tolerancia(c_a):
        return {"dispara": False, "motivo": f"c/a={c_a} fuera de tolerancia"}

    piv = dict(res.get("pivotes") or {})
    v = verificar_pivote(df_amplio, res["direccion"], piv.get("abc_a_inicio_fecha"),
                         piv.get("abc_a_inicio"))
    if v is None:
        return {"dispara": False, "motivo": "sin fecha narrada o fecha fuera de los datos disponibles"}
    if not v["ok"]:
        return {"dispara": False, "motivo": v["razon"]}

    piv["abc_a_inicio"] = v["abc_a_inicio"]
    piv["abc_c_fin"] = v["abc_c_fin"]
    piv["stop_extremo"] = v["abc_c_fin"]

    try:
        trade = sg.compute_trade(res["direccion"], "B_fin_abc", piv)
    except (KeyError, ValueError, TypeError) as e:
        return {"dispara": False, "motivo": f"pivotes incompletos ({e})"}

    precio = res.get("precio_actual")
    if precio:
        dist = abs(float(precio) - trade["entrada"]) / trade["entrada"]
        if dist > sg.MAX_DIST_ENTRADA:
            return {"dispara": False, "trade": trade, "motivo": f"entrada rancia ({dist:.0%})"}

    ch = res.get("checklist") or {}
    s1 = str(ch.get("s1_retroceso", "")).strip().upper() in ("SÍ", "SI")
    s2 = str(ch.get("s2_estructura", "")).strip().upper() in ("SÍ", "SI")
    if s1 and s2:
        return {"dispara": False, "trade": trade, "motivo": "Modo B obsoleto: S1+S2 ya confirmados"}

    rr = trade["R:R"].get(sg.TARGET_GATE)
    if not mb.get("invalidacion_clara", True):
        return {"dispara": False, "motivo": "invalidación no clara", "trade": trade}
    if rr is None or rr < sg.RR_MINIMO:
        return {"dispara": False, "motivo": f"R:R({sg.TARGET_GATE})={rr} < {sg.RR_MINIMO}x", "trade": trade}

    return {"dispara": True,
            "motivo": f"c/a={c_a} OK, R:R({sg.TARGET_GATE})={rr}x ≥ {sg.RR_MINIMO}x, "
                      f"pivote verificado en ${v['abc_a_inicio']:,.0f}",
            "trade": trade}


def l1_levels_de(fecha: str, l1: dict) -> dict:
    return {"techo": l1.get("techo_operativo", {}).get("precio"),
            "operativo": l1.get("niveles_para_l2", {}).get("low_operativo")}


def main() -> None:
    costo_total = 0.0
    filas = []
    for fecha in CASOS:
        l1, l2 = cargar_l1l2(fecha)
        price_csv = sg.fetch_4h_data(360, asof=fecha)
        prompt = build_prompt_test(l1, l2, price_csv, fecha)

        print(f"\n[{fecha}] llamando a Opus...", end=" ", flush=True)
        result, usage = sg.call_model(prompt, "claude-opus-4-8")
        r_in, r_out = sg.RATES["claude-opus-4-8"]
        costo = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
        costo_total += costo
        print(f"OK (${costo:.3f}, acumulado ${costo_total:.3f})")

        piv = result.get("pivotes", {})
        print(f"  abc_a_inicio=${piv.get('abc_a_inicio')} fecha={piv.get('abc_a_inicio_fecha')} "
              f"abc_c_fin=${piv.get('abc_c_fin')} direccion={result.get('direccion')}")

        df_amplio = fetch_df_amplio(fecha)
        v2 = evaluar_v2(result, df_amplio)
        prod = sg.evaluar_modo_b(result, l1_levels_de(fecha, l1))
        # Nota: prod usa la lectura FRESCA de hoy (no la cacheada históricamente), por lo que
        # no es 100% comparable a la decisión histórica real — pero sí muestra si, para ESTA
        # narración del modelo, L1 (sustitución) y la verificación de fecha (v2) coinciden.
        filas.append((fecha, prod["dispara"], prod["motivo"], v2["dispara"], v2["motivo"]))

    print(f"\n{'='*110}\nCosto total: ${costo_total:.3f} USD\n{'='*110}")
    print(f"{'fecha':<12} {'PROD(L1)':<10} {'v2(fecha)':<10} motivo_v2")
    print("-" * 110)
    discrepancias = []
    for fecha, p_disp, p_mot, v_disp, v_mot in filas:
        print(f"{fecha:<12} {'SÍ' if p_disp else 'no':<10} {'SÍ' if v_disp else 'no':<10} {v_mot}")
        if p_disp != v_disp:
            discrepancias.append(fecha)
    print("-" * 110)
    if discrepancias:
        print(f"⚠ Discrepancias PROD vs v2: {discrepancias}")
    else:
        print("✅ Sin discrepancias.")

if __name__ == "__main__":
    main()
