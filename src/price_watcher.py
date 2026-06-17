"""Vigila el precio vs el plan de alertas y manda Telegram cuando se toca un nivel.
NO usa IA — solo compara precio (gratis en tokens). Pensado para correr seguido (cron/Actions).

Requiere: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el entorno.
Uso: TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... python3 src/price_watcher.py
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
PLAN_FILE = DATA_DIR / "alertas_activas.json"
TICKER = "BTC-USD"

def precio_actual() -> float:
    df = yf.Ticker(TICKER).history(period="1d", interval="1m")
    if df.empty:
        df = yf.Ticker(TICKER).history(period="5d", interval="1h")
    return float(df["Close"].iloc[-1])

def enviar_telegram(mensaje: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("  (sin TELEGRAM_TOKEN/CHAT_ID — mostrando en consola)")
        print(f"  >> {mensaje}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": mensaje}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        r.read()

def gatillada(alerta: dict, precio: float) -> bool:
    if alerta["direccion"] == "cruce_abajo":
        return precio <= alerta["nivel"]
    return precio >= alerta["nivel"]

def check() -> None:
    if not PLAN_FILE.exists():
        sys.exit("ERROR: no hay plan de alertas. Corre alertas.py (o el pipeline) primero.")
    plan = json.loads(PLAN_FILE.read_text())
    precio = precio_actual()

    pendientes = [a for a in plan["alertas"] if not a["fired"]]
    disparadas = 0
    for a in pendientes:
        if gatillada(a, precio):
            enviar_telegram(f"{a['mensaje']}\n(precio actual ${precio:,.0f})")
            a["fired"] = True
            disparadas += 1
            try:
                import database
                database.log_alert_fired(TICKER, a, precio)
            except Exception:
                pass

    if disparadas:
        PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2))

    activas = sum(1 for a in plan["alertas"] if not a["fired"])
    print(f"Precio ${precio:,.0f} — {disparadas} disparada(s), {activas} activa(s)")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        enviar_telegram("✅ SigmaView: prueba de conexión Telegram OK.")
        print("Mensaje de prueba enviado.")
    else:
        check()
