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
CANDLE_COUNT = 360          # velas 4h (~60 días) para ver la estructura fina
DEFAULT_MODEL = "claude-opus-4-8"
RATES = {"claude-opus-4-8": (15, 75), "claude-sonnet-4-6": (3, 15)}
PROMPT_PATH = Path(__file__).parent / "prompts" / "level3_modoB_fuerte.txt"
DATA_DIR = Path(__file__).parent.parent / "data"
L1_FILE = DATA_DIR / "l1_btc_latest.json"
L2_FILE = DATA_DIR / "l2_btc_latest.json"
OUTPUT_FILE = DATA_DIR / "l3_btc_latest.json"

# ── API key ───────────────────────────────────────────────────────────────────

def clean_api_key() -> str:
    raw = os.environ.get("ANTHROPIC_API_KEY", "")
    match = re.search(r"sk-ant-[A-Za-z0-9_\-]+", raw)
    if not match:
        sys.exit("ERROR: no se encontró una API key válida (sk-ant-...) en ANTHROPIC_API_KEY")
    return match.group(0)

def _enviar_telegram(mensaje: str) -> None:
    import urllib.parse, urllib.request
    token = os.environ.get("TELEGRAM_TOKEN")
    chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": mensaje}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
    except Exception:
        pass

# ── Datos ─────────────────────────────────────────────────────────────────────

def fetch_4h_data(candles: int, asof: str | None = None) -> str:
    # Prefiere DB propia (historia acumulada) sobre yfinance (límite ~730d 1h)
    try:
        import database
        csv = database.fetch_4h_from_db(TICKER, candles, asof)
        if csv:
            return csv
    except Exception:
        pass
    # yfinance: 4h no es nativo; bajamos 1h y resampleamos a 4h
    # Nota: yfinance solo guarda ~730 días de datos horarios → asof debe estar en esa ventana
    df = yf.Ticker(TICKER).history(period="730d", interval="1h")
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df4 = df.resample("4h").agg(agg).dropna()
    if asof:
        # Hasta el fin del día asof (incluye todas las velas 4h de ese día, ninguna posterior)
        df4 = df4[df4.index < pd.Timestamp(asof) + pd.Timedelta(days=1)]
    df4 = df4.tail(candles)
    df4.index = df4.index.strftime("%Y-%m-%d %H:%M")
    return df4.to_csv()

def load_json(path: Path, label: str) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: no existe {path}. Corre {label} primero.")
    return json.loads(path.read_text())

# ── Gate de disparo Modo B (determinista en Python) ─────────────────────────────

CA_TOLERANCIA = 0.20        # ±20% sobre 1.0 o 1.618 (absorbe ambigüedad de conteo)
RR_MINIMO = 5.0             # R:R(O2) mínimo para disparar Modo B
TARGET_GATE = "O2"         # objetivo conservador para el gate (recuperación de la corrección)
MAX_DIST_ENTRADA = 0.10    # el precio debe estar a <=10% de la entrada (setup no rancio)
MIN_STOP_BUFFER = 0.02     # colchón mínimo del stop (2%) — stop sobrevivible al ruido, R:R realista
ENTRY_BUFFER = 0.01        # colchón de entrada Modo B (1%) — entrar cerca del extremo, no en la mecha exacta (llenable)
DEGREE_MISMATCH_TOL = 0.20 # techo L1 (cachea, refresca solo lunes) vs abc_a_inicio del modelo HOY:
                           # si difieren más de esto, son grados distintos — no sustituir pivotes.
                           # Validado contra los 7 disparos históricos reales (máx 11.5% de diferencia).

def ca_en_tolerancia(c_sobre_a: float) -> bool:
    if c_sobre_a is None:
        return False
    for ref in (1.0, 1.618):
        if abs(c_sobre_a - ref) / ref <= CA_TOLERANCIA:
            return True
    return False

