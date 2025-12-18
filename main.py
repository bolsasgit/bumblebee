import time
import threading
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ======================================================
# 🐝 BumbleBee v19
# ======================================================

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"
PRICE_POLL_SECONDS = 5

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
        mode TEXT,
        shares_target INTEGER,
        shares_yes INTEGER DEFAULT 0,
        shares_no INTEGER DEFAULT 0,
        max_price REAL,
        profit REAL DEFAULT 0
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
    running = False
    mode = "paper"
    shares = 20              # por lado
    max_price = 0.35         # preço máximo por lado
    max_sessions = None      # None = 24/7
    current_sessions = 0
    status_msg = "IDLE"

STATE = BotState()
LOCK = threading.Lock()

# =========================
# HELPERS
# =========================
def get_active_btc_15m_market():
    r = requests.get(
        f"{GAMMA_API}/markets",
        params={"active": True, "closed": False, "limit": 50},
        timeout=10
    )
    for m in r.json():
        q = (m.get("question") or "").lower()
        if "btc" in q and "15" in q:
            return m
    return None

def get_latest_prices():
    r = requests.get(f"{DATA_API}/trades", params={"limit": 50}, timeout=10)
    yes = no = None
    for t in r.json():
        side = (t.get("outcome") or "").upper()
        price = float(t.get("price"))
        if side == "YES":
            yes = price
        elif side == "NO":
            no = price
        if yes is not None and no is not None:
            return yes, no
    return None

