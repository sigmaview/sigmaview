"""Base de datos SQLite para SigmaView.
Acumula historia de precios (1h candles) y resultados del pipeline.
"""
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "sigmaview.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            ticker TEXT NOT NULL,
            ts     TEXT NOT NULL,
            open   REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (ticker, ts)
        );
        CREATE TABLE IF NOT EXISTS analysis (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha           TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            precio_actual   REAL,
            veredicto       TEXT,
            modo_entrada    TEXT,
            sub_onda        TEXT,
            sesgo_macro     TEXT,
            fase_impulso    TEXT,
            calidad_senal   TEXT,
            l2_alerta       TEXT,
            modelo          TEXT,
            UNIQUE(fecha, ticker)
        );
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha       TEXT NOT NULL,
            ticker      TEXT NOT NULL,
            modo        TEXT,
            direccion   TEXT,
            calidad     TEXT,
            entrada     REAL,
            stop        REAL,
            o1 REAL, o2 REAL, o3 REAL,
            rr_o1 REAL, rr_o2 REAL, rr_o3 REAL,
            resultado_r REAL,
            estado      TEXT DEFAULT 'ABIERTO',
            UNIQUE(fecha, ticker)
        );
        CREATE TABLE IF NOT EXISTS analysis_weekly (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha                TEXT NOT NULL UNIQUE,
            ticker               TEXT NOT NULL,
            techo_precio         REAL,
            techo_fecha          TEXT,
            escenarios_json      TEXT,
            acuerdo_sesgo        TEXT,
            acuerdo_niveles      TEXT,
            acuerdo_operar       TEXT,
            divergencia_pregunta TEXT,
            divergencia_resuelve TEXT,
            resumen              TEXT,
            modelo               TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts_fired (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at      TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            alert_id      TEXT,
            tipo          TEXT,
            nivel         REAL,
            precio_actual REAL,
            mensaje       TEXT
        );
        """)
    # Migración: agrega columnas nuevas a signals si no existen (DB ya creada)
    with _conn() as c:
        for col in ["o1_hit INTEGER DEFAULT 0", "o2_hit INTEGER DEFAULT 0",
                    "o3_hit INTEGER DEFAULT 0", "fecha_cierre TEXT", "mfe_48h REAL"]:
            try:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        try:
            c.execute("ALTER TABLE analysis ADD COLUMN analizado_at TEXT")
        except sqlite3.OperationalError:
            pass


def upsert_ohlcv(ticker: str, df: pd.DataFrame) -> int:
    """Inserta candles 1h en la DB (INSERT OR IGNORE). Retorna n filas nuevas."""
    init_db()
    rows = []
    for ts, row in df.iterrows():
        ts_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
        rows.append((
            ticker, ts_str,
            float(row.get("Open") or 0),
            float(row.get("High") or 0),
            float(row.get("Low") or 0),
            float(row.get("Close") or 0),
            float(row.get("Volume") or 0),
        ))
    inserted = 0
    with _conn() as conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                "INSERT OR IGNORE INTO ohlcv (ticker,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                r,
            )
            inserted += cur.rowcount
    return inserted


def fetch_4h_from_db(ticker: str, n_candles: int, asof: str | None = None) -> str | None:
    """Devuelve CSV de velas 4h resampleadas desde la DB. None si no hay suficientes datos."""
    init_db()
    if asof:
        query = ("SELECT ts,open,high,low,close,volume FROM ohlcv "
                 "WHERE ticker=? AND ts < ? ORDER BY ts")
        params = [ticker, f"{asof} 23:59"]
    else:
        query = "SELECT ts,open,high,low,close,volume FROM ohlcv WHERE ticker=? ORDER BY ts"
        params = [ticker]

    with _conn() as c:
        df = pd.read_sql_query(query, c, params=params, parse_dates=["ts"], index_col="ts")

    if df.empty or len(df) < n_candles * 4:
        return None

    df.columns = [col.capitalize() for col in df.columns]
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    df4 = df.resample("4h").agg(agg).dropna().tail(n_candles)
    df4.index = df4.index.strftime("%Y-%m-%d %H:%M")
    return df4.to_csv()


def log_analysis(fecha: str, ticker: str, result: dict, l2_alerta: str = "") -> None:
    init_db()
    le = result.get("lectura_estructural", {})
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO analysis
            (fecha, ticker, precio_actual, veredicto, modo_entrada, sub_onda,
             sesgo_macro, fase_impulso, calidad_senal, l2_alerta, modelo, analizado_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fecha, ticker,
            result.get("precio_actual"),
            result.get("veredicto"),
            result.get("modo_entrada"),
            le.get("sub_onda_actual"),
            result.get("sesgo_macro"),
            result.get("fase_impulso_macro"),
            result.get("calidad_señal"),
            l2_alerta,
            result.get("_meta", {}).get("modelo"),
            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))


def log_signal(fecha: str, ticker: str, result: dict) -> None:
    pt = result.get("plan_trade")
    if not pt:
        return
    entry = pt.get("entrada")
    stop = pt.get("stop")
    risk = abs(entry - stop) if entry and stop else None

    def rr(target):
        if risk and risk > 0 and target:
            return round(abs(target - entry) / risk, 2)
        return None

    init_db()
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO signals
            (fecha, ticker, modo, direccion, calidad, entrada, stop,
             o1, o2, o3, rr_o1, rr_o2, rr_o3)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fecha, ticker,
            result.get("modo_entrada"),
            result.get("direccion"),
            result.get("calidad_señal"),
            entry, stop,
            pt.get("O1"), pt.get("O2"), pt.get("O3"),
            rr(pt.get("O1")), rr(pt.get("O2")), rr(pt.get("O3")),
        ))


def invalidar_señales_pendientes(ticker: str, fecha_hoy: str) -> int:
    """Marca como INVALIDADA cualquier señal ABIERTA de un día anterior cuya entrada
    nunca se tocó. alertas_activas.json se sobreescribe completo cada corrida de
    signal_generator, así que si la entrada no se llenó antes de la corrida de hoy,
    esos niveles dejaron de vigilarse — la señal queda huérfana en estado ABIERTO
    para siempre si no se marca explícitamente. Retorna cuántas se invalidaron."""
    init_db()
    n = 0
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        abiertas = conn.execute(
            "SELECT id, fecha FROM signals WHERE ticker=? AND estado='ABIERTO' AND fecha < ?",
            (ticker, fecha_hoy)
        ).fetchall()
        for sig in abiertas:
            entrada_fired = conn.execute(
                "SELECT 1 FROM alerts_fired WHERE ticker=? AND alert_id='entrada' "
                "AND fired_at >= ? LIMIT 1",
                (ticker, sig["fecha"])
            ).fetchone()
            if entrada_fired:
                continue  # sí se llenó — sigue su curso normal vía update_signal_resultado
            conn.execute(
                "UPDATE signals SET estado='INVALIDADA', fecha_cierre=? WHERE id=?",
                (fecha_hoy, sig["id"])
            )
            n += 1
    return n


def update_signal_resultado(ticker: str, alert_id: str, precio: float) -> None:
    """Actualiza estado/resultado de la señal ABIERTA cuando price_watcher toca un nivel."""
    init_db()
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, rr_o1, rr_o2, rr_o3, o1_hit, o2_hit FROM signals "
            "WHERE ticker=? AND estado='ABIERTO' ORDER BY fecha DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        if not row:
            return
        sid    = row["id"]
        rr_o1  = row["rr_o1"] or 0
        rr_o2  = row["rr_o2"] or 0
        rr_o3  = row["rr_o3"] or 0
        o1_hit = row["o1_hit"]
        o2_hit = row["o2_hit"]

        if alert_id == "o1":
            conn.execute("UPDATE signals SET o1_hit=1 WHERE id=?", (sid,))

        elif alert_id == "o2":
            conn.execute("UPDATE signals SET o2_hit=1 WHERE id=?", (sid,))

        elif alert_id == "o3":
            r = round(rr_o1 * 0.50 + rr_o2 * 0.25 + rr_o3 * 0.25, 2)
            conn.execute(
                "UPDATE signals SET o3_hit=1, resultado_r=?, estado='GANADOR', fecha_cierre=? WHERE id=?",
                (r, now, sid),
            )

        elif alert_id == "stop":
            if o1_hit and o2_hit:
                r, estado = round(rr_o1 * 0.50 + rr_o2 * 0.25, 2), "GANADOR"
            elif o1_hit:
                r = round(rr_o1 * 0.50, 2)
                estado = "GANADOR" if r > 0 else "BREAKEVEN"
            else:
                r, estado = -1.0, "PERDEDOR"
            conn.execute(
                "UPDATE signals SET resultado_r=?, estado=?, fecha_cierre=? WHERE id=?",
                (r, estado, now, sid),
            )


def log_l1(fecha: str, ticker: str, result: dict) -> None:
    import json as _json
    init_db()
    techo   = result.get("techo_operativo", {})
    acuerdo = result.get("acuerdo", {})
    div     = result.get("divergencia", {})
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO analysis_weekly
            (fecha, ticker, techo_precio, techo_fecha, escenarios_json,
             acuerdo_sesgo, acuerdo_niveles, acuerdo_operar,
             divergencia_pregunta, divergencia_resuelve, resumen, modelo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            fecha, ticker,
            techo.get("precio"),
            techo.get("fecha"),
            _json.dumps(result.get("escenarios", []), ensure_ascii=False),
            acuerdo.get("sesgo_cercano"),
            _json.dumps(acuerdo.get("niveles_compartidos", []), ensure_ascii=False),
            acuerdo.get("como_operar_hoy"),
            div.get("pregunta_abierta"),
            div.get("se_resuelve_si"),
            result.get("resumen"),
            result.get("_meta", {}).get("modelo"),
        ))


def actualizar_mfe_pendientes(ticker: str) -> None:
    """Calcula mfe_48h (momentum favorable a las 48h del fill) para señales C_breakout
    que ya cumplieron esa ventana — solo captura el dato, no cambia el trade.
    Hallazgo en estudio (ver backlog): tercio de menor MFE_48h tuvo win rate 9% en
    backtest histórico vs 55% en terciles medio/alto."""
    init_db()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        pendientes = conn.execute(
            "SELECT id, fecha, direccion, entrada, stop FROM signals "
            "WHERE ticker=? AND modo='C_breakout' AND mfe_48h IS NULL",
            (ticker,)
        ).fetchall()

        for sig in pendientes:
            entrada_fired = conn.execute(
                "SELECT fired_at FROM alerts_fired WHERE ticker=? AND alert_id='entrada' "
                "AND fired_at >= ? ORDER BY fired_at LIMIT 1",
                (ticker, sig["fecha"])
            ).fetchone()
            if not entrada_fired:
                continue
            fired_dt = datetime.strptime(entrada_fired["fired_at"], "%Y-%m-%dT%H:%M:%SZ")
            if datetime.utcnow() - fired_dt < timedelta(hours=48):
                continue

            df = pd.read_sql_query(
                "SELECT high,low FROM ohlcv WHERE ticker=? AND ts >= ? ORDER BY ts LIMIT 49",
                conn, params=[ticker, fired_dt.strftime("%Y-%m-%d %H:%M")]
            )
            if df.empty:
                continue
            long = (sig["direccion"] or "").upper() == "LONG"
            riesgo = abs(sig["entrada"] - sig["stop"]) if sig["entrada"] and sig["stop"] else 0
            if riesgo == 0:
                continue
            mfe = ((df["high"].max() - sig["entrada"]) / riesgo if long
                   else (sig["entrada"] - df["low"].min()) / riesgo)
            conn.execute("UPDATE signals SET mfe_48h=? WHERE id=?", (round(float(mfe), 2), sig["id"]))


def log_alert_fired(ticker: str, alert: dict, precio: float) -> None:
    init_db()
    with _conn() as c:
        c.execute("""
            INSERT INTO alerts_fired (fired_at, ticker, alert_id, tipo, nivel, precio_actual, mensaje)
            VALUES (?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            ticker,
            alert.get("id"),
            alert.get("tipo"),
            alert.get("nivel"),
            precio,
            alert.get("mensaje"),
        ))