def evaluar_modo_b(res: dict, l1_levels: dict | None = None) -> dict:
    """Decide el disparo de Modo B con R:R calculado por Python (no por el modelo).
    Riesgo = entrada−stop (conocido). Recompensa = proyección Fibonacci (O2). Sin hindsight.
    Si l1_levels {techo, operativo} está disponible, usa el extremo operativo de Python como
    fin de C (determinista) en vez del abc_c_fin del modelo (que varía entre corridas)."""
    mb = res.get("modo_b_check", {})
    if not mb.get("abc_detectada"):
        return {"dispara": False, "motivo": "no hay ABC detectada"}

    c_a = mb.get("c_sobre_a")
    if not ca_en_tolerancia(c_a):
        return {"dispara": False, "motivo": f"c/a={c_a} fuera de tolerancia (≈1.0 o 1.618 ±20%)"}

    # Pivotes deterministas: el fin de C es el extremo operativo (mínimo/máximo real de los datos,
    # calculado por Python en L1), y el inicio de la corrección es el techo operativo.
    piv = dict(res.get("pivotes") or {})
    if l1_levels and l1_levels.get("operativo") and l1_levels.get("techo"):
        l1_techo = float(l1_levels["techo"])
        # L1 se recalcula solo los lunes; si el techo cacheado difiere mucho del techo que el
        # modelo está narrando HOY para esta misma corrección ABC, están leyendo grados distintos
        # (ej. L1 ancla un techo de hace meses mientras L3 ya pasó a una corrección más reciente y
        # menor). Sustituir los pivotes en ese caso produce objetivos sin relación con el movimiento
        # actual — más seguro bloquear que disparar con niveles desincronizados.
        modelo_a_inicio = piv.get("abc_a_inicio")
        if modelo_a_inicio:
            disc = abs(l1_techo - float(modelo_a_inicio)) / float(modelo_a_inicio)
            if disc > DEGREE_MISMATCH_TOL:
                return {"dispara": False,
                        "motivo": f"L1 desincronizado: techo L1 ${l1_techo:,.0f} vs techo narrado "
                                  f"hoy ${float(modelo_a_inicio):,.0f} ({disc:.0%} de diferencia) — "
                                  f"grados distintos, no se sustituyen pivotes"}
        piv["abc_c_fin"] = float(l1_levels["operativo"])
        piv["abc_a_inicio"] = l1_techo
        piv["stop_extremo"] = float(l1_levels["operativo"])  # el colchón mínimo lo ensancha
    try:
        trade = compute_trade(res["direccion"], "B_fin_abc", piv)
    except (KeyError, ValueError, TypeError) as e:
        return {"dispara": False, "motivo": f"pivotes incompletos ({e})"}

    # La entrada anticipada debe seguir siendo alcanzable: si el precio ya se alejó del
    # extremo de C, el setup está rancio (el rebote ya ocurrió). Esto distingue un fin de
    # corrección actuable de uno que ya pasó.
    precio = res.get("precio_actual")
    if precio:
        dist = abs(float(precio) - trade["entrada"]) / trade["entrada"]
        if dist > MAX_DIST_ENTRADA:
            return {"dispara": False, "trade": trade,
                    "motivo": f"entrada rancia: precio a {dist:.0%} del fin de C (máx {MAX_DIST_ENTRADA:.0%})"}

    # Modo B es anticipatorio: asume que el precio está EN o cerca del fin de C, antes de que
    # exista estructura del nuevo impulso. Si el checklist Santos ya confirma S1 (retroceso)
    # + S2 (estructura 1-2 contable), el mercado ya formó W1+W2 del impulso post-corrección —
    # el punto óptimo de Modo B ya pasó, aunque el precio siga numéricamente "cerca" del fin de C.
    ch = res.get("checklist") or {}
    s1 = str(ch.get("s1_retroceso", "")).strip().upper() in ("SÍ", "SI")
    s2 = str(ch.get("s2_estructura", "")).strip().upper() in ("SÍ", "SI")
    if s1 and s2:
        return {"dispara": False, "trade": trade,
                "motivo": "Modo B obsoleto: S1+S2 ya confirmados (W1+W2 de impulso post-C) — "
                          "evaluar Modo A/C en su lugar"}

    rr = trade["R:R"].get(TARGET_GATE)
    if not mb.get("invalidacion_clara", True):
        return {"dispara": False, "motivo": "invalidación no clara", "rr_python": rr, "trade": trade}
    if rr is None or rr < RR_MINIMO:
        return {"dispara": False, "motivo": f"R:R({TARGET_GATE})={rr} < {RR_MINIMO}x", "rr_python": rr, "trade": trade}

    return {"dispara": True, "motivo": f"c/a={c_a} OK, R:R({TARGET_GATE})={rr}x ≥ {RR_MINIMO}x, entrada vigente",
            "rr_python": rr, "trade": trade}

# ── Modo sombra: Modo B sin sustitución de L1 (en evaluación, no decide aún) ───────────
# Idea: cada grado de onda tiene su propio objetivo, calculado con las anclas de ESE grado
# (Enrique Santos, "El mayor grado") — nunca se debe sustituir el pivote de una corrección de
# grado L3 con el techo/suelo de grado L1. En vez de eso, el modelo narra la FECHA en que
# empezó la onda A y Python verifica ese pivote contra los datos reales de precio (igual
# patrón que compute_operative_levels en L1, pero en el grado de L3). Corre en paralelo a
# evaluar_modo_b() y solo se registra para comparar — no afecta el veredicto, el trade ni
# las alertas hasta que se valide en producción real durante un período de observación.

PIVOTE_TOL = 0.05  # el precio narrado debe corresponder a un extremo real dentro de este margen

def fetch_precio_amplio(ticker: str = TICKER, asof: str | None = None) -> pd.DataFrame:
    """Ventana de verificación AMPLIA (histórico diario completo) — independiente de las
    velas que ve el modelo en el prompt de L3. Evita que un pivote real pero antiguo (más
    allá de la ventana corta del prompt) quede como 'no verificable' por falta de datos."""
    df = yf.Ticker(ticker).history(period="max", interval="1d")
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    if asof:
        df = df[df.index < pd.Timestamp(asof) + pd.Timedelta(days=1)]
    return df

