"""
Registro PROSPECTIVO de señales Elliott — mide si las entradas son factibles y si los trades ganan.

Dos preguntas operacionales por señal:
  1. ¿Se llena la entrada? (fill rate — ¿las zonas de entrada son realistas?)
  2. Dado que se llenó, ¿toca TP o SL? (win rate + R promedio — ¿el método tiene edge?)

Ciclo de vida de un trade:
  pendiente → abierto (entry llenó) → ganado (TP) | perdido (SL)
  pendiente → no_lleno (horizonte expiró sin llenar)

Uso:
  python3 src/prospectivo.py vivas        # QUÉ ESTOY VIGILANDO AHORA y a qué precio (vista diaria)
  python3 src/prospectivo.py vivas v3     # solo las de una versión del método
  python3 src/prospectivo.py resolver     # actualiza estados contra precio real (yfinance)
  python3 src/prospectivo.py scorecard    # edge vs naive, calibración, fill/win rate, R
"""
import json, sys, os
from datetime import datetime, timezone

LOG = os.path.join(os.path.dirname(__file__), "..", "data", "predicciones_prospectivas.jsonl")

# Versión del MÉTODO con que se generó la predicción. Sin esto el scorecard mezcla predicciones
# hechas con prompts distintos y deja de ser interpretable: no se puede saber si un cambio de
# win-rate viene del mercado o de que cambiamos las reglas a mitad de camino.
# REGLA: subir la versión SOLO al agregar/quitar reglas del prompt. Un bugfix no la mueve.
# Al subirla, congelar y acumular muestra antes del siguiente cambio.
METODO_VERSION = "v3"
METODO_HISTORIAL = {
    "v1": "prompt original 348 líneas (hasta 2026-07-25): Modos A/B/C/D, checklist 3 señales, "
          "sin registro obligatorio ni conteo alternativo",
    "v2": "2026-07-26: + PASO 0 (pivotes mecánicos), consistencia de grado, PASO 4 (predicción "
          "falsable con primario/alternativo y probabilidad), simetría obj/inval, congelamiento del conteo",
    "v3": "2026-07-27: + gate de régimen y liquidez (PASO 1.0), checklist de 6 puntos de Santos para "
          "Modo B, nodo 1.618 eliminado del zigzag, figura ideal + jerarquía de divergencias, "
          "Modo D con Fibonacci como criterio duro, corte 2-4 brusco, techo de confianza por fase, RSI(14)",
}


def _load():
    if not os.path.exists(LOG):
        return []
    with open(LOG) as f:
        return [json.loads(l) for l in f if l.strip()]


