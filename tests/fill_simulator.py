"""Simulador de ejecución: dado un setup (entrada/stop/objetivos) y una fecha en que se ARMA
la orden, simula el precio hacia adelante y determina si se llenó, cuándo, y el resultado en R.

Clave: la orden se coloca EN la fecha de armado y se llena cuando el precio toca el nivel
(incluyendo la misma vela del armado en adelante). Así una orden pre-puesta SÍ atrapa la mecha,
a diferencia de descubrir la entrada después de que el fondo ya pasó.

Gestión Enfoque B: O1 cierra 50%, O2 cierra 25% (sube stop a entrada/BE), O3 cierra 25%.
"""
import yfinance as yf

def simular(direccion: str, entrada: float, stop: float,
            o1: float, o2: float, o3: float,
            fecha_armado: str, dias: int = 400) -> dict:
    long = direccion.upper() == "LONG"
    df = yf.Ticker("BTC-USD").history(start=fecha_armado, interval="1d")
    df = df.head(dias)
    if df.empty:
        return {"error": "sin datos"}
    df.index = df.index.strftime("%Y-%m-%d")

    riesgo = abs(entrada - stop)
    R = lambda obj: abs(obj - entrada) / riesgo if riesgo else 0

    # ── 1. Fill de la entrada ──
    # La orden se llena cuando el precio TOCA el nivel (el rango Low-High de la vela lo cruza).
    fill_dia = None
    for dia, row in df.iterrows():
        if row["Low"] <= entrada <= row["High"]:
            fill_dia = dia
            break
    if fill_dia is None:
        return {"filled": False, "motivo": "el precio nunca tocó la entrada", "r": 0.0}

    # ── 2. Recorrido posterior al fill: stop vs objetivos ──
    post = df[df.index >= fill_dia]
    stop_actual = stop
    cerrado = 0.0          # fracción cerrada
    r_total = 0.0
    eventos = []
    objetivos = [("O1", o1, 0.50), ("O2", o2, 0.25), ("O3", o3, 0.25)]
    idx_obj = 0

    for dia, row in post.iterrows():
        # ¿Stop primero? (conservador: si stop y objetivo el mismo día, stop gana)
        stop_hit = row["Low"] <= stop_actual if long else row["High"] >= stop_actual
        if stop_hit:
            r_stop = (stop_actual - entrada) / riesgo if long else (entrada - stop_actual) / riesgo
            r_total += (1 - cerrado) * r_stop
            eventos.append(f"{dia}: STOP ${stop_actual:,.0f} (resto {1-cerrado:.0%})")
            return {"filled": True, "fill_dia": fill_dia, "cierre_dia": dia,
                    "r": round(r_total, 2), "resultado": "STOP" if cerrado == 0 else "STOP parcial",
                    "eventos": eventos}
        # ¿Objetivos?
        while idx_obj < len(objetivos):
            nombre, nivel, frac = objetivos[idx_obj]
            alcanzado = row["High"] >= nivel if long else row["Low"] <= nivel
            if not alcanzado:
                break
            r_total += frac * R(nivel)
            cerrado += frac
            eventos.append(f"{dia}: {nombre} ${nivel:,.0f} (+{frac:.0%} a {R(nivel):.1f}R)")
            if nombre == "O2":
                stop_actual = entrada  # breakeven
            idx_obj += 1
        if idx_obj >= len(objetivos):
            return {"filled": True, "fill_dia": fill_dia, "cierre_dia": dia,
                    "r": round(r_total, 2), "resultado": "O3 (completo)", "eventos": eventos}

    # Trade aún abierto al final de los datos
    ultimo = post["Close"].iloc[-1]
    r_abierto = (ultimo - entrada) / riesgo if long else (entrada - ultimo) / riesgo
    r_total += (1 - cerrado) * r_abierto
    eventos.append(f"{post.index[-1]}: ABIERTO en ${ultimo:,.0f} ({r_abierto:+.1f}R sobre resto)")
    return {"filled": True, "fill_dia": fill_dia, "cierre_dia": None,
            "r": round(r_total, 2), "resultado": "ABIERTO", "eventos": eventos}

if __name__ == "__main__":
    import sys
    # demo: LONG abril 2025 armado el 1-abr (antes del fondo)
    r = simular("LONG", 74436, 73000, 92000, 108268, 134000, "2025-04-01")
    print(r)
