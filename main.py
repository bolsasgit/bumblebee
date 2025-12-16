import time
import json
import threading
import sqlite3
from typing import Optional, Dict, Any
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"

T3_DEFAULT = 0.70
T2_DEFAULT = 0.85

PRICE_POLL_SECONDS = 5

# =========================
# DB
# =========================
def db():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        condition_id TEXT,
        market_question TEXT,
        start_ts TEXT,
        end_ts TEXT,
        mode TEXT,
        wallet_id TEXT,
        had_opportunity INTEGER,
        tier TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        mode TEXT,
        side TEXT,
        entry_price REAL,
        exit_price REAL,
        size REAL,
        pnl REAL,
        ts TEXT
    )
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
    max_sessions: Optional[int] = None
    current_sessions = 0
    wallet_id: Optional[str] = None
    status_msg = "stopped"
    t3 = T3_DEFAULT
    t2 = T2_DEFAULT

STATE = BotState()
LOCK = threading.Lock()

# =========================
# POLYMARKET
# =========================
def get_active_market():
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

def get_prices():
    r = requests.get(f"{DATA_API}/trades", params={"limit": 50}, timeout=10)
    y = n = None
    for t in r.json():
        if t["outcome"] == "YES":
            y = float(t["price"])
        if t["outcome"] == "NO":
            n = float(t["price"])
        if y and n:
            return y, n
    return None

# =========================
# CORE LOOP
# =========================
def bot_loop():
    current_condition = None
    session_id = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        market = get_active_market()
        if not market:
            time.sleep(5)
            continue

        cid = market["conditionId"]

        if cid != current_condition:
            if session_id:
                db().execute(
                    "UPDATE sessions SET end_ts=datetime('now') WHERE id=?",
                    (session_id,)
                )
                STATE.current_sessions += 1
                if STATE.max_sessions and STATE.current_sessions >= STATE.max_sessions:
                    STATE.running = False
                    STATE.status_msg = "completed"
                    break

            cur = db().cursor()
            cur.execute("""
                INSERT INTO sessions (condition_id, market_question, start_ts, mode, wallet_id, had_opportunity)
                VALUES (?, ?, datetime('now'), ?, ?, 0)
            """, (cid, market["question"], STATE.mode, STATE.wallet_id))
            session_id = cur.lastrowid
            cur.connection.commit()
            current_condition = cid

        prices = get_prices()
        if prices:
            y, n = prices
            s = y + n
            tier = None
            if s < STATE.t3:
                tier = "T3"
            elif s < STATE.t2:
                tier = "T2"

            if tier:
                pnl = 1.0 - s
                db().execute(
                    "INSERT INTO trades VALUES (NULL, ?, ?, 'BOTH', ?, 1.0, 1.0, ?, datetime('now'))",
                    (session_id, STATE.mode, s, pnl)
                )
                db().execute(
                    "UPDATE sessions SET had_opportunity=1, tier=? WHERE id=?",
                    (tier, session_id)
                )
                db().commit()

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class StartReq(BaseModel):
    mode: str
    sessions: Optional[int]
    t3: Optional[float]
    t2: Optional[float]

@app.post("/start")
def start(req: StartReq):
    with LOCK:
        STATE.mode = req.mode
        STATE.max_sessions = req.sessions
        STATE.t3 = req.t3 or T3_DEFAULT
        STATE.t2 = req.t2 or T2_DEFAULT
        STATE.current_sessions = 0
        STATE.running = True
        STATE.status_msg = "running"
    return {"ok": True}

@app.post("/stop")
def stop():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "stopped"
    return {"ok": True}

@app.get("/status")
def status():
    return {
        "running": STATE.running,
        "mode": STATE.mode,
        "session": f"{STATE.current_sessions}/{STATE.max_sessions or '∞'}",
        "t3": STATE.t3,
        "t2": STATE.t2,
        "status": STATE.status_msg
    }

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!DOCTYPE html>
<html>
<body style="background:#0f1220;color:white;font-family:Arial;text-align:center">
<h2>🐝 BumbleBee</h2>

T3 <input id=t3 value=0.70 step=0.01>
T2 <input id=t2 value=0.85 step=0.01><br><br>

<button onclick="m='paper'">PAPER</button>
<button onclick="m='real'">REAL</button><br><br>

<button onclick="s=10">10</button>
<button onclick="s=50">50</button>
<button onclick="s=100">100</button>
<button onclick="s=null">24/7</button><br><br>

<button onclick="start()">START</button>
<button onclick="fetch('/stop',{method:'POST'})">STOP</button>

<pre id=v></pre>

<script>
let m="paper",s=null;
function start(){
fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({
mode:m,sessions:s,
t3:parseFloat(t3.value),
t2:parseFloat(t2.value)
})}).then(r=>r.json()).then(d=>v.innerText=JSON.stringify(d,null,2))
}
setInterval(()=>fetch('/status').then(r=>r.json()).then(d=>v.innerText=JSON.stringify(d,null,2)),5000)
</script>
</body>
</html>
"""

# =========================
# THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