def compute_abc_pivots_grado_propio(df_amplio: pd.DataFrame, direccion: str,
                                     fecha_narrada: str | None, precio_narrado: float | None,
                                     precio_c_narrado: float | None = None,
                                     tol: float = PIVOTE_TOL) -> dict | None:
    """El modelo dice CUÁNDO empezó la onda A (su propio grado); Python ubica el precio real
    en los datos (±3 días, por si la fecha exacta cae en una vela distinta) y calcula el fin
    de C como el extremo opuesto posterior — mismo patrón que compute_operative_levels en L1,
    aplicado al grado de L3. Si el precio narrado no corresponde a ningún extremo real cercano
    a esa fecha, el pivote no es verificable: no estamos perfectamente situados, no se opera.
    Si abc_a_inicio es techo o suelo se infiere comparándolo con abc_c_fin narrado (no se
    confía en su valor exacto, solo en si es mayor o menor) — más robusto que inferirlo de
    'direccion', que describe el trade DESPUÉS de la corrección, no la forma de la corrección
    misma (una corrección puede ser una caída o un rebote independientemente del trade que siga)."""
    if not fecha_narrada or not precio_narrado:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", fecha_narrada)
    if not m:
        return None
    fecha = pd.Timestamp(m.group(0))
    es_techo = precio_c_narrado is None or float(precio_c_narrado) < float(precio_narrado)
    ventana = df_amplio[(df_amplio.index >= fecha - pd.Timedelta(days=3)) &
                         (df_amplio.index <= fecha + pd.Timedelta(days=3))]
    if ventana.empty:
        return None
    real = float(ventana["High"].max()) if es_techo else float(ventana["Low"].min())
    if abs(real - float(precio_narrado)) / float(precio_narrado) > tol:
        return None
    despues = df_amplio[df_amplio.index > fecha]
    if despues.empty:
        return None
    c_fin = float(despues["Low"].min()) if es_techo else float(despues["High"].max())
    return {"abc_a_inicio": round(real, 2), "abc_c_fin": round(c_fin, 2)}

def evaluar_modo_b_grado_propio(res: dict, df_amplio: pd.DataFrame) -> dict:
    """Misma lógica de gates que evaluar_modo_b(), pero el pivote ABC se deriva enteramente
    de los datos de precio (el propio grado de L3) — nunca de L1."""
    mb = res.get("modo_b_check", {})
    if not mb.get("abc_detectada"):
        return {"dispara": False, "motivo": "no hay ABC detectada"}

    c_a = mb.get("c_sobre_a")
    if not ca_en_tolerancia(c_a):
        return {"dispara": False, "motivo": f"c/a={c_a} fuera de tolerancia (≈1.0 o 1.618 ±20%)"}

    piv = dict(res.get("pivotes") or {})
    pivotes_reales = compute_abc_pivots_grado_propio(
        df_amplio, res.get("direccion", ""), piv.get("abc_a_inicio_fecha"), piv.get("abc_a_inicio"),
        piv.get("abc_c_fin"))
    if pivotes_reales is None:
        return {"dispara": False,
                "motivo": "pivote ABC no verificable en los datos de precio — "
                          "no estamos perfectamente situados, no se opera"}
    piv["abc_a_inicio"] = pivotes_reales["abc_a_inicio"]
    piv["abc_c_fin"] = pivotes_reales["abc_c_fin"]
    piv["stop_extremo"] = pivotes_reales["abc_c_fin"]

    try:
        trade = compute_trade(res["direccion"], "B_fin_abc", piv)
    except (KeyError, ValueError, TypeError) as e:
        return {"dispara": False, "motivo": f"pivotes incompletos ({e})"}

    precio = res.get("precio_actual")
    if precio:
        dist = abs(float(precio) - trade["entrada"]) / trade["entrada"]
        if dist > MAX_DIST_ENTRADA:
            return {"dispara": False, "trade": trade,
                    "motivo": f"entrada rancia: precio a {dist:.0%} del fin de C (máx {MAX_DIST_ENTRADA:.0%})"}

    ch = res.get("checklist") or {}
    s1 = str(ch.get("s1_retroceso", "")).strip().upper() in ("SÍ", "SI")
    s2 = str(ch.get("s2_estructura", "")).strip().upper() in ("SÍ", "SI")
    if s1 and s2:
        return {"dispara": False, "trade": trade,
                "motivo": "Modo B obsoleto: S1+S2 ya confirmados (W1+W2 de impulso post-C) — "
                          "evaluar Modo A/C en su lugar"}

    rr = trade["R:R"].get(TARGET_GATE)
    if not mb.get("invalidacion_clara", True):
        return {"dispara": False, "motivo": "invalidación no clara", "rr_python": rr, "trade": trade}
    if rr is None or rr < RR_MINIMO:
        return {"dispara": False, "motivo": f"R:R({TARGET_GATE})={rr} < {RR_MINIMO}x", "rr_python": rr, "trade": trade}

    return {"dispara": True, "motivo": f"c/a={c_a} OK, R:R({TARGET_GATE})={rr}x ≥ {RR_MINIMO}x, "
                      f"pivote verificado en ${pivotes_reales['abc_a_inicio']:,.0f}",
            "rr_python": rr, "trade": trade}

