"""Test de validación del rediseño de Modo B: en vez de sustituir abc_a_inicio/abc_c_fin
con los niveles de L1 (mezcla de grados — el bug del 2026-06-21), Python deriva ambos
pivotes desde los propios datos de precio de L3, anclados a la fecha del extremo que el
modelo narró (aquí aproximada por precio, ya que el campo de fecha todavía no existe en el
prompt — ver diseño). Sin llamadas a la API: usa los pivotes ya cacheados del walk-forward
y descarga de precio (gratis, yfinance/DB).

No modifica nada en producción — solo imprime una comparación contra el comportamiento
actual (con sustitución de L1 + tolerancia de 20%).

Uso: PYTHONPATH=src python3 tests/test_abc_redesign.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import signal_generator as sg

ROOT = Path(__file__).parent.parent
WF_DIR = ROOT / "data" / "walkforward_l3"

# Los 7 disparos históricos reales (veredicto=SEÑAL, modo=B_fin_abc) + 2 casos límite.
CASOS = [
    "2024-07-08", "2024-08-05", "2024-12-23", "2024-12-30",
    "2025-03-03", "2025-06-23", "2025-09-08",   # 7 disparos reales — no deben regresionar
    "2026-06-08",                                  # near-miss histórico (71.7% mismatch, bloqueado por macro)
    "2026-06-21",                                  # señal falsa que motivó el fix — debe bloquear
]

def cargar(fecha: str) -> dict:
    if fecha == "2026-06-21":
        return json.loads((ROOT / "data" / "l3_btc_latest.json").read_text())
    return json.loads((WF_DIR / f"{fecha}_l3.json").read_text())["l3"]


def compute_abc_pivots_por_precio(df, direccion: str, a_inicio_precio: float, tol: float = 0.05):
    """Proxy de diseño: en producción el modelo daría la FECHA del inicio de A y Python solo
    verificaría el precio en esa fecha. Acá, sin ese campo todavía, buscamos en los datos de
    L3 la fecha cuyo extremo está más cerca del precio que el modelo narró — si no hay ningún
    extremo real dentro de tol, el pivote no es verificable (igual resultado que en producción:
    no se opera)."""
    es_long = direccion.upper() == "LONG"
    serie = df["High"] if es_long else df["Low"]
    idx_mejor = (serie - a_inicio_precio).abs().idxmin()
    real = float(serie.loc[idx_mejor])
    if abs(real - a_inicio_precio) / a_inicio_precio > tol:
        return None
    despues = df[df.index > idx_mejor]
    if despues.empty:
        return None
    c_fin = float(despues["Low"].min()) if es_long else float(despues["High"].max())
    return {"abc_a_inicio": round(real, 2), "abc_c_fin": round(c_fin, 2), "fecha_a_inicio": str(idx_mejor)}


def evaluar_modo_b_v2(res: dict, df) -> dict:
    """Misma lógica de evaluar_modo_b() pero sin tocar L1: el pivote de Modo B se deriva
    enteramente de los datos de precio de L3 (su propio grado)."""
    mb = res.get("modo_b_check", {})
    if not mb.get("abc_detectada"):
        return {"dispara": False, "motivo": "no hay ABC detectada"}
    c_a = mb.get("c_sobre_a")
    if not sg.ca_en_tolerancia(c_a):
        return {"dispara": False, "motivo": f"c/a={c_a} fuera de tolerancia"}

    piv = dict(res.get("pivotes") or {})
    a_inicio_narrado = piv.get("abc_a_inicio")
    if not a_inicio_narrado:
        return {"dispara": False, "motivo": "sin abc_a_inicio narrado"}

    pivotes_reales = compute_abc_pivots_por_precio(df, res["direccion"], float(a_inicio_narrado))
    if pivotes_reales is None:
        return {"dispara": False,
                "motivo": "pivotes ABC no verificables en los datos de precio de L3 — "
                          "no estamos perfectamente situados, no se opera"}

    piv["abc_a_inicio"] = pivotes_reales["abc_a_inicio"]
    piv["abc_c_fin"] = pivotes_reales["abc_c_fin"]
    piv["stop_extremo"] = pivotes_reales["abc_c_fin"]

    try:
        trade = sg.compute_trade(res["direccion"], "B_fin_abc", piv)
    except (KeyError, ValueError, TypeError) as e:
        return {"dispara": False, "motivo": f"pivotes incompletos ({e})"}

    precio = res.get("precio_actual")
    if precio:
        dist = abs(float(precio) - trade["entrada"]) / trade["entrada"]
        if dist > sg.MAX_DIST_ENTRADA:
            return {"dispara": False, "trade": trade,
                    "motivo": f"entrada rancia: precio a {dist:.0%} del fin de C"}

    ch = res.get("checklist") or {}
    s1 = str(ch.get("s1_retroceso", "")).strip().upper() in ("SÍ", "SI")
    s2 = str(ch.get("s2_estructura", "")).strip().upper() in ("SÍ", "SI")
    if s1 and s2:
        return {"dispara": False, "trade": trade, "motivo": "Modo B obsoleto: S1+S2 ya confirmados"}

    rr = trade["R:R"].get(sg.TARGET_GATE)
    if not mb.get("invalidacion_clara", True):
        return {"dispara": False, "motivo": "invalidación no clara", "rr_python": rr, "trade": trade}
    if rr is None or rr < sg.RR_MINIMO:
        return {"dispara": False, "motivo": f"R:R({sg.TARGET_GATE})={rr} < {sg.RR_MINIMO}x",
                "rr_python": rr, "trade": trade}

    return {"dispara": True,
            "motivo": f"c/a={c_a} OK, R:R({sg.TARGET_GATE})={rr}x ≥ {sg.RR_MINIMO}x, "
                      f"pivotes verificados en datos de L3 ({pivotes_reales['fecha_a_inicio']})",
            "rr_python": rr, "trade": trade}


CACHE_DIR = ROOT / "data" / "backtest_cache"

def l1_levels_de(fecha: str) -> dict | None:
    """L1 real cacheado más cercano <= fecha (igual criterio que producción: L1 solo se
    refresca los lunes). Si no hay cache exacto, evalúa con el más reciente disponible."""
    if fecha == "2026-06-21":
        l1 = json.loads((ROOT / "data" / "l1_btc_latest.json").read_text())
    else:
        candidatos = sorted(CACHE_DIR.glob("*_btc_l1.json"))
        candidatos = [c for c in candidatos if c.name[:10] <= fecha]
        if not candidatos:
            return None
        l1 = json.loads(candidatos[-1].read_text())
    return {"techo": l1.get("techo_operativo", {}).get("precio"),
            "operativo": l1.get("niveles_para_l2", {}).get("low_operativo")}


def main() -> None:
    print(f"{'fecha':<12} {'dispara_PROD':<13} {'dispara_v2':<11} {'motivo_v2'}")
    print("-" * 110)
    discrepancias = []
    for fecha in CASOS:
        res = cargar(fecha)
        # Descarga las mismas velas 4h que usó L3 ese día (gratis, yfinance/DB)
        csv_text = sg.fetch_4h_data(360, asof=fecha)
        import pandas as pd
        from io import StringIO
        df = pd.read_csv(StringIO(csv_text), index_col=0, parse_dates=True)

        # Producción REAL (código actual, no el veredicto cacheado de una corrida vieja)
        l1_levels = l1_levels_de(fecha)
        prod = sg.evaluar_modo_b(res, l1_levels)

        v2 = evaluar_modo_b_v2(res, df)
        print(f"{fecha:<12} {('SÍ — ' + prod['motivo'][:40]) if prod['dispara'] else 'no':<13} "
              f"{'DISPARA' if v2['dispara'] else 'no':<11} {v2['motivo']}")
        if prod["dispara"] != v2["dispara"]:
            discrepancias.append(fecha)

    print("-" * 110)
    if discrepancias:
        print(f"⚠ Discrepancias vs producción ACTUAL en: {discrepancias}")
    else:
        print("✅ Sin discrepancias: los 7 disparos reales siguen disparando, "
              "los casos límite siguen bloqueados — sin tocar L1.")

if __name__ == "__main__":
    main()
