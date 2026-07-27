"""Test de un clasificador de tipología de pauta correctiva (zigzag / plana / triángulo).
No toca el prompt de producción: construye una copia local de level1_weekly.txt con un paso
nuevo (PASO 2.2) que pide los sub-pivotes A/B/[C/D] de la corrección EN CURSO desde
techo_operativo, llama al modelo una vez con datos reales, verifica esos sub-pivotes contra
las velas semanales reales (igual patrón que compute_alerta_anticipada_grado_propio en
signal_generator.py) y clasifica la tipología con las reglas de "Pautas Correctivas" de
Enrique Santos (Tabla 1 de planas, regla de 61.8% de zigzag, requisitos de triángulo).
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import analyzer_weekly as l1m

PROMPT_PATH = Path(__file__).parent.parent / "src" / "prompts" / "level1_weekly.txt"

NUEVA_INSTRUCCION = """
PASO 2.2 — SUB-PIVOTES DE LA CORRECCIÓN EN CURSO (NUEVO, staging)
Para la corrección que describes en "posicion_hoy" (la que parte de techo_operativo y aún
NO ha completado su onda mayor), narra los sub-pivotes que YA VES confirmados en los datos:
fin de la onda A y fin de la onda B como mínimo. Si además distingues una estructura de
cinco tramos correctivos contrayéndose (posible triángulo), narra también fin de C y fin de D.
NO incluyas más de lo que ya está confirmado en los datos — no proyectes hacia el futuro.
techo_operativo YA es el origen de A, no lo repitas en esta lista.
Mismo estándar de precisión que en PASO 2.1: el precio de cada sub-pivote es SIEMPRE el
extremo real de la vela (High si es techo, Low si es suelo), nunca el cierre (Close). Busca
la fila exacta en los datos para cada fecha antes de responder.
Si la corrección todavía no tiene ni siquiera la onda A completa, deja la lista vacía.
"""

CAMPO_SCHEMA = """      "correccion_actual": {{
        "sub_pivotes": [{{"onda": "<A|B|C|D>", "precio": <número>, "fecha": "<YYYY-MM-DD>"}}]
      }},