# ── Alerta anticipada: mismo bug de mezcla de grados que tenía Modo B, código separado ──
# El bug real (2026-06-22): el modelo calculaba nivel_c_1x/1618 = b_fin ± a_size mezclando
# un b_fin de rebote reciente (grado menor) con un a_size de la caída grande original (grado
# mayor) — proyección sin relación con la estructura narrada ese mismo día. Mismo principio
# que evaluar_modo_b_grado_propio(): el modelo da las FECHAS de los 3 pivotes (inicio de A,
# fin de A, fin de B), Python verifica cada uno contra los datos reales y calcula la
# proyección — nunca confía en la aritmética libre del modelo.

def compute_alerta_anticipada_grado_propio(df_amplio: pd.DataFrame,
                                            a_inicio_fecha: str | None, a_inicio_precio: float | None,
                                            a_fin_fecha: str | None, a_fin_precio: float | None,
                                            b_fin_fecha: str | None, b_fin_precio: float | None,
                                            tol: float = PIVOTE_TOL) -> dict | None:
    """Verifica los 3 pivotes de la onda A-B en curso (inicio de A, fin de A/inicio de B,
    fin de B/inicio de C) contra los datos reales, todos del MISMO movimiento, y calcula la
    proyección de C determinísticamente. Si algún pivote no es verificable, no hay proyección
    confiable — más seguro no alertar que alertar con niveles mezclados de otro grado."""
    if not (a_inicio_fecha and a_inicio_precio and a_fin_fecha and a_fin_precio
            and b_fin_fecha and b_fin_precio):
        return None
    fechas = [re.search(r"\d{4}-\d{2}-\d{2}", f) for f in (a_inicio_fecha, a_fin_fecha, b_fin_fecha)]
    if not all(fechas):
        return None
    f_a_inicio, f_a_fin, f_b_fin = (pd.Timestamp(m.group(0)) for m in fechas)
    if not (f_a_inicio < f_a_fin < f_b_fin):
        return None  # los 3 pivotes deben estar en orden cronológico del mismo movimiento

    def _extremo(fecha, es_techo, piso=None):
        # 'piso': la ventana de tolerancia (±3 días) nunca puede retroceder antes del pivote
        # cronológicamente anterior — si no, podría "encontrar" un extremo de ANTES de que
        # ese tramo existiera (ej. el máximo de B emparejado con un día previo al mínimo de A).
        desde = fecha - pd.Timedelta(days=3)
        if piso is not None:
            desde = max(desde, piso)
        ventana = df_amplio[(df_amplio.index >= desde) & (df_amplio.index <= fecha + pd.Timedelta(days=3))]
        if ventana.empty:
            return None
        return float(ventana["High"].max()) if es_techo else float(ventana["Low"].min())

    # a_inicio y a_fin son extremos opuestos (A es un tramo direccional); b_fin es del
    # mismo tipo que a_inicio (B retrocede de vuelta hacia el lado de donde partió A).
    es_techo_a_inicio = float(a_fin_precio) < float(a_inicio_precio)
    a_inicio_real = _extremo(f_a_inicio, es_techo_a_inicio)
    a_fin_real    = _extremo(f_a_fin, not es_techo_a_inicio, piso=f_a_inicio)
    b_fin_real    = _extremo(f_b_fin, es_techo_a_inicio, piso=f_a_fin)
    if a_inicio_real is None or a_fin_real is None or b_fin_real is None:
        return None
    for real, narrado in ((a_inicio_real, a_inicio_precio), (a_fin_real, a_fin_precio), (b_fin_real, b_fin_precio)):
        if abs(real - float(narrado)) / float(narrado) > tol:
            return None

    a_size = abs(a_inicio_real - a_fin_real)
    signo = -1 if es_techo_a_inicio else 1  # A bajista → C sigue bajando; A alcista → C sigue subiendo
    return {
        "b_fin": round(b_fin_real, 2), "a_size": round(a_size, 2),
        "nivel_c_1x":   round(b_fin_real + signo * 1.0 * a_size, 2),
        "nivel_c_1618": round(b_fin_real + signo * 1.618 * a_size, 2),
    }

def contra_macro(direccion: str, sesgo_macro: str) -> bool:
    """Un trade direccional va CONTRA el sesgo macro (no operar): short en macro alcista o
    long en macro bajista. NEUTRO no bloquea."""
    d = (direccion or "").upper()
    s = (sesgo_macro or "NEUTRO").upper()
    return (d == "SHORT" and s == "ALCISTA") or (d == "LONG" and s == "BAJISTA")

