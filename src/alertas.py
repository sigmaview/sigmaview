"""Genera el plan de alertas (niveles a vigilar) desde el output de L1 y L3.
No usa IA — solo arma la lista de niveles que el price_watcher vigilará en tiempo real.
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
L1_FILE = DATA_DIR / "l1_btc_latest.json"
L3_FILE = DATA_DIR / "l3_btc_latest.json"
PLAN_FILE = DATA_DIR / "alertas_activas.json"
TICKER = "BTC-USD"

def _precio_mercado() -> float:
    import yfinance as yf
    df = yf.Ticker(TICKER).history(period="1d", interval="1m")
    if df.empty:
        df = yf.Ticker(TICKER).history(period="5d", interval="1h")
    return float(df["Close"].iloc[-1])

def _direccion(nivel: float, precio: float) -> str:
    """Hacia dónde debe moverse el precio para gatillar la alerta."""
    return "cruce_abajo" if nivel <= precio else "cruce_arriba"

def _posicion_abierta_real(ticker: str):
    """Devuelve la fila de la señal ABIERTA en DB cuya entrada YA fue confirmada por
    price_watcher (alert 'entrada' disparada después de su fecha) — una posición real en
    curso, no una señal pendiente que todavía espera tocar la entrada (esa sigue su propio
    camino vía el plan_trade de hoy, o queda invalidada por invalidar_señales_pendientes).
    None si no hay ninguna."""
    import sqlite3
    db_path = Path(__file__).parent.parent / "data" / "sigmaview.db"
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT fecha, stop, o1, o2, o3, o1_hit, o2_hit, o3_hit FROM signals "
        "WHERE ticker=? AND estado='ABIERTO' ORDER BY fecha DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row:
        con.close()
        return None
    entrada_fired = con.execute(
        "SELECT 1 FROM alerts_fired WHERE ticker=? AND alert_id='entrada' AND fired_at >= ? LIMIT 1",
        (ticker, row["fecha"]),
    ).fetchone()
    con.close()
    return row if entrada_fired else None

def generar_plan(precio_actual: float | None = None) -> dict:
    l1 = json.loads(L1_FILE.read_text()) if L1_FILE.exists() else {}
    l3 = json.loads(L3_FILE.read_text()) if L3_FILE.exists() else {}
    # Precio de referencia para fijar la dirección de cada alerta: el de mercado, no el techo
    precio = precio_actual or _precio_mercado()
    alertas = []

    # Niveles resolutorios de L1 (resuelven escenarios → re-evaluar)
    for lvl in (l1.get("niveles_para_l2", {}).get("niveles_resolutorios") or []):
        try:
            lvl = float(lvl)
        except (TypeError, ValueError):
            continue
        alertas.append({
            "id": f"resolutorio_{int(lvl)}",
            "nivel": lvl,
            "direccion": _direccion(lvl, precio),
            "tipo": "RESOLUTORIO",
            "mensaje": f"⚠️ BTC alcanzó ${lvl:,.0f} — nivel resolutorio L1. Re-evaluar escenarios.",
            "fired": False,
        })

    # Alerta anticipatoria Modo B: ABC en formación, C aún no llegó al target
    aa = l3.get("alerta_anticipada") or {}
    if aa.get("activa") and aa.get("nivel_c_1x"):
        d = l3.get("direccion", "")
        for key, label in (("nivel_c_1x", "1.0×A"), ("nivel_c_1618", "1.618×A")):
            nivel = aa.get(key)
            if nivel:
                alertas.append({
                    "id": f"anticipada_{key}",
                    "nivel": float(nivel),
                    "direccion": _direccion(float(nivel), precio),
                    "tipo": "MODO_B_ANTICIPADO",
                    "mensaje": (f"🔔 ZONA C PROYECTADA ({label}): BTC tocó ${float(nivel):,.0f}. "
                                f"ABC {d} en target — evaluar Modo B AHORA.\n"
                                f"B terminó en ${aa.get('b_fin', '?'):,} | "
                                f"A size: ${aa.get('a_size', '?'):,} pts"),
                    "fired": False,
                })

    # Posición YA ABIERTA en DB (entrada ya confirmada) — se vigila SIEMPRE, sin importar
    # el veredicto de hoy. Un día que dice "ESPERAR" no cierra una posición real que sigue
    # corriendo desde un día anterior; antes de este fix, generar_plan() solo construía
    # stop/objetivos a partir del plan_trade DEL DÍA, y una posición abierta se quedaba sin
    # vigilancia en cuanto la corrida siguiente no fuera otra vez "SEÑAL".
    abierta = _posicion_abierta_real(TICKER)
    if abierta:
        _ACCION = {"o1": "cerrar 50%", "o2": "cerrar 25%, subir stop a entrada",
                   "o3": "cerrar 25% restante"}
        for k in ("stop", "o1", "o2", "o3"):
            if k != "stop" and abierta[f"{k}_hit"]:
                continue  # objetivo ya tocado en una corrida anterior, no re-vigilar
            nivel = abierta["stop"] if k == "stop" else abierta[k]
            if nivel is None:
                continue
            if k == "stop":
                tipo, mensaje = "STOP", f"🛑 BTC tocó el STOP ${nivel:,.0f}. Setup invalidado."
            else:
                tipo = "OBJETIVO"
                mensaje = f"🎯 BTC alcanzó {k.upper()} ${nivel:,.0f} — {_ACCION[k]}"
            alertas.append({
                "id": k, "nivel": nivel, "direccion": _direccion(nivel, precio),
                "tipo": tipo, "mensaje": mensaje, "fired": False,
            })

    # Plan de trade NUEVO de L3 (si hoy hay setup y no hay ya una posición real abierta)
    pt = l3.get("plan_trade")
    if not abierta and pt and l3.get("veredicto") == "SEÑAL":
        d = l3.get("direccion", "")
        cal = l3.get("calidad_señal", "")
        alertas.append({
            "id": "entrada", "nivel": pt["entrada"], "direccion": _direccion(pt["entrada"], precio),
            "tipo": "ENTRADA",
            "mensaje": (f"🟢 ENTRADA {d} ({cal}): BTC tocó ${pt['entrada']:,.0f}.\n"
                        f"Stop ${pt['stop']:,.0f} | O1 ${pt['O1']:,.0f} | O2 ${pt['O2']:,.0f} | O3 ${pt['O3']:,.0f}"),
            "fired": False,
        })
        alertas.append({
            "id": "stop", "nivel": pt["stop"], "direccion": _direccion(pt["stop"], precio),
            "tipo": "STOP", "mensaje": f"🛑 BTC tocó el STOP ${pt['stop']:,.0f}. Setup invalidado.",
            "fired": False,
        })
        for k in ("O1", "O2", "O3"):
            alertas.append({
                "id": k.lower(), "nivel": pt[k], "direccion": _direccion(pt[k], precio),
                "tipo": "OBJETIVO",
                "mensaje": f"🎯 BTC alcanzó {k} ${pt[k]:,.0f} — {pt.get(k+'_accion','')}",
                "fired": False,
            })

    plan = {
        "generado": l3.get("fecha") or l1.get("fecha_analisis"),
        "precio_referencia": precio,
        "direccion_setup": l3.get("direccion"),
        "veredicto": l3.get("veredicto"),
        "alertas": alertas,
    }
    DATA_DIR.mkdir(exist_ok=True)
    PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return plan

if __name__ == "__main__":
    p = generar_plan()
    print(f"Plan generado: {len(p['alertas'])} alertas activas (precio ref ${p['precio_referencia']:,.0f})")
    for a in p["alertas"]:
        print(f"  [{a['tipo']}] ${a['nivel']:,.0f} ({a['direccion']}) — {a['mensaje'].splitlines()[0]}")