def _save_all(rows):
    with open(LOG, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def registrar(activo, precio, direccion, objetivo, invalidacion, horizonte_fecha,
              confianza, grado, prediccion_texto, benchmark_naive_dir, modo="", notas="",
              plan_trade=None, clase="otro", metodo_version=METODO_VERSION):
    """Agrega una predicción falsable al log (append-only). Devuelve el id.

    plan_trade (opcional): dict con la operación concreta para medir P&L en R además de la dirección:
      {"entry": float, "stop": float, "o1": float, "o2": float, "o3": float}
      Gestión Enfoque B: 1/3 en cada objetivo, stop→break-even tras O1. R = |entry-stop|.
      Si el veredicto fue ESPERAR con setup pendiente, igual se registra (se rastrea si llena y su R)."""
    rows = _load()
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = sum(1 for r in rows if r["activo"] == activo and r["ts_registro"][:10] == hoy) + 1
    pid = f"{hoy}-{activo.split('-')[0]}-{n:03d}"
    row = {
        "id": pid,
        "ts_registro": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metodo_version": metodo_version,
        "activo": activo, "clase": clase, "precio_registro": round(float(precio), 4),
        "grado": grado, "direccion": direccion,
        "objetivo": round(float(objetivo), 2), "invalidacion": round(float(invalidacion), 2),
        "horizonte_fecha": horizonte_fecha,
        "criterio": ("ACIERTO si toca OBJETIVO antes que INVALIDACIÓN dentro del horizonte; "
                     "FALLO si toca INVALIDACIÓN primero; EXPIRADO si ninguno."),
        "confianza": round(float(confianza), 2),
        "benchmark_naive_dir": benchmark_naive_dir,   # qué diría 'seguir la tendencia reciente'
        "modo_elliott": modo, "prediccion_texto": prediccion_texto, "notas": notas,
        "resultado": None, "ts_resolucion": None, "precio_resolucion": None, "benchmark_resultado": None,
        # --- bloque de operación (opcional) para P&L en R ---
        "plan_trade": ({k: round(float(v), 2) for k, v in plan_trade.items()} if plan_trade else None),
        "trade_estado": ("pendiente" if plan_trade else None),  # pendiente|lleno|no_lleno
        "trade_r": None, "trade_detalle": None,
    }
    with open(LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tp = f" · trade @ ${plan_trade['entry']:,.0f}" if plan_trade else ""
    print(f"Registrada {pid}: {prediccion_texto}{tp}")
    return pid


def _sim_trade(df, pt, dirc, horizonte):
    """Simula la operación (Enfoque B: 1/3 en O1/O2/O3, stop→BE tras O1). Devuelve (estado, R, detalle).
    df: velas diarias desde el registro. pt: {entry,stop,o1,o2,o3}."""
    import pandas as pd
    e, st = pt["entry"], pt["stop"]; R = abs(e - st)
    if R <= 0:
        return "no_lleno", None, "R=0"
    fill = None
    for ts, row in df.iterrows():
        if row["Low"] <= e <= row["High"]:
            fill = ts; break
        if pd.Timestamp(ts).tz_localize(None) >= horizonte:
            return "no_lleno", None, "no llenó dentro del horizonte"
    if fill is None:
        return "pendiente", None, "aún no llena"
    pos = df[df.index > fill]; sc = st; ti = 0; r = 0.0; frac = 1.0; hits = []; closed = False; hit_stop = False
    tgts = [pt["o1"], pt["o2"], pt["o3"]]
    for ts, row in pos.iterrows():
        hi, lo = row["High"], row["Low"]
        sh = (lo <= sc) if dirc == "LONG" else (hi >= sc)
        if sh:
            rt = ((sc - e) / R) if dirc == "LONG" else ((e - sc) / R)
            r += frac * rt; hits.append("STOP@%.0f" % sc); closed = True; hit_stop = True; break
        while ti < 3:
            lvl = tgts[ti]; th = (hi >= lvl) if dirc == "LONG" else (lo <= lvl)
            if not th:
                break
            rt = ((lvl - e) / R) if dirc == "LONG" else ((e - lvl) / R)
            r += (1 / 3) * rt; frac -= 1 / 3; hits.append("O%d" % (ti + 1))
            if ti == 0:
                sc = e
            ti += 1
        if ti >= 3:
            closed = True; break
    if closed:
        estado = "perdido" if hit_stop else "ganado"
    else:
        estado = "abierto"
        if frac > 1e-9:
            last = pos["Close"].iloc[-1] if len(pos) else e
            rt = ((last - e) / R) if dirc == "LONG" else ((e - last) / R)
            r += frac * rt; hits.append("abierto@%.0f" % last)
    return estado, round(r, 2), " → ".join(hits)


def resolver():
    """Resuelve mecánicamente predicciones (dirección) y operaciones (R) pendientes contra precio real."""
    import yfinance as yf, pandas as pd
    rows = _load(); cambios = 0
    for r in rows:
        pendiente_pred = r["resultado"] is None
        pendiente_trade = r.get("plan_trade") and r.get("trade_estado") in ("pendiente", "abierto")
        if not (pendiente_pred or pendiente_trade):
            continue
        ini = r["ts_registro"][:10]
        df = yf.download(r["activo"], start=ini, interval="1d", auto_adjust=True, progress=False)
        if df.empty:
            continue
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df.index = [pd.Timestamp(t).tz_localize(None) if pd.Timestamp(t).tzinfo else pd.Timestamp(t) for t in df.index]
        dirc = r["direccion"]; horizonte = pd.Timestamp(r["horizonte_fecha"])
        # --- predicción direccional ---
        if pendiente_pred:
            obj, inv = r["objetivo"], r["invalidacion"]; res = resd = resp = None
            for ts, row in df.iterrows():
                hit_obj = row["High"] >= obj if dirc == "LONG" else row["Low"] <= obj
                hit_inv = row["Low"] <= inv if dirc == "LONG" else row["High"] >= inv
                if hit_obj and hit_inv: res, resd, resp = "fallo", ts.strftime("%Y-%m-%d"), inv; break
                if hit_obj: res, resd, resp = "acierto", ts.strftime("%Y-%m-%d"), obj; break
                if hit_inv: res, resd, resp = "fallo", ts.strftime("%Y-%m-%d"), inv; break
                if ts >= horizonte: res, resd, resp = "expirado", ts.strftime("%Y-%m-%d"), float(row["Close"]); break
            if res:
                r["resultado"] = res; r["ts_resolucion"] = resd; r["precio_resolucion"] = round(resp, 2)
                if res != "expirado":
                    nk = (r["benchmark_naive_dir"] == dirc and res == "acierto") or (r["benchmark_naive_dir"] != dirc and res == "fallo")
                    r["benchmark_resultado"] = "acierto" if nk else "fallo"
                cambios += 1; print(f"  {r['id']} PRED: {res.upper()} @ {resd} (${resp:,.0f})")
        # --- operación (R) ---
        if pendiente_trade:
            est, rr, det = _sim_trade(df, r["plan_trade"], dirc, horizonte)
            if est != r.get("trade_estado") or rr != r.get("trade_r"):
                r["trade_estado"] = est; r["trade_r"] = rr; r["trade_detalle"] = det
                cambios += 1
                if rr is not None: print(f"  {r['id']} TRADE: {est} · {rr:+.2f}R · {det}")
    if cambios:
        _save_all(rows)
    print(f"Actualizadas {cambios} entradas.")


DB = os.path.join(os.path.dirname(__file__), "..", "data", "sigmaview.db")
WEB = os.path.join(os.path.dirname(__file__), "..", "docs", "data")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predicciones (
  id TEXT PRIMARY KEY,
  ts_registro TEXT NOT NULL,
  metodo_version TEXT NOT NULL,
  activo TEXT NOT NULL, clase TEXT,
  precio_registro REAL, grado TEXT, direccion TEXT,
  objetivo REAL, invalidacion REAL, horizonte_fecha TEXT,
  confianza REAL,
  benchmark_naive_dir TEXT,
  contradice_naive INTEGER,          -- 1 = test limpio del método (Elliott != momentum)
  modo_elliott TEXT, prediccion_texto TEXT, notas TEXT,
  resultado TEXT, ts_resolucion TEXT, precio_resolucion REAL, benchmark_resultado TEXT,
  plan_entry REAL, plan_stop REAL, plan_o1 REAL, plan_o2 REAL, plan_o3 REAL,
  trade_estado TEXT, trade_r REAL, trade_detalle TEXT
);
CREATE INDEX IF NOT EXISTS ix_pred_ver    ON predicciones(metodo_version);
CREATE INDEX IF NOT EXISTS ix_pred_estado ON predicciones(resultado);
CREATE INDEX IF NOT EXISTS ix_pred_limpio ON predicciones(contradice_naive);
"""


def build_db():
    """Reconstruye la tabla `predicciones` DESDE el .jsonl. Es idempotente y destructiva por diseño:
    la base es un índice consultable, nunca la fuente de verdad. Si se corrompe o cambia el esquema,
    se borra y se reconstruye — el log append-only versionado en git es lo que no se puede perder."""
    import sqlite3
    rows = _load()
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    con.execute("DELETE FROM predicciones")
    for r in rows:
        pt = r.get("plan_trade") or {}
        vals = (r["id"], r["ts_registro"], r.get("metodo_version", "v1"), r["activo"], r.get("clase"),
             r.get("precio_registro"), r.get("grado"), r.get("direccion"),
             r.get("objetivo"), r.get("invalidacion"), r.get("horizonte_fecha"), r.get("confianza"),
             r.get("benchmark_naive_dir"),
             1 if r.get("direccion") != r.get("benchmark_naive_dir") else 0,
             r.get("modo_elliott"), r.get("prediccion_texto"), r.get("notas"),
             r.get("resultado"), r.get("ts_resolucion"), r.get("precio_resolucion"),
             r.get("benchmark_resultado"),
             pt.get("entry"), pt.get("stop"), pt.get("o1"), pt.get("o2"), pt.get("o3"),
             r.get("trade_estado"), r.get("trade_r"), r.get("trade_detalle"))
        # placeholders derivados de los valores: si cambia el esquema, no hay que tocar un número
        con.execute("INSERT INTO predicciones VALUES (" + ",".join("?" * len(vals)) + ")", vals)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM predicciones").fetchone()[0]
    con.close()
    print(f"DB reconstruida desde el log: {n} predicciones en {os.path.relpath(DB)}")
    return n


def export_web():
    """Escribe docs/data/prospectivo.json — lo único que consume el dashboard.
    Incluye precios actuales para poder mostrar distancia a objetivo/invalidación sin JS pesado."""
    import yfinance as yf
    from datetime import date, datetime as _dt
    rows = _load()
    for r in rows:                       # campos derivados (no tocan el log): entrada fija y R:R de diseño
        r["entrada_efectiva"] = _entrada(r)
        r["rr"] = _rr(r)
    vivos = [r for r in rows if r.get("resultado") is None]

    precios = {}
    for a in sorted({r["activo"] for r in vivos}):
        try:
            d = yf.download(a, period="5d", interval="1d", auto_adjust=True, progress=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            precios[a] = round(float(d["Close"].dropna().iloc[-1]), 4)
        except Exception:
            precios[a] = None

    res = [r for r in rows if r.get("resultado") in ("acierto", "fallo")]
    contra = [r for r in rows if r["direccion"] != r.get("benchmark_naive_dir")]
    contra_res = [r for r in contra if r.get("resultado") in ("acierto", "fallo")]
    confs = [r["confianza"] for r in rows] or [0]

    def _tasa(g):
        rv = [x for x in g if x.get("resultado") in ("acierto", "fallo")]
        ac = sum(1 for x in rv if x["resultado"] == "acierto")
        return {"resueltas": len(rv), "aciertos": ac,
                "pct": round(ac / len(rv) * 100, 1) if rv else None}

    payload = {
        "generado": _dt.utcnow().isoformat(timespec="seconds") + "Z",
        "fecha": date.today().isoformat(),
        "metodo_version_actual": METODO_VERSION,
        "metodo_historial": METODO_HISTORIAL,
        "precios": precios,
        "predicciones": rows,
        "resumen": {
            "total": len(rows), "vivas": len(vivos), "resueltas": len(res),
            "tests_limpios": {"registrados": len(contra), "resueltos": len(contra_res),
                              **_tasa(contra)},
            "coinciden_naive": _tasa([r for r in rows if r["direccion"] == r.get("benchmark_naive_dir")]),
            "calibracion": {
                "conf_min": min(confs), "conf_max": max(confs),
                "dispersion": round(max(confs) - min(confs), 3),
                "brier": round(sum((r["confianza"] - (1 if r["resultado"] == "acierto" else 0)) ** 2
                                   for r in res) / len(res), 4) if res else None,
            },
            "por_version": {v: {"registradas": sum(1 for r in rows if r.get("metodo_version") == v),
                                **_tasa([r for r in rows if r.get("metodo_version") == v])}
                            for v in sorted({r.get("metodo_version", "v1") for r in rows})},
            "por_clase": {c: _tasa([r for r in rows if r.get("clase") == c])
                          for c in sorted({r.get("clase") or "otro" for r in rows})},
        },
    }
    os.makedirs(WEB, exist_ok=True)
    p = os.path.join(WEB, "prospectivo.json")
    with open(p, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"Export web: {os.path.relpath(p)} ({len(rows)} predicciones, {len(vivos)} vivas)")
    return p


def vivas(solo_version=None):
    """Vista operativa: qué estoy vigilando AHORA y a qué precio.
    Trae el precio actual de cada activo y muestra la distancia a objetivo, invalidación y,
    si hay plan_trade pendiente, a la entrada. Ordenado por lo más cerca de gatillarse."""
    import yfinance as yf
    from datetime import date
    rows = [r for r in _load() if r.get("resultado") is None]
    if solo_version:
        rows = [r for r in rows if r.get("metodo_version") == solo_version]
    if not rows:
        print("No hay predicciones vivas.")
        return

    precios = {}
    for a in sorted({r["activo"] for r in rows}):
        try:
            d = yf.download(a, period="5d", interval="1d", auto_adjust=True, progress=False)
            d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
            precios[a] = float(d["Close"].dropna().iloc[-1])
        except Exception:
            precios[a] = None

    hoy = date.today()
    filas = []
    for r in rows:
        p = precios.get(r["activo"])
        if p is None:
            continue
        obj, inv = r["objetivo"], r["invalidacion"]
        d_obj = abs(p - obj) / p * 100
        d_inv = abs(p - inv) / p * 100
        dias = (date.fromisoformat(r["horizonte_fecha"]) - hoy).days
        entrada = _entrada(r); rr = _rr(r)
        filas.append((min(d_obj, d_inv), r, p, d_obj, d_inv, entrada, rr, dias))
    filas.sort(key=lambda x: x[0])

    print(f"\n{'='*100}")
    print(f"  SEÑALES VIVAS ({len(filas)})  —  ordenadas por proximidad al gatillo   [{hoy}]")
    print(f"{'='*100}")
    print(f"  {'activo':10s} {'ver':3s} {'dir':5s} {'entrada':>9s} {'actual':>9s} {'R:R':>5s} "
          f"{'objetivo':>11s} {'inval':>11s} {'días':>5s}  estado")
    print(f"  {'-'*96}")
    for _, r, p, d_obj, d_inv, entrada, rr, dias in filas:
        rr_s = (f"{rr:.2f}" + ("!" if rr < 1 else "")) if rr is not None else "—"
        alerta = "  ⚠ CERCA" if min(d_obj, d_inv) < 5 else ("  ⏰ VENCE" if dias < 14 else "")
        print(f"  {r['activo']:10s} {r.get('metodo_version','?'):3s} {r['direccion']:5s} "
              f"{entrada:>9,.2f} {p:>9,.2f} {rr_s:>5s} "
              f"{obj_s(r['objetivo'], d_obj):>11s} {obj_s(r['invalidacion'], d_inv):>11s} "
              f"{dias:>5d}{alerta}")
    print(f"\n  entrada = precio fijo del setup · actual = último cierre · R:R = riesgo:beneficio en la entrada"
          f" ('!' = R:R<1, se arriesga más de lo que se busca; no se filtra, se observa).")
    print(f"  Formato objetivo/inval: precio(distancia% desde actual).  ⚠ = a menos de 5% de resolverse.")
    print(f"  Detalle completo de cualquiera: grep '<id>' data/predicciones_prospectivas.jsonl")


def obj_s(v, d):
    return f"{v:,.0f}({d:.0f}%)" if v >= 100 else f"{v:,.2f}({d:.0f}%)"


def _entrada(r):
    """Precio de entrada de la señal: el del plan_trade si existe, si no el de registro.
    Es FIJO — el precio con que se concibió el trade, no el de mercado de hoy."""
    pt = r.get("plan_trade") or {}
    e = pt.get("entry")
    return e if e is not None else r.get("precio_registro")


def _rr(r):
    """Reward:Risk del setup medido EN LA ENTRADA (no en el precio actual, que se mueve).
    Es la calidad de diseño del trade: cuánto se arriesga por unidad de objetivo, fija desde el
    registro. NO se usa para filtrar señales — solo se muestra y se acumula para aprender si un R:R
    bajo efectivamente pierde más. Devuelve None si el riesgo es incoherente con la dirección."""
    e = _entrada(r)
    obj, inv = r.get("objetivo"), r.get("invalidacion")
    if e is None or obj is None or inv is None:
        return None
    if r.get("direccion") == "LONG":
        reward, risk = obj - e, e - inv
    else:
        reward, risk = e - obj, inv - e
    if risk <= 0:
        return None
    return round(reward / risk, 2)


def scorecard():
    """Métricas operacionales: fill rate, win rate, R promedio, expectativa."""
    rows = _load()
    con_trade = [r for r in rows if r.get("plan_trade")]
    sin_trade  = [r for r in rows if not r.get("plan_trade")]

    print(f"=== SCORECARD PROSPECTIVO === ({len(rows)} señales totales)\n")

    # ── bloque principal: trades ──────────────────────────────────────────────
    if con_trade:
        llenados   = [r for r in con_trade if r.get("trade_estado") in ("abierto", "ganado", "perdido")]
        no_llenados = [r for r in con_trade if r.get("trade_estado") == "no_lleno"]
        pendientes  = [r for r in con_trade if r.get("trade_estado") == "pendiente"]
        cerrados    = [r for r in con_trade if r.get("trade_estado") in ("ganado", "perdido")]
        ganados     = [r for r in cerrados  if r.get("trade_estado") == "ganado"]
        perdidos    = [r for r in cerrados  if r.get("trade_estado") == "perdido"]

        total_evaluados = len(llenados) + len(no_llenados)
        fill_rate = len(llenados) / total_evaluados if total_evaluados else None

        print(f"TRADES (señales con entry/stop/TP): {len(con_trade)} total")
        print(f"  Pendientes de entry : {len(pendientes)}")
        if total_evaluados:
            pct = f"{fill_rate*100:.0f}%" if fill_rate is not None else "—"
            print(f"  Fill rate           : {len(llenados)}/{total_evaluados} = {pct}  (¿llega el precio a la entry?)")
        if cerrados:
            wr = len(ganados) / len(cerrados)
            rs = [r["trade_r"] for r in cerrados if r.get("trade_r") is not None]
            exp = sum(rs) / len(rs) if rs else 0
            print(f"  Win rate            : {len(ganados)}/{len(cerrados)} = {wr*100:.0f}%  (ganado/perdido de los cerrados)")
            print(f"  R promedio          : {exp:+.2f}R/trade")
            print(f"  R total acumulado   : {sum(rs):+.2f}R")
            print(f"  Expectativa neta    : {(fill_rate or 0)*exp:+.2f}R/señal  (fill_rate × R_promedio)")
        if llenados and not cerrados:
            print(f"  Abiertos en curso   : {len(llenados)}")

        # desglose por activo/clase
        clases = sorted(set(r.get("clase", r["activo"]) for r in cerrados))
        if len(clases) > 1:
            print("\n  Por clase (independencia):")
            for c in clases:
                b = [r for r in cerrados if r.get("clase", r["activo"]) == c]
                g = sum(1 for r in b if r["trade_estado"] == "ganado")
                rs_c = [r["trade_r"] for r in b if r.get("trade_r") is not None]
                print(f"    {c:14s}: {g}/{len(b)} ganados · {sum(rs_c):+.2f}R total (n={len(b)})")

    # ── bloque secundario: señales sin trade (solo dirección) ─────────────────
    res_dir = [r for r in sin_trade if r.get("resultado") in ("acierto", "fallo")]
    if res_dir:
        ac = sum(1 for r in res_dir if r["resultado"] == "acierto")
        print(f"\nSEÑALES DIRECCIONALES (sin trade concreto): {len(res_dir)} resueltas")
        print(f"  Acierto dirección   : {ac}/{len(res_dir)} = {ac/len(res_dir)*100:.0f}%  (referencia: 50%)")

    if not con_trade and not res_dir:
        print("Aún no hay señales resueltas.")

    # ── LO QUE DE VERDAD MIDE EL EDGE: Elliott vs naive, separando acuerdos de desacuerdos ──
    # Solo las predicciones donde Elliott CONTRADICE al naive son test limpio del método.
    # Donde coinciden, un acierto no prueba nada sobre Elliott: lo habría acertado el momentum solo.
    contra = [r for r in rows if r["direccion"] != r.get("benchmark_naive_dir")]
    coincide = [r for r in rows if r["direccion"] == r.get("benchmark_naive_dir")]
    print(f"\nELLIOTT vs NAIVE (seguir-tendencia) — el test de edge:")
    for grupo, lbl, nota in ((contra, "CONTRADICEN naive", "← test limpio: solo esto mide si Elliott aporta"),
                             (coincide, "coinciden con naive", "  (mide el sistema completo, no Elliott)")):
        rv = [r for r in grupo if r.get("resultado") in ("acierto", "fallo")]
        ac = sum(1 for r in rv if r["resultado"] == "acierto")
        tasa = f"{ac}/{len(rv)} = {ac/len(rv)*100:.0f}%" if rv else "0 resueltas"
        print(f"  {lbl:22s}: {len(grupo):2d} registradas · {tasa:16s} {nota}")
    if not [r for r in contra if r.get("resultado") in ("acierto", "fallo")]:
        print("  ⚠️  CERO tests limpios resueltos — todavía no hay NINGUNA evidencia sobre el edge de Elliott.")

    # ── CALIBRACIÓN: ¿las de 60% aciertan ~60%? Sin dispersión de confianza no es medible ──
    resueltas = [r for r in rows if r.get("resultado") in ("acierto", "fallo")]
    confs = [r["confianza"] for r in rows]
    print(f"\nCALIBRACIÓN: confianzas declaradas entre {min(confs):.2f} y {max(confs):.2f} "
          f"(dispersión {max(confs)-min(confs):.2f})")
    if max(confs) - min(confs) < 0.25:
        print("  ⚠️  Dispersión insuficiente: con todo apiñado cerca de 0.55 no se puede construir")
        print("      una curva de calibración. Hay que mojarse más — declarar 0.35 y 0.75 cuando toque.")
    if resueltas:
        brier = sum((r["confianza"] - (1 if r["resultado"] == "acierto" else 0))**2 for r in resueltas) / len(resueltas)
        print(f"  Brier score: {brier:.3f}  (0=perfecto · 0.25=azar declarando 0.5 · >0.25 peor que azar)")

    # ── desglose por VERSIÓN DEL MÉTODO (crítico: sin esto se mezclan prompts distintos) ──
    vers = sorted({r.get("metodo_version", "sin_version") for r in rows})
    if len(vers) > 1:
        print("\nPOR VERSIÓN DEL MÉTODO (no comparar entre versiones — son prompts distintos):")
        for v in vers:
            b = [r for r in rows if r.get("metodo_version", "sin_version") == v]
            rv = [r for r in b if r.get("resultado") in ("acierto", "fallo")]
            ac = sum(1 for r in rv if r["resultado"] == "acierto")
            pend = len(b) - len(rv)
            tasa = f"{ac}/{len(rv)} = {ac/len(rv)*100:.0f}%" if rv else "sin resolver aún"
            print(f"  {v:5s}: {len(b):2d} registradas · {pend:2d} vivas · dirección {tasa}")
        print("  (meta: 20-30 resueltas DENTRO de una misma versión antes de concluir nada)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "vivas"
    if cmd == "resolver":
        resolver()
    elif cmd == "scorecard":
        scorecard()
    elif cmd == "build":          # jsonl -> sqlite
        build_db()
    elif cmd == "export":         # jsonl -> docs/data/prospectivo.json
        export_web()
    elif cmd == "sync":           # el ciclo completo, el que corre GitHub Actions
        resolver(); build_db(); export_web()
    else:
        vivas(sys.argv[2] if len(sys.argv) > 2 else None)
