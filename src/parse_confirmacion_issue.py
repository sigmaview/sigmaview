"""Parsea el body de un GitHub Issue Form de confirmación de entrada manual y registra la
posición real en la base de datos. Invocado desde
.github/workflows/confirmar_entrada.yml con el body del issue en ISSUE_BODY.
Reemplaza la inferencia automática "se disparó la alerta → asumo que se ejecutó" (ver
alertas._posicion_abierta_real) por una confirmación explícita de Felipe.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import database


def _parse_form(body: str) -> dict:
    """GitHub Issue Forms renderiza cada campo como '### <label>\n\n<valor>\n\n'."""
    secciones = re.split(r"^### +", body, flags=re.MULTILINE)[1:]
    campos = {}
    for s in secciones:
        label, _, resto = s.partition("\n")
        campos[label.strip()] = resto.strip()
    return campos


def _num(valor: str | None) -> float | None:
    if not valor or valor.strip() in ("", "_No response_"):
        return None
    try:
        return float(valor.replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def main():
    body = os.environ["ISSUE_BODY"]
    fecha = (os.environ.get("ISSUE_DATE_RAW") or "")[:10]
    campos = _parse_form(body)

    confirmado = "[x]" in campos.get("Confirmación", "").lower()
    ticker = campos.get("Activo", "").strip()
    direccion = campos.get("Dirección", "").strip()
    entrada = _num(campos.get("Precio de entrada ejecutado"))
    cantidad = _num(campos.get("Cantidad"))
    stop = _num(campos.get("Stop loss"))
    o1 = _num(campos.get("Objetivo 1 (O1)"))
    o2 = _num(campos.get("Objetivo 2 (O2)"))
    o3 = _num(campos.get("Objetivo 3 (O3)"))

    if not confirmado:
        print("NO_CONFIRMADO: el checkbox de confirmación no está marcado, no se registra nada.")
        sys.exit(1)
    if not (ticker and direccion and entrada and cantidad and fecha):
        print(f"FALTAN_CAMPOS: ticker={ticker!r} direccion={direccion!r} "
              f"entrada={entrada!r} cantidad={cantidad!r} fecha={fecha!r}")
        sys.exit(1)

    sid = database.registrar_entrada_manual(
        fecha=fecha, ticker=ticker, direccion=direccion, entrada=entrada,
        cantidad=cantidad, stop=stop, o1=o1, o2=o2, o3=o3,
    )
    print(f"OK: señal id={sid} registrada — {direccion} {ticker} @ {entrada} (cantidad {cantidad})")


if __name__ == "__main__":
    main()
