"""Test del fix de Modo C: distinguir si la rotura inicia una onda EXTENDIDA (W3, típico)
o una onda NO extendida (W5/C terminal, cuando W3 o A ya fue la extendida) — regla Santos:
"una onda impulsiva es la extendida >161.8%; las dos NO extendidas tienden a igualdad o 61.8%".
No toca el prompt de producción: construye una copia local con la instrucción + campo nuevo,
llama al modelo una vez con datos reales (reusa L1/L2 ya cacheados) y compara el trade viejo
(siempre 1.0/1.618/2.618×W1) contra el nuevo (depende de onda_iniciada).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import signal_generator as l3m

PROMPT_PATH = Path(__file__).parent.parent / "src" / "prompts" / "level3_modoB_fuerte.txt"

NUEVA_MODO_C = """MODO C — Entrada en ROTURA/continuación (cuando el precio YA rompió y está en W3 o W5):
- El retroceso de W2 ya ocurrió; el precio perfora el extremo de W1 iniciando la siguiente onda impulsiva.
- Entrada en la rotura del extremo de W1 (no esperes un retroceso que ya pasó).
- Stop AJUSTADO sobre el swing de W2 (no en el origen de W1) → mejor R:R.
- CRÍTICO — clasifica qué onda inicia esta rotura (regla Santos: "una onda impulsiva es la
  EXTENDIDA >161.8% de las otras; las dos NO extendidas tienden a igualdad o 61.8% entre sí"):
  - "W3": ninguna onda previa de este impulso fue claramente la extendida — es razonable
    esperar que ESTA onda (la que inicia ahora) sea la extendida. O1=1.0×W1, O2=1.618×W1,
    O3=2.618×W1 (fórmula sin cambios).
  - "W5_o_C_terminal": la onda 3 (o la onda A, si esto es una corrección) YA fue la extendida
    de este impulso — esta onda terminal normalmente NO se extiende, tiende a igualdad o
    61.8% de W1, NUNCA a 2.618x. O1=0.382×W1, O2=0.618×W1, O3=1.0×W1.
  - Declara tu clasificación en pivotes.onda_iniciada. Si tienes dudas, usa "W5_o_C_terminal"
    (más conservador — evita proyectar objetivos que ya no tienen espacio estructural).
- Úsalo cuando la lectura estructural dice que estamos entrando/dentro de una onda impulsiva."""

CAMPO_SCHEMA = '    "onda_iniciada": "<W3|W5_o_C_terminal — solo si modo_entrada=C_breakout, si no null>",\n'


def build_prompt_test(l1, l2, price_csv, date, asset, candle_count, timeframe_label):
    template = PROMPT_PATH.read_text()
    old_modo_c = """MODO C — Entrada en ROTURA/continuación (cuando el precio YA rompió y está en W3):
- El retroceso de W2 ya ocurrió; el precio perfora el extremo de W1 iniciando W3.
- Entrada en la rotura del extremo de W1 (no esperes un retroceso que ya pasó).
- Stop AJUSTADO sobre el swing de W2 (no en el origen de W1) → mejor R:R.
- O1=1.0×W1, O2=1.618×W1, O3=2.618×W1 de continuación desde la entrada.
- Úsalo cuando la lectura estructural dice que estamos entrando/dentro de W3."""
    assert old_modo_c in template, "no se encontró el bloque MODO C original"
    template = template.replace(old_modo_c, NUEVA_MODO_C)

    old_pivote_line = '    "w2_swing": <precio del extremo del retroceso W2 = stop en Modo C, o null>,\n'
    assert old_pivote_line in template, "no se encontró la línea w2_swing"
    template = template.replace(old_pivote_line, old_pivote_line + CAMPO_SCHEMA)

    ctx_l1 = json.dumps({k: l1.get(k) for k in ("techo_operativo", "escenarios", "acuerdo",
                                                "divergencia", "niveles_para_l2")}, ensure_ascii=False)
    ctx_l2 = json.dumps({k: l2.get(k) for k in ("escenario_favorecido", "resolutorios_cruzados",
                                                "score_santos", "resumen")}, ensure_ascii=False)
    return template.format(
        asset=asset, date=date,
        fecha_l1=l1.get("fecha_analisis", "?"), contexto_l1=ctx_l1,
        fecha_l2=l2.get("fecha", "?"), contexto_l2=ctx_l2,
        candle_count=candle_count, price_data=price_csv,
        timeframe_label=timeframe_label,
    )


def compute_trade_nuevo(direccion, modo, piv):
    """Copia de compute_trade() con la rama Modo C dependiente de onda_iniciada."""
    long = direccion.upper() == "LONG"
    sign = 1 if long else -1

    if modo == "C_breakout":
        o = float(piv["w1_origen"])
        extremo = float(piv["w1_fin"])
        w2 = float(piv["w2_swing"])
        w1 = abs(extremo - o)
        entrada = extremo
        stop = w2
        if piv.get("onda_iniciada") == "W5_o_C_terminal":
            m1, m2, m3 = 0.382, 0.618, 1.0
        else:
            m1, m2, m3 = 1.0, 1.618, 2.618
        o1 = entrada + sign * m1 * w1
        o2 = entrada + sign * m2 * w1
        o3 = entrada + sign * m3 * w1
    else:
        raise ValueError("este test solo cubre C_breakout")

    riesgo = abs(entrada - stop)
    return {
        "entrada": round(entrada, 2), "stop": round(stop, 2),
        "O1": round(o1, 2), "O2": round(o2, 2), "O3": round(o3, 2),
        "R:R": {"O1": round(abs(o1 - entrada) / riesgo, 2),
                "O2": round(abs(o2 - entrada) / riesgo, 2),
                "O3": round(abs(o3 - entrada) / riesgo, 2)},
    }


def main():
    print("Cargando L1/L2 cacheados (sin re-llamar a esos niveles)...")
    l1 = l3m.load_json(l3m.DATA_DIR / "l1_btc_latest.json", "L1")
    l2 = l3m.load_json(l3m.DATA_DIR / "l2_btc_latest.json", "L2")
    price_csv = l3m.fetch_price_data(l3m.CANDLE_COUNT, ticker="BTC-USD", modo="4h_resample")
    print("OK\n")

    prompt = build_prompt_test(l1, l2, price_csv, "2026-06-25", l3m.ASSET, l3m.CANDLE_COUNT, "4 HORAS")

    print("Llamando a claude-opus-4-8...")
    result, usage = l3m.call_model(prompt, "claude-opus-4-8")
    print("OK\n")

    out_path = Path(__file__).parent.parent / "data" / "test_modo_c_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Resultado completo guardado en: {out_path}\n")

    print("=" * 70)
    print("CLASIFICACIÓN DEL MODELO:")
    print("  modo_entrada:", result.get("modo_entrada"))
    print("  sub_onda_actual:", result.get("lectura_estructural", {}).get("sub_onda_actual"))
    onda_iniciada = result.get("pivotes", {}).get("onda_iniciada")
    print("  onda_iniciada (campo nuevo):", onda_iniciada)

    if result.get("modo_entrada") != "C_breakout":
        print("\n  (Este análisis no salió en Modo C — no aplica comparación de targets.)")
        return

    piv = result["pivotes"]
    direccion = result["direccion"]

    trade_viejo = l3m.compute_trade(direccion, "C_breakout", piv)
    trade_nuevo = compute_trade_nuevo(direccion, "C_breakout", piv)

    print("\n" + "=" * 70)
    print("COMPARACIÓN DE TARGETS — viejo (siempre 1.0/1.618/2.618×W1) vs nuevo (depende de onda):")
    print(f"  Entrada:  ${trade_viejo['entrada']:,}  (igual en ambos)")
    print(f"  Stop:     ${trade_viejo['stop']:,}  (igual en ambos)")
    for k in ("O1", "O2", "O3"):
        print(f"  {k}:  viejo ${trade_viejo[k]:,}  (R:R {trade_viejo['R:R'][k]}x)   ->   "
              f"nuevo ${trade_nuevo[k]:,}  (R:R {trade_nuevo['R:R'][k]}x)")

    print("\n  Para contexto, la razón del modelo cita zona objetivo (texto libre):")
    print("   ", result.get("razon", "")[:300])

    rates = {"claude-opus-4-8": (15, 75)}
    r_in, r_out = rates.get("claude-opus-4-8", (15, 75))
    cost = (usage.input_tokens * r_in + usage.output_tokens * r_out) / 1_000_000
    print(f"\nTokens: {usage.input_tokens} in / {usage.output_tokens} out — ${cost:.4f} USD")


if __name__ == "__main__":
    main()
