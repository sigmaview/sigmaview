"""Test del campo nuevo 'pivotes_onda' en L1 (conteo estructurado para graficar).
No toca el prompt de producción: construye una copia local con el campo agregado,
llama al modelo una vez con datos reales y reporta calidad + regresión de campos existentes.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import analyzer_weekly as l1m

PROMPT_PATH = Path(__file__).parent.parent / "src" / "prompts" / "level1_weekly.txt"

NUEVA_INSTRUCCION = """
PASO 2.1 — PIVOTES DEL CONTEO (NUEVO, para graficar)
Para cada escenario, además de wave_III_fin, entrega TODOS los pivotes principales
de SU conteo en orden cronológico (no solo donde termina Wave III) — cada punto
donde empieza/termina una onda del grado relevante para ese escenario, con fecha
y precio reales de las velas. Incluye desde el inicio del conteo hasta el último
pivote confirmado (no proyectes el futuro aquí, eso ya está en objetivo/se_confirma_si).
Sé preciso con fecha y precio — deben corresponder a velas reales de los datos dados.
El precio del pivote es SIEMPRE el extremo de la vela (High si es techo, Low si es suelo),
NUNCA el cierre (Close) — incluso para el pivote inicial del conteo (el primero de la lista,
el de fecha más antigua, suele equivocarse usando Close: revísalo dos veces).
Antes de responder, para CADA pivote busca su fila exacta en los datos y copia el valor de
la columna High (si "tipo"="techo") o Low (si "tipo"="suelo") de esa fila — no la columna Close.
IMPORTANTE: el PRIMER elemento del array siempre debe ser el ORIGEN del conteo — el punto
desde el cual PARTE la onda I (no donde la onda I termina). Este origen NO es ambiguo: es
un punto fijo (el extremo previo desde el cual arranca el impulso), inclúyelo siempre con
"onda": "0". Luego sigue con "I" (fin de onda I), "II", etc.
"""

CAMPO_SCHEMA = """      "pivotes_onda": [
        {{"onda": "<0|I|II|III|IV|V|A|B|C según corresponda>", "precio": <número>, "fecha": "<YYYY-MM-DD>"}}
      ],
"""


def build_prompt_test(price_csv, date, asset, start_date):
    template = PROMPT_PATH.read_text()
    # Inserta la instrucción nueva después de PASO 2
    template = template.replace(
        "PASO 3 — ELIMINACIÓN POR REGLAS DURAS",
        NUEVA_INSTRUCCION + "\nPASO 3 — ELIMINACIÓN POR REGLAS DURAS",
    )
    # Inserta el campo nuevo dentro del schema de cada escenario, antes de "extension"
    template = template.replace(
        '      "extension": "<cuál onda extendida y multiplicador>"',
        CAMPO_SCHEMA + '      "extension": "<cuál onda extendida y multiplicador>"',
    )
    return template.format(price_data=price_csv, date=date, asset=asset, start_date=start_date)


def main():
    print("Bajando datos BTC semanales (mismo fetch que producción)...")
    df = l1m.fetch_weekly_df("BTC-USD", start_date="2014-01-01")
    price_csv = l1m.df_to_csv(df)
    print(f"OK ({len(df)} velas)\n")

    prompt = build_prompt_test(price_csv, "2026-06-22", "BTC/USD", "2014-01-01")

    print(f"Llamando a {l1m.MODEL} con el prompt de prueba...")
    result, usage = l1m.call_model(prompt)
    print("OK\n")

    out_path = Path(__file__).parent.parent / "data" / "test_l1_pivotes_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Resultado completo guardado en: {out_path}\n")

    # ── Reporte de calidad ──────────────────────────────────────────────────
    print("=" * 70)
    print("REGRESIÓN — campos existentes presentes y con forma esperada:")
    campos_top = ["fecha_analisis", "activo", "techo_operativo", "escenarios_eliminados",
                  "escenarios", "acuerdo", "divergencia", "niveles_para_l2", "resumen"]
    for c in campos_top:
        ok = c in result
        print(f"  [{'OK' if ok else 'FALTA'}] {c}")

    print("\n" + "=" * 70)
    print(f"ESCENARIOS ({len(result.get('escenarios', []))}):")
    for e in result.get("escenarios", []):
        print(f"\n  [{e.get('id')}] {e.get('etiqueta_macro')}")
        print(f"    wave_III_fin (texto, ya existía): {e.get('wave_III_fin')}")
        pivotes = e.get("pivotes_onda")
        if not pivotes:
            print("    pivotes_onda: AUSENTE o vacío")
            continue
        print(f"    pivotes_onda ({len(pivotes)} puntos):")
        fechas = []
        for p in pivotes:
            print(f"      {p.get('onda'):>6}  {p.get('fecha'):>12}  ${p.get('precio'):,}")
            fechas.append(p.get("fecha"))
        # Chequeo de orden cronológico
        ordenado = fechas == sorted(fechas)
        print(f"    Orden cronológico: {'OK' if ordenado else 'FALLA — fechas: ' + str(fechas)}")
        # Chequeo de consistencia: ¿el último pivote de tipo V/III coincide con techo_operativo/wave_III_fin?
        techo = result.get("techo_operativo", {})
        techo_precio = techo.get("precio")
        match_techo = any(abs(p.get("precio", 0) - techo_precio) < 1 for p in pivotes) if techo_precio else None
        print(f"    ¿Algún pivote coincide con techo_operativo (${techo_precio:,})?: {match_techo}")

    print("\n" + "=" * 70)
    rates = {"claude-opus-4-8": (15, 75)}
    r_in, r_out = rates.get(l1m.MODEL, (15, 75))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
    print(f"Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")


if __name__ == "__main__":
    main()
