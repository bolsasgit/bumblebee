import time
import threading
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"
PRICE_POLL_SECONDS = 5
MODE = "paper"

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
        wallet_id TEXT
    );

    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        ts TEXT,
        seconds_to_expiry INTEGER,
        price_yes REAL,
        price_no REAL
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
        self.mode = MODE
        self.max_sessions = 1
        self.current_sessions = 0
        self.wallet_id = "pt"
        self.status_msg = "idle"

STATE = BotState()
LOCK = threading.Lock()

# =========================
# HELPERS
# =========================
def get_active_btc_15m_market():
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={"active": True, "closed": False, "limit": 50},
        timeout=10
    )
    for m in resp.json():
        q = (m.get("question") or "").lower()
        if "btc" in q and "15" in q:
            return m
    return None

def get_latest_yes_no_prices():
    resp = requests.get(f"{DATA_API}/trades", params={"limit": 50}, timeout=10)
    yes, no = None, None
    for t in resp.json():
        side = (t.get("outcome") or "").upper()
        price = float(t.get("price"))
        if side == "YES":
            yes = price
        if side == "NO":
            no = price
        if yes is not None and no is not None:
            return yes, no
    return None

# =========================
# BOT LOOP
# =========================
def bot_loop():
    session_id = None
    session_end_dt = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        if not session_id:
            market = get_active_btc_15m_market()
            if not market:
                time.sleep(5)
                continue

            session_end_dt = datetime.fromisoformat(
                market["endDate"].replace("Z", "+00:00")
            )

            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions (condition_id, market_question, start_ts, mode, wallet_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                market["conditionId"],
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.wallet_id
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

        seconds_left = int((session_end_dt - datetime.now(timezone.utc)).total_seconds())

        if seconds_left <= 0:
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET end_ts=? WHERE id=?",
                        (datetime.utcnow().isoformat(), session_id))
            conn.commit()
            conn.close()

            session_id = None
            with LOCK:
                STATE.current_sessions += 1
                if STATE.current_sessions >= STATE.max_sessions:
                    STATE.running = False
                    STATE.status_msg = "completed"
            continue

        prices = get_latest_yes_no_prices()
        if prices:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO price_snapshots
                (session_id, ts, seconds_to_expiry, price_yes, price_no)
                VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                datetime.utcnow().isoformat(),
                seconds_left,
                prices[0],
                prices[1]
            ))
            conn.commit()
            conn.close()

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee")

@app.post("/start")
def start_bot():
    with LOCK:
        STATE.running = True
        STATE.current_sessions = 0
        STATE.status_msg = "started"
    return {"ok": True}

@app.post("/stop")
def stop_bot():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "stopped"
    return {"ok": True}

@app.get("/status")
def status():
    return STATE.__dict__

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html = f"""
    <html>
    <head>
    <style>
        body {{ background:#0f0f0f; color:white; font-family:Arial; padding:30px }}
        button {{
            padding:15px; font-size:18px; margin:5px;
            border-radius:8px; border:none; cursor:pointer;
        }}
        .start {{ background:#1e90ff }}
        .stop {{ background:#ff4444 }}
        .active {{ box-shadow:0 0 10px #00ff00 }}
        .panel {{ border:2px solid gold; padding:15px; margin-top:20px }}
    </style>
    </head>
    <body>

    <h1>BUMBLEBEE — CONTROL DASH</h1>

    <button id="start" class="start" onclick="start()">START BOT</button>
    <button id="stop" class="stop" onclick="stop()">STOP BOT</button>

    <div class="panel">
        <h2>STATUS</h2>
        <p id="visor">Idle</p>
    </div>

    <script>
    async function start() {{
        await fetch('/start', {{method:'POST'}});
        document.getElementById('start').classList.add('active');
        document.getElementById('stop').classList.remove('active');
        document.getElementById('visor').innerText = "BOT STARTED";
    }}

    async function stop() {{
        await fetch('/stop', {{method:'POST'}});
        document.getElementById('stop').classList.add('active');
        document.getElementById('start').classList.remove('active');
        document.getElementById('visor').innerText = "BOT STOPPED";
    }}
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