def decidir_veredicto(res: dict, l1_levels: dict | None = None) -> dict:
    """Decisión de disparo DETERMINISTA en Python. El modelo juzga la estructura
    (checklist, pivotes, sesgo macro); Python decide si hay señal.
    Reglas: (1) no operar contra el sesgo macro; (2) no entrar en continuación si el impulso
    macro está terminal; (3) Modo B dispara por R:R; (4) si no, checklist >=2/3 dispara Modo A/C.
    l1_levels {techo, operativo}: extremos deterministas de L1 para el cálculo del Modo B."""
    direccion = res.get("direccion")
    sesgo_macro = res.get("sesgo_macro")
    fase = (res.get("fase_impulso_macro") or "").lower()

    d = (direccion or "").upper()
    s = (sesgo_macro or "NEUTRO").upper()
    alineado_con_impulso = (d == "LONG" and s == "ALCISTA") or (d == "SHORT" and s == "BAJISTA")

    es_terminal = "terminal" in fase   # acepta "terminal", "terminal del ciclo", etc.

    # Filtro 1 — agotamiento: CONTINUACIÓN en dirección del macro terminal = entrar al final.
    # Comprar W5 alcista o seguir vendiendo W5 bajista = zona de reversión, no de entrada.
    if es_terminal and alineado_con_impulso:
        return {"veredicto": "ESPERAR", "modo": None, "calidad": "INSUFICIENTE",
                "trade": None, "motivo": f"impulso macro terminal — continuación bloqueada, esperar reversión"}

    # Filtro 2 — contra-macro: en fase no-terminal, no operar contra el grado mayor.
    # EXCEPCIÓN: si el macro ya está terminal, el trade CONTRARIO es la reversión esperada
    # (SHORT en macro alcista terminal = vender el techo del ciclo; LONG en bajista terminal =
    # comprar el suelo). Estas son las entradas más importantes — no bloquear.
    if contra_macro(direccion, sesgo_macro) and not es_terminal:
        return {"veredicto": "ESPERAR", "modo": None, "calidad": "INSUFICIENTE",
                "trade": None, "motivo": f"{d} contra sesgo macro {s} (fase {fase}) — no operar contra el grado mayor"}

    mb = evaluar_modo_b(res, l1_levels)
    if mb["dispara"]:
        return {"veredicto": "SEÑAL", "modo": "B_fin_abc", "calidad": "MODO_B",
                "trade": mb["trade"], "motivo": mb["motivo"]}

    # cumplidas: lee los campos individuales s1/s2/s3 además del resumen — el modelo a veces
    # escribe cumplidas=0 para auto-bloquear en el JSON aunque los campos digan SÍ.
    ch = res.get("checklist") or {}
    si_count = sum(1 for k in ("s1_retroceso", "s2_estructura", "s3_linea24")
                   if str(ch.get(k, "")).strip().upper() in ("SÍ", "SI"))
    cumplidas = max(int(ch.get("cumplidas") or 0), si_count)
    if cumplidas >= 2:
        # El modelo elige el estilo de entrada: retroceso (A) o breakout/continuación (C)
        modo = res.get("modo_entrada", "A_enfoque_b")
        if modo not in ("A_enfoque_b", "C_breakout"):
            modo = "A_enfoque_b"
        trade = None
        try:
            trade = compute_trade(res["direccion"], modo, res["pivotes"])
        except (KeyError, ValueError, TypeError):
            # Fallback al otro estilo si faltan pivotes
            alt = "A_enfoque_b" if modo == "C_breakout" else None
            if alt:
                try:
                    trade = compute_trade(res["direccion"], alt, res["pivotes"])
                    modo = alt
                except (KeyError, ValueError, TypeError):
                    pass
        return {"veredicto": "SEÑAL", "modo": modo,
                "calidad": "DEFINITIVA" if cumplidas >= 3 else "FUERTE",
                "trade": trade, "motivo": f"checklist {cumplidas}/3 (Santos: 2/3 ya es FUERTE)"}

    return {"veredicto": "ESPERAR", "modo": None, "calidad": "INSUFICIENTE",
            "trade": None, "motivo": f"checklist {cumplidas}/3; Modo B: {mb['motivo']}"}

# ── Cálculo determinista del trade (Enfoque B) ──────────────────────────────────