# =========================
# BOT LOOP
# =========================
def bot_loop():
    session_id = None
    condition_id = None
    session_end = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        market = get_active_btc_15m_market()
        if not market:
            time.sleep(5)
            continue

        if market["conditionId"] != condition_id:
            condition_id = market["conditionId"]
            session_end = datetime.fromisoformat(
                market["endDate"].replace("Z", "+00:00")
            )

            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions
                (condition_id, market_question, start_ts, mode, shares_target, max_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                condition_id,
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.shares,
                STATE.max_price
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

        # encerra sessão
        if datetime.now(timezone.utc) >= session_end:
            conn = db()
            cur = conn.cursor()

            # payoff estrutural = shares_yes + shares_no (cada lado vale 1 se correto)
            cur.execute("""
                SELECT
                  SUM(CASE WHEN side='YES' THEN price*shares ELSE 0 END),
                  SUM(CASE WHEN side='NO' THEN price*shares ELSE 0 END),
                  SUM(CASE WHEN side='YES' THEN shares ELSE 0 END),
                  SUM(CASE WHEN side='NO' THEN shares ELSE 0 END)
                FROM trades WHERE session_id=?
            """, (session_id,))
            cost_yes, cost_no, sy, sn = cur.fetchone()

            cost_yes = cost_yes or 0
            cost_no = cost_no or 0
            sy = sy or 0
            sn = sn or 0

            payoff = min(sy, sn) * 1.0
            profit = payoff - (cost_yes + cost_no)

            cur.execute("""
                UPDATE sessions
                SET end_ts=?, profit=?
                WHERE id=?
            """, (datetime.utcnow().isoformat(), profit, session_id))

            conn.commit()
            conn.close()

            with LOCK:
                STATE.current_sessions += 1
                if STATE.max_sessions and STATE.current_sessions >= STATE.max_sessions:
                    STATE.running = False
                    STATE.status_msg = "LIMITE DE SESSÕES ATINGIDO"

            session_id = None
            condition_id = None
            continue

        prices = get_latest_prices()
        if prices:
            yes, no = prices
            conn = db()
            cur = conn.cursor()

            cur.execute("SELECT shares_yes, shares_no FROM sessions WHERE id=?", (session_id,))
            sy, sn = cur.fetchone()

            # YES
            if yes <= STATE.max_price and sy < STATE.shares:
                buy = STATE.shares - sy
                cur.execute("""
                    INSERT INTO trades (session_id, ts, side, price, shares)
                    VALUES (?, ?, 'YES', ?, ?)
                """, (session_id, datetime.utcnow().isoformat(), yes, buy))
                cur.execute("UPDATE sessions SET shares_yes=shares_yes+? WHERE id=?",
                            (buy, session_id))
                STATE.status_msg = f"BUY YES {buy} @ {yes}"

            # NO
            if no <= STATE.max_price and sn < STATE.shares:
                buy = STATE.shares - sn
                cur.execute("""
                    INSERT INTO trades (session_id, ts, side, price, shares)
                    VALUES (?, ?, 'NO', ?, ?)
                """, (session_id, datetime.utcnow().isoformat(), no, buy))
                cur.execute("UPDATE sessions SET shares_no=shares_no+? WHERE id=?",
                            (buy, session_id))
                STATE.status_msg = f"BUY NO {buy} @ {no}"

            conn.commit()
            conn.close()

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee v19")

class ControlReq(BaseModel):
    shares: Optional[int] = None
    max_price: Optional[float] = None
    mode: Optional[str] = None
    max_sessions: Optional[int] = None

@app.post("/start")
def start():
    with LOCK:
        STATE.running = True
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
        if req.mode is not None: STATE.mode = req.mode
        if req.max_sessions is not None:
            STATE.max_sessions = req.max_sessions if req.max_sessions > 0 else None
        STATE.status_msg = "CONFIGURAÇÕES SALVAS"
    return {"ok": True}

@app.get("/status")
def status():
    return STATE.__dict__

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 5")
    sessions = cur.fetchall()
    cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20")
    trades = cur.fetchall()
    conn.close()

    html = f"""
    <html>
    <head>
    <style>
      body {{ background:#0f0f0f; color:white; font-family:Arial; padding:20px }}
      .box {{ border:2px solid gold; padding:15px; margin-top:15px }}
      table {{ width:100%; border-collapse:collapse; color:white }}
      td,th {{ border:1px solid #555; padding:5px; font-size:13px }}
      #visor {{ text-align:center; color:red; font-size:22px }}
      .glow {{ box-shadow:0 0 12px }}
    </style>
    </head>
    <body>

    <div id="visor">🐝 BumbleBee v19 — {STATE.status_msg}</div>

    <div class="box">
      Shares <input id="shares" value="{STATE.shares}">
      Max Price <input id="mp" value="{STATE.max_price}">
      Mode <select id="mode"><option>paper</option><option>real</option></select>
      Max Sessions <input id="ms">
      <button onclick="save(this)">SALVAR</button>
      <button onclick="startBot(this)">START</button>
      <button onclick="stopBot(this)">STOP</button>
    </div>

    <div class="box">
      <h3>Sessions</h3>
      <table>
        <tr><th>ID</th><th>YES</th><th>NO</th><th>Profit</th></tr>
        {''.join(f"<tr><td>{s[0]}</td><td>{s[6]}</td><td>{s[7]}</td><td>{round(s[9],4)}</td></tr>" for s in sessions)}
      </table>
    </div>

    <div class="box">
      <h3>Trades</h3>
      <table>
        <tr><th>Side</th><th>Price</th><th>Shares</th><th>Time</th></tr>
        {''.join(f"<tr><td>{t[3]}</td><td>{t[4]}</td><td>{t[5]}</td><td>{t[2]}</td></tr>" for t in trades)}
      </table>
    </div>

    <div class="box">
      <a href="https://polymarket.com" target="_blank">Polymarket</a> |
      <a href="https://kashi.io" target="_blank">Kashi</a>
    </div>

    <script>
      function glow(b){{b.classList.add('glow');setTimeout(()=>b.classList.remove('glow'),600);}}
      async function save(b){{glow(b);await fetch('/controls',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{shares:parseInt(shares.value),max_price:parseFloat(mp.value),
        mode:mode.value,max_sessions:ms.value?parseInt(ms.value):0}})}});}}
      async function startBot(b){{glow(b);await fetch('/start',{{method:'POST'}});}}
      async function stopBot(b){{glow(b);await fetch('/stop',{{method:'POST'}});}}
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
