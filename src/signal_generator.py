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
        piv["abc_c_fin"] = float(l1_levels["operativo"])
        piv["abc_a_inicio"] = float(l1_levels["techo"])
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

    rr = trade["R:R"].get(TARGET_GATE)
    if not mb.get("invalidacion_clara", True):
        return {"dispara": False, "motivo": "invalidación no clara", "rr_python": rr, "trade": trade}
    if rr is None or rr < RR_MINIMO:
        return {"dispara": False, "motivo": f"R:R({TARGET_GATE})={rr} < {RR_MINIMO}x", "rr_python": rr, "trade": trade}

    return {"dispara": True, "motivo": f"c/a={c_a} OK, R:R({TARGET_GATE})={rr}x ≥ {RR_MINIMO}x, entrada vigente",
            "rr_python": rr, "trade": trade}

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
        o1 = entrada + sign * 1.0 * w1           # objetivos de continuación W3
        o2 = entrada + sign * 1.618 * w1
        o3 = entrada + sign * 2.618 * w1
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