"""


def build_prompt_test(price_csv, date, asset, start_date):
    template = PROMPT_PATH.read_text()
    template = template.replace(
        "PASO 3 — ELIMINACIÓN POR REGLAS DURAS",
        NUEVA_INSTRUCCION + "\nPASO 3 — ELIMINACIÓN POR REGLAS DURAS",
    )
    template = template.replace(
        '      "extension": "<cuál onda extendida y multiplicador>"',
        CAMPO_SCHEMA + '      "extension": "<cuál onda extendida y multiplicador>"',
    )
    return template.format(price_data=price_csv, date=date, asset=asset, start_date=start_date)


def validar_c_real(objetivo_c: dict, b_precio: float, c_real: float) -> dict | None:
    """Si ya hay un pivote verificado MÁS ALLÁ de B (una C real, aunque no se haya usado
    para clasificar), compara dónde cayó contra la banda proyectada por la fórmula elegida.
    No asume qué campo es numéricamente mayor — distintas tipologías/direcciones invierten
    el orden — solo junta los límites no-nulos y los ordena."""
    if not objetivo_c or c_real is None:
        return None
    valores = [v for v in objetivo_c.values() if v is not None]
    if not valores:
        return None
    if len(valores) >= 2:
        lo, hi = min(valores), max(valores)
        return {
            "c_real": round(c_real, 2), "banda_proyectada": [round(lo, 2), round(hi, 2)],
            "dentro_de_banda": lo <= c_real <= hi,
        }
    # Un solo límite (rango abierto: "débil" >100% o "fuerte+" <100%) — el campo presente
    # indica si lo abierto es hacia más extremo (min sin max) o hacia b (max sin min).
    bound = valores[0]
    campo = next(k for k, v in objetivo_c.items() if v is not None)
    signo_dir = 1 if bound >= b_precio else -1
    dist_c = (c_real - b_precio) * signo_dir
    dist_bound = (bound - b_precio) * signo_dir
    if campo == "min":  # abierto hacia más extremo que bound
        dentro = dist_c >= dist_bound
    else:  # campo == "max": abierto entre b y bound (menos extremo)
        dentro = 0 <= dist_c <= dist_bound
    return {
        "c_real": round(c_real, 2), "limite_abierto": {campo: round(bound, 2)},
        "dentro_de_banda": dentro,
    }


# ── Clasificador determinístico (Pautas Correctivas, Enrique Santos) ───────────────────

def classify_correccion(df: pd.DataFrame, techo_precio: float, techo_fecha: str,
                         tipo_techo: str, sub_pivotes: list, tol: float = 0.05) -> dict:
    """Verifica los sub-pivotes A/B/[C/D] narrados contra velas semanales reales y clasifica
    la tipología de la corrección. Nunca fuerza una clasificación si los pivotes no calzan
    con los datos reales o no alcanzan para distinguir entre tipologías."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", techo_fecha or "")
    if not m:
        return {"tipo": "indeterminado", "motivo": "techo_fecha inválida"}
    techo_ts = pd.Timestamp(m.group(0))
    es_techo = "mín" not in (tipo_techo or "").lower()  # True si techo_operativo es un máximo

    pivs = sorted(
        [p for p in (sub_pivotes or []) if p.get("onda") in ("A", "B", "C", "D") and p.get("fecha")],
        key=lambda p: p["fecha"],
    )
    if len(pivs) < 2:
        return {"tipo": "indeterminado", "motivo": "se necesitan al menos A y B narrados"}

    verificados = []
    piso = techo_ts
    for i, p in enumerate(pivs):
        fm = re.search(r"\d{4}-\d{2}-\d{2}", p.get("fecha", ""))
        if not fm:
            return {"tipo": "indeterminado", "motivo": f"fecha inválida en onda {p.get('onda')}"}
        fecha = pd.Timestamp(fm.group(0))
        if fecha <= piso:
            return {"tipo": "indeterminado",
                     "motivo": f"onda {p.get('onda')} no es posterior al pivote previo"}
        # A,C,.. son del lado OPUESTO al techo; B,D,.. vuelven al mismo lado que el techo.
        es_techo_pivote = (not es_techo) if i % 2 == 0 else es_techo
        desde = max(fecha - pd.Timedelta(days=7), piso)
        ventana = df[(df.index >= desde) & (df.index <= fecha + pd.Timedelta(days=7))]
        if ventana.empty:
            return {"tipo": "indeterminado",
                     "motivo": f"sin velas cerca de {p.get('fecha')} (onda {p.get('onda')})"}
        real = float(ventana["High"].max()) if es_techo_pivote else float(ventana["Low"].min())
        narrado = float(p.get("precio") or 0)
        if narrado and abs(real - narrado) / narrado > tol:
            return {"tipo": "indeterminado",
                     "motivo": (f"onda {p.get('onda')}: narrado ${narrado:,.0f} vs real "
                                f"${real:,.0f} (fuera de tolerancia {tol:.0%})")}
        verificados.append({"onda": p["onda"], "precio": round(real, 2), "fecha": fm.group(0)})
        piso = fecha

    a, b = verificados[0], verificados[1]
    a_size = abs(a["precio"] - techo_precio)
    b_size = abs(b["precio"] - a["precio"])
    if a_size == 0:
        return {"tipo": "indeterminado", "motivo": "onda A de tamaño cero",
                 "pivotes_verificados": verificados}
    b_sobre_a = b_size / a_size
    signo = -1 if es_techo else 1  # techo=máximo → la corrección sigue cayendo; mínimo → sigue subiendo

    # ── Triángulo: 4+ pivotes, retrocesos sucesivos >=50% (salvo el último) y b/a 38.2%-261.8% ──
    if len(verificados) >= 4 and 0.382 <= b_sobre_a <= 2.618:
        c, d = verificados[2], verificados[3]
        c_sobre_b = abs(c["precio"] - b["precio"]) / b_size if b_size else 0
        if c_sobre_b >= 0.5:
            mayor = max(a_size, b_size, abs(c["precio"] - b["precio"]))
            return {
                "tipo": "triangulo", "b_sobre_a": round(b_sobre_a, 4),
                "pivotes_verificados": verificados,
                "rango_objetivo_post_rotura": {
                    "min": round(mayor * 0.75, 2), "max": round(mayor * 1.25, 2),
                },
                "nota": ("Magnitud del recorrido esperado tras la rotura confirmada de la "
                         "línea b-d (75%-125% de la onda más larga) — se suma/resta desde el "
                         "precio de rotura cuando ocurra, no desde B."),
            }

    # Si ya hay un pivote más allá de B (narrado como C, aunque no se use para clasificar
    # zigzag/plana), sirve para validar la proyección contra lo que YA ocurrió.
    c_real = verificados[2]["precio"] if len(verificados) >= 3 else None

    # ── Zigzag vs Plana, según retroceso de B sobre A ──
    if b_sobre_a <= 0.618:
        # Pautas Correctivas 3.1: "el intervalo comprendido entre el 61,8% de la onda A
        # proyectado A PARTIR DE LA ONDA B (relación interna) y el 161,8% de A proyectado
        # DESDE EL FINAL DE A (relación externa)" — son dos anclas distintas, no la misma.
        # Interno e igualdad se miden desde B; el externo (el caso raro, casi nunca se
        # cumple según Santos) se mide desde el final de A, no desde B.
        objetivo_c = {
            "interno_61_8": round(b["precio"] + signo * 0.618 * a_size, 2),
            "igualdad_100": round(b["precio"] + signo * 1.0 * a_size, 2),
            "externo_161_8": round(a["precio"] + signo * 1.618 * a_size, 2),
        }
        resultado = {
            "tipo": "zigzag", "b_sobre_a": round(b_sobre_a, 4),
            "pivotes_verificados": verificados, "objetivo_c": objetivo_c,
        }
    else:
        # Tabla 1 (pág. 17, Pautas Correctivas) — fuerza de B y rango de retroceso C/B esperado
        if b_sobre_a <= 0.80:
            fuerza, c_min_pct, c_max_pct = "débil", 1.0, None
        elif b_sobre_a <= 1.00:
            fuerza, c_min_pct, c_max_pct = "normal", 1.0, 1.382
        elif b_sobre_a <= 1.236:
            fuerza, c_min_pct, c_max_pct = "fuerte", 1.0, 1.0
        else:
            fuerza, c_min_pct, c_max_pct = "fuerte+", None, 1.0

        objetivo_c = {
            "min": round(b["precio"] + signo * c_min_pct * b_size, 2) if c_min_pct is not None else None,
            "max": round(b["precio"] + signo * c_max_pct * b_size, 2) if c_max_pct is not None else None,
        }
        resultado = {
            "tipo": "plana", "b_sobre_a": round(b_sobre_a, 4), "fuerza_b": fuerza,
            "pivotes_verificados": verificados, "objetivo_c": objetivo_c,
        }

    validacion = validar_c_real(objetivo_c, b["precio"], c_real)
    if validacion:
        resultado["validacion_c"] = validacion
    return resultado


