# ======================================================
# 🐝 BumbleBee v20.1 — AUTO 15 MIN + DASH INTACTO
# ======================================================

import time
import threading
import sqlite3
import requests
from datetime import datetime
from typing import Optional, Dict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DB_NAME = "polymarket.db"
POLL_SECONDS = 5
MARKET_SCAN_SECONDS = 10

# =========================
# DB
# =========================
def db():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        condition_id TEXT,
        market_question TEXT,
        start_ts TEXT,
        end_ts TEXT,
        shares_target INTEGER,
        shares_yes INTEGER DEFAULT 0,
        shares_no INTEGER DEFAULT 0,
        max_price REAL,
        profit REAL
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        ts TEXT,
        side TEXT,
        price REAL,
        shares INTEGER
    );
    """)
    conn.commit()
    conn.close()

init_db()

# =========================
# STATE
# =========================
class BotState:
    def __init__(self):
        self.running = False
        self.shares = 20
        self.max_price = 0.6
        self.max_sessions = None
        self.current_sessions = 0
        self.status_msg = "IDLE"
        self.start_time = None

        self.current_market: Optional[Dict] = None
        self.current_condition: Optional[str] = None
        self.last_scan = 0

STATE = BotState()
LOCK = threading.Lock()

# =========================
# MARKET DETECTION (15m)
# =========================
def find_live_15m_market() -> Optional[Dict]:
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": True, "closed": False, "limit": 200},
            timeout=10
        )
        for m in r.json():
            q = (m.get("question") or "").lower()
            if "up or down" in q and "15 minute" in q and m.get("isLive"):
                return m
    except:
        pass
    return None

def get_prices(market: Dict):
    try:
        outs = market["outcomes"]
        yes = float(outs[0]["price"])
        no  = float(outs[1]["price"])
        return yes, no
    except:
        return None

# =========================
# BOT LOOP
# =========================
def bot_loop():
    session_id = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        # scan market
        if time.time() - STATE.last_scan > MARKET_SCAN_SECONDS:
            market = find_live_15m_market()
            STATE.last_scan = time.time()
        else:
            market = STATE.current_market

        if not market:
            STATE.status_msg = "AGUARDANDO MERCADO 15M"
            time.sleep(POLL_SECONDS)
            continue

        condition_id = market["conditionId"]

        # new market => new session
        if STATE.current_condition != condition_id:
            # close previous
            if session_id is not None:
                conn = db()
                cur = conn.cursor()
                cur.execute("""
                    SELECT
                      SUM(CASE WHEN side='YES' THEN price*shares ELSE 0 END),
                      SUM(CASE WHEN side='NO'  THEN price*shares ELSE 0 END),
                      SUM(CASE WHEN side='YES' THEN shares ELSE 0 END),
                      SUM(CASE WHEN side='NO'  THEN shares ELSE 0 END)
                    FROM trades WHERE session_id=?
                """, (session_id,))
                cy, cn, sy, sn = cur.fetchone()
                cy, cn = cy or 0, cn or 0
                sy, sn = sy or 0, sn or 0
                profit = min(sy, sn) - (cy + cn)

                cur.execute("""
                    UPDATE sessions SET end_ts=?, profit=? WHERE id=?
                """, (datetime.utcnow().isoformat(), profit, session_id))
                conn.commit()
                conn.close()

                with LOCK:
                    STATE.current_sessions += 1
                    if STATE.max_sessions and STATE.current_sessions >= STATE.max_sessions:
                        STATE.running = False
                        STATE.status_msg = "LIMITE DE SESSÕES ATINGIDO"
                        session_id = None
                        continue

            # open new session
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions
                (condition_id, market_question, start_ts, shares_target, max_price)
                VALUES (?,?,?,?,?)
            """, (
                condition_id,
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.shares,
                STATE.max_price
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

            STATE.current_condition = condition_id
            STATE.current_market = market
            STATE.status_msg = "NOVA SESSÃO 15M"

        # trading
        prices = get_prices(market)
        if prices:
            yes, no = prices
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT shares_yes, shares_no FROM sessions WHERE id=?", (session_id,))
            sy, sn = cur.fetchone()

            if yes <= STATE.max_price and sy < STATE.shares:
                buy = STATE.shares - sy
                cur.execute(
                    "INSERT INTO trades VALUES (NULL,?,?,?,?,?)",
                    (session_id, datetime.utcnow().isoformat(), "YES", yes, buy)
                )
                cur.execute(
                    "UPDATE sessions SET shares_yes=shares_yes+? WHERE id=?",
                    (buy, session_id)
                )
                STATE.status_msg = f"BUY YES {buy} @ {yes}"

            if no <= STATE.max_price and sn < STATE.shares:
                buy = STATE.shares - sn
                cur.execute(
                    "INSERT INTO trades VALUES (NULL,?,?,?,?,?)",
                    (session_id, datetime.utcnow().isoformat(), "NO", no, buy)
                )
                cur.execute(
                    "UPDATE sessions SET shares_no=shares_no+? WHERE id=?",
                    (buy, session_id)
                )
                STATE.status_msg = f"BUY NO {buy} @ {no}"

            conn.commit()
            conn.close()

        time.sleep(POLL_SECONDS)

# =========================
# API + DASHBOARD (INTACTO)
# =========================
app = FastAPI(title="BumbleBee v20.1")

class ControlReq(BaseModel):
    shares: Optional[int]
    max_price: Optional[float]
    max_sessions: Optional[int]

@app.post("/start")
def start():
    with LOCK:
        STATE.running = True
        STATE.start_time = time.time()
        STATE.current_sessions = 0
        STATE.status_msg = "BOT INICIADO"
    return {"ok": True}

@app.post("/stop")
def stop():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "BOT PARADO"
    return {"ok": True}

@app.post("/controls")
def controls(req: ControlReq):
    with LOCK:
        if req.shares is not None: STATE.shares = req.shares
        if req.max_price is not None: STATE.max_price = req.max_price
        if req.max_sessions is not None:
            STATE.max_sessions = req.max_sessions if req.max_sessions > 0 else None
        STATE.status_msg = "CONFIGURAÇÕES SALVAS"
    return {"ok": True}

@app.get("/status")
def status():
    elapsed = int(time.time() - STATE.start_time) if STATE.start_time else 0
    return {
        "running": STATE.running,
        "shares": STATE.shares,
        "max_price": STATE.max_price,
        "max_sessions": STATE.max_sessions,
        "current_sessions": STATE.current_sessions,
        "status_msg": STATE.status_msg,
        "elapsed": elapsed
    }

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 10")
    sessions = cur.fetchall()
    cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20")
    trades = cur.fetchall()
    cur.execute("SELECT SUM(profit) FROM sessions WHERE profit IS NOT NULL")
    total_profit = cur.fetchone()[0] or 0
    conn.close()

    sess_rows = "".join(
        f"<tr><td>{s[0]}</td><td>{s[6]}</td><td>{s[7]}</td><td>{round(s[9],4) if s[9] else 0}</td></tr>"
        for s in sessions
    )
    trade_rows = "".join(
        f"<tr><td>{t[3]}</td><td>{t[4]}</td><td>{t[5]}</td><td>{t[2]}</td></tr>"
        for t in trades
    )

    return f"""
    <h1>🐝 BumbleBee v20.1</h1>
    <h2 style='color:red'>{STATE.status_msg}</h2>
    <p>⏱️ Sessões: {STATE.current_sessions} | 💰 Total Profit: {round(total_profit,4)}</p>

    <h3>Sessões</h3>
    <table border=1>
      <tr><th>ID</th><th>YES</th><th>NO</th><th>Profit</th></tr>
      {sess_rows}
    </table>

    <h3>Trades</h3>
    <table border=1>
      <tr><th>Side</th><th>Price</th><th>Shares</th><th>Time</th></tr>
      {trade_rows}
    </table>
    """

# =========================
# START THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