def compute_trade(direccion: str, modo: str, piv: dict) -> dict:
    """Opus da los pivotes; Python calcula entrada/stop/objetivos/R:R."""
    long = direccion.upper() == "LONG"
    sign = 1 if long else -1

    if modo == "A_enfoque_b":
        o = float(piv["w1_origen"])
        t = float(piv["w1_fin"])
        w1 = abs(t - o)
        entrada = o + sign * 0.382 * w1        # retroceso 61.8% de W1
        stop = o                                # origen de W1
        o1 = entrada + sign * 0.809 * w1
        o2 = entrada + sign * 1.618 * w1
        o3 = entrada + sign * 2.618 * w1
    elif modo == "C_breakout":
        o = float(piv["w1_origen"])             # inicio de W1
        extremo = float(piv["w1_fin"])          # extremo de W1 = nivel de rotura = entrada
        w2 = float(piv["w2_swing"])             # extremo del rebote W2 = stop ajustado
        w1 = abs(extremo - o)
        entrada = extremo                        # entrar en la rotura, ya en W3
        stop = w2                                # sobre el swing de W2 (no en el origen de W1)
        # Santos: una onda impulsiva es la EXTENDIDA (>161.8%); las dos NO extendidas tienden
        # a igualdad o 61.8% entre sí. Si la onda que se inicia es terminal (W5/C, ya hubo una
        # onda extendida antes en este impulso), NO se proyecta como si fuera a extenderse.
        if piv.get("onda_iniciada") == "W5_o_C_terminal":
            m1, m2, m3 = 0.382, 0.618, 1.0
        else:
            m1, m2, m3 = 1.0, 1.618, 2.618
        o1 = entrada + sign * m1 * w1
        o2 = entrada + sign * m2 * w1
        o3 = entrada + sign * m3 * w1
    else:  # B_fin_abc
        c_fin = float(piv["abc_c_fin"])
        # Entrar un colchón ADENTRO del extremo de C (no en la mecha exacta) → orden llenable
        entrada = c_fin * (1 + sign * ENTRY_BUFFER)
        stop = float(piv["stop_extremo"])
        a_ini = float(piv.get("abc_a_inicio", c_fin))
        rango = abs(a_ini - entrada)           # tamaño de la corrección a recuperar
        o1 = entrada + sign * 0.5 * rango
        o2 = entrada + sign * 1.0 * rango      # recuperación total de la corrección
        o3 = entrada + sign * 1.618 * rango

    # Colchón mínimo de stop: evita stops razor-thin que se barren por ruido e inflan el R:R.
    # El stop debe estar al menos MIN_STOP_BUFFER del precio de entrada.
    min_dist = entrada * MIN_STOP_BUFFER
    if abs(entrada - stop) < min_dist:
        stop = entrada - sign * min_dist       # ensancha el stop al colchón mínimo

    riesgo = abs(entrada - stop)
    rr = {
        "O1": round(abs(o1 - entrada) / riesgo, 2) if riesgo else None,
        "O2": round(abs(o2 - entrada) / riesgo, 2) if riesgo else None,
        "O3": round(abs(o3 - entrada) / riesgo, 2) if riesgo else None,
    }
    return {
        "entrada": round(entrada, 2),
        "stop": round(stop, 2),
        "riesgo_por_unidad": round(riesgo, 2),
        "O1": round(o1, 2), "O1_accion": "cerrar 50%",
        "O2": round(o2, 2), "O2_accion": "cerrar 25%, subir stop a entrada",
        "O3": round(o3, 2), "O3_accion": "cerrar 25% restante",
        "R:R": rr,
        "rr_valido_5x": (rr["O3"] or 0) >= 5.0,
    }

# ── API ───────────────────────────────────────────────────────────────────────