def main():
    print("Bajando datos BTC semanales (mismo fetch que producción)...")
    df = l1m.fetch_weekly_df("BTC-USD", start_date="2014-01-01")
    price_csv = l1m.df_to_csv(df)
    print(f"OK ({len(df)} velas)\n")

    prompt = build_prompt_test(price_csv, "2026-06-25", "BTC/USD", "2014-01-01")

    print(f"Llamando a {l1m.MODEL} con el prompt de prueba...")
    result, usage = l1m.call_model(prompt)
    print("OK\n")

    out_path = Path(__file__).parent.parent / "data" / "test_pautas_correctivas_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Resultado completo guardado en: {out_path}\n")

    techo = result.get("techo_operativo", {})
    techo_precio = float(techo.get("precio", 0))
    techo_fecha = techo.get("fecha", "")
    techo_tipo = techo.get("tipo", "")
    print("=" * 70)
    print(f"TECHO OPERATIVO: ${techo_precio:,.0f} ({techo_fecha}, tipo={techo_tipo})\n")

    for e in result.get("escenarios", []):
        print(f"[{e.get('id')}] {e.get('etiqueta_macro')}")
        print(f"  posicion_hoy (texto libre del modelo): {e.get('posicion_hoy')}")
        sub_pivotes = (e.get("correccion_actual") or {}).get("sub_pivotes") or []
        if not sub_pivotes:
            print("  correccion_actual.sub_pivotes: AUSENTE o vacío — no clasificable aún\n")
            continue
        print(f"  sub_pivotes narrados: {sub_pivotes}")
        clasif = classify_correccion(df, techo_precio, techo_fecha, techo_tipo, sub_pivotes)
        print(f"  CLASIFICACIÓN: {json.dumps(clasif, ensure_ascii=False, indent=4)}\n")

    print("=" * 70)
    rates = {"claude-opus-4-8": (15, 75)}
    r_in, r_out = rates.get(l1m.MODEL, (15, 75))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
    print(f"Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")


if __name__ == "__main__":
    main()