def call_model(prompt: str, model: str) -> tuple[dict, object]:
    client = anthropic.Anthropic(api_key=clean_api_key(), base_url="https://api.anthropic.com", max_retries=8)
    response = client.messages.create(
        model=model,
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    return json.loads(text[start:end]), response.usage

# ── Reporte ───────────────────────────────────────────────────────────────────

def print_report(r: dict, trade: dict | None, usage, model: str) -> None:
    r_in, r_out = RATES.get(model, (15, 75))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
    ver = r.get("veredicto", "?")
    icon = "🟢" if ver == "SEÑAL" else "⏸"
    le = r.get("lectura_estructural", {})

    print(f"\n{'='*60}")
    print(f"  L3 {r.get('activo','')} — {r.get('fecha','')}  |  ${r.get('precio_actual',''):,}  [{model}]")
    print(f"{'='*60}")
    print(f"  LECTURA ESTRUCTURAL (datapoint diario):")
    print(f"    Sub-onda actual: {le.get('sub_onda_actual','')}")
    print(f"    Grado:           {le.get('grado','')}")
    print(f"    Estado:          {le.get('estado','')}")
    print(f"    Qué esperar:     {le.get('que_esperar','')}")
    print(f"\n  {icon} {ver}  —  {r.get('direccion','')} ({r.get('modo_entrada','')})")
    print(f"  Calidad: {r.get('calidad_señal','')}  |  Checklist: {r.get('checklist',{}).get('cumplidas','')}/3")
    print(f"{'-'*60}")
    ch = r.get("checklist", {})
    print(f"  S1 {ch.get('s1_retroceso','')}  S2 {ch.get('s2_estructura','')}  S3 {ch.get('s3_linea24','')}")
    print(f"\n  {r.get('razon','')}")

    if trade:
        print(f"\n  PLAN DE TRADE ({r.get('direccion','')}):")
        print(f"    Entrada:  ${trade['entrada']:,}")
        print(f"    Stop:     ${trade['stop']:,}  (riesgo ${trade['riesgo_por_unidad']:,}/unidad)")
        print(f"    O1:       ${trade['O1']:,}  → {trade['O1_accion']}  (R:R {trade['R:R']['O1']}x)")
        print(f"    O2:       ${trade['O2']:,}  → {trade['O2_accion']}  (R:R {trade['R:R']['O2']}x)")
        print(f"    O3:       ${trade['O3']:,}  → {trade['O3_accion']}  (R:R {trade['R:R']['O3']}x)")
        print(f"    R:R > 5x: {'✅ sí' if trade['rr_valido_5x'] else '⚠ no'}")

    print(f"\n  Invalidación: {r.get('invalidacion','')}")
    print(f"\n  Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")

# ── Main ──────────────────────────────────────────────────────────────────────

def build_l3_prompt_from(l1: dict, l2: dict, price_csv: str, date: str) -> str:
    ctx_l1 = json.dumps({k: l1.get(k) for k in ("techo_operativo", "escenarios", "acuerdo",
                                                "divergencia", "niveles_para_l2")}, ensure_ascii=False)
    ctx_l2 = json.dumps({k: l2.get(k) for k in ("escenario_favorecido", "resolutorios_cruzados",
                                                "score_santos", "resumen")}, ensure_ascii=False)
    template = PROMPT_PATH.read_text()
    return template.format(
        asset=ASSET, date=date,
        fecha_l1=l1.get("fecha_analisis", "?"), contexto_l1=ctx_l1,
        fecha_l2=l2.get("fecha", "?"), contexto_l2=ctx_l2,
        candle_count=CANDLE_COUNT, price_data=price_csv,
    )

def build_l3_prompt(date: str, asof: str | None = None) -> tuple[str, dict, dict]:
    l1 = load_json(L1_FILE, "analyzer_weekly.py")
    l2 = load_json(L2_FILE, "monitor_daily.py")
    price_csv = fetch_4h_data(CANDLE_COUNT, asof=asof)
    return build_l3_prompt_from(l1, l2, price_csv, date), l1, l2

def run(model: str | None = None) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")

    print("Cargando contexto L1/L2 y bajando datos 4h...", end=" ", flush=True)
    prompt, l1, l2 = build_l3_prompt(date)
    print("OK")

    # Tiered: Opus solo cuando L2 confirma SEÑAL; Sonnet para el datapoint diario.
    if model is None:
        es_señal = l2.get("nivel_alerta") == "SEÑAL"
        model = "claude-opus-4-8" if es_señal else "claude-sonnet-4-6"
        print(f"L2 = {l2.get('nivel_alerta','?')} → usando {model}")

    print(f"Llamando a {model} (L3)...", end=" ", flush=True)
    result, usage = call_model(prompt, model)
    print("OK")

    # Upgrade forzado: si Sonnet detecta un ABC en formación (posible Modo B), su conteo es
    # menos confiable para esta lectura fina — re-correr con Opus el mismo prompt/contexto
    # para una lectura de mayor calidad antes de decidir el veredicto. Costo solo cuando aplica.
    if model != "claude-opus-4-8" and (result.get("modo_b_check", {}) or {}).get("abc_detectada"):
        print(f"  abc_detectada=true con {model} → forzando upgrade a Opus...", end=" ", flush=True)
        model = "claude-opus-4-8"
        result, usage = call_model(prompt, model)
        print("OK")

    # Decisión de disparo DETERMINISTA en Python (el modelo solo juzga la estructura).
    # Los extremos operativos vienen de L1 (calculados por Python) → Modo B reproducible.
    l1_levels = {
        "techo": l1.get("techo_operativo", {}).get("precio"),
        "operativo": l1.get("niveles_para_l2", {}).get("low_operativo"),
    }
    decision = decidir_veredicto(result, l1_levels)
    result["veredicto"] = decision["veredicto"]
    result["calidad_señal"] = decision["calidad"]
    if decision["modo"]:
        result["modo_entrada"] = decision["modo"]
    result["_decision_python"] = decision["motivo"]
    trade = decision["trade"]
    if trade:
        result["plan_trade"] = trade
    if decision["veredicto"] == "SEÑAL":
        print(f"  ⚡ SEÑAL ({decision['calidad']}, {decision['modo']}): {decision['motivo']}")

    df_amplio = None
    if (result.get("modo_b_check") or {}).get("abc_detectada"):
        df_amplio = fetch_precio_amplio(TICKER, asof=date)

    # Modo sombra: registra qué habría decidido el rediseño de Modo B (sin sustituir L1,
    # pivote verificado por fecha) en paralelo a la decisión real — solo para comparar.
    # No afecta veredicto/trade/alertas. Ver evaluar_modo_b_grado_propio().
    if df_amplio is not None:
        try:
            prod_mb = evaluar_modo_b(result, l1_levels)
            sombra = evaluar_modo_b_grado_propio(result, df_amplio)
            log_path = DATA_DIR / "shadow_modo_b.jsonl"
            with log_path.open("a") as f:
                f.write(json.dumps({
                    "fecha": date, "ticker": TICKER,
                    "dispara_prod": prod_mb["dispara"], "motivo_prod": prod_mb["motivo"],
                    "dispara_sombra": sombra["dispara"], "motivo_sombra": sombra["motivo"],
                }, ensure_ascii=False) + "\n")
            print(f"  🔬 modo sombra registrado: prod={prod_mb['dispara']} sombra={sombra['dispara']} "
                  f"({sombra['motivo'][:70]})")
        except Exception as e:
            print(f"  ⚠ modo sombra no se pudo evaluar: {e}")

    # Alerta anticipada: proyección de C verificada contra datos reales (no la aritmética
    # libre del modelo) — ver compute_alerta_anticipada_grado_propio().
    aa_raw = result.get("alerta_anticipada") or {}
    if aa_raw.get("activa") and df_amplio is not None:
        piv = result.get("pivotes") or {}
        aa_verificada = compute_alerta_anticipada_grado_propio(
            df_amplio, piv.get("abc_a_inicio_fecha"), piv.get("abc_a_inicio"),
            aa_raw.get("a_fin_fecha"), aa_raw.get("a_fin"),
            aa_raw.get("b_fin_fecha"), aa_raw.get("b_fin"))
        if aa_verificada:
            result["alerta_anticipada"] = {"activa": True, **aa_verificada}
            print(f"  🔬 alerta_anticipada verificada: b_fin=${aa_verificada['b_fin']:,.0f} "
                  f"a_size=${aa_verificada['a_size']:,.0f}")
        else:
            result["alerta_anticipada"] = {"activa": False}
            print(f"  ⚠ alerta_anticipada: pivotes no verificables en datos reales — no se alerta")

    # Alerta anticipatoria: ABC en formación, C aún no llegó al target Fibonacci
    aa = result.get("alerta_anticipada") or {}
    if aa.get("activa") and aa.get("nivel_c_1x"):
        d = result.get("direccion", "?")
        print(f"  🔔 ALERTA ANTICIPATORIA {d}: zona C proyectada "
              f"${aa['nivel_c_1x']:,.0f} – ${aa.get('nivel_c_1618', aa['nivel_c_1x']):,.0f}")

    print_report(result, trade, usage, model)

    result["_meta"] = {"generado": date, "modelo": model}
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n  Guardado en: {OUTPUT_FILE}")

    # Actualiza el plan de alertas que vigilará price_watcher.py
    try:
        import alertas
        plan = alertas.generar_plan(result.get("precio_actual"))
        print(f"  Plan de alertas: {len(plan['alertas'])} niveles a vigilar")
    except Exception as e:
        print(f"  ⚠ No se pudo generar el plan de alertas: {e}")

    # Guarda análisis y señales en DB histórica
    try:
        import database
        l2 = json.loads(L2_FILE.read_text()) if L2_FILE.exists() else {}
        n_inv = database.invalidar_señales_pendientes(TICKER, date)
        if n_inv:
            print(f"  {n_inv} señal(es) anterior(es) invalidada(s) — entrada nunca se tocó")
        database.log_analysis(date, TICKER, result, l2.get("nivel_alerta", ""))
        if result.get("veredicto") == "SEÑAL":
            database.log_signal(date, TICKER, result)
    except Exception as e:
        print(f"  ⚠ No se pudo guardar en DB: {e}")

    # Notificación Telegram inmediata cuando hay SEÑAL o alerta anticipatoria
    if result.get("veredicto") == "SEÑAL" and trade:
        d    = result.get("direccion", "")
        cal  = result.get("calidad_señal", "")
        ent  = trade["entrada"]
        stp  = trade["stop"]
        risk = abs(ent - stp)
        def rr(t): return abs(t - ent) / risk if risk > 0 else 0
        msg = (f"⚡ SEÑAL {d} ({cal}) — {date}\n"
               f"Entrada: ${ent:,.0f}\n"
               f"Stop:    ${stp:,.0f}\n"
               f"O1: ${trade['O1']:,.0f} ({rr(trade['O1']):.1f}x) → cerrar 50%\n"
               f"O2: ${trade['O2']:,.0f} ({rr(trade['O2']):.1f}x) → cerrar 25%\n"
               f"O3: ${trade['O3']:,.0f} ({rr(trade['O3']):.1f}x) → cerrar 25%\n"
               f"Precio actual: ${result.get('precio_actual', 0):,.0f}")
        _enviar_telegram(msg)
    aa = result.get("alerta_anticipada") or {}
    if aa.get("activa") and aa.get("nivel_c_1x"):
        d = result.get("direccion", "")
        msg = (f"🔔 ALERTA ANTICIPATORIA {d} — {date}\n"
               f"Zona C proyectada: ${aa['nivel_c_1x']:,.0f} – ${aa.get('nivel_c_1618', aa['nivel_c_1x']):,.0f}\n"
               f"Pre-armar orden límite en esa zona.")
        _enviar_telegram(msg)

    return result

if __name__ == "__main__":
    mdl = sys.argv[1] if len(sys.argv) > 1 else None  # None = auto-tier según L2
    run(mdl)
