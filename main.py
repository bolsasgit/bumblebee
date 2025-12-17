import time
import threading
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"

PRICE_POLL_SECONDS = 5
T2_THRESHOLD = 0.85

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
        wallet_id TEXT,
        final_pnl REAL DEFAULT 0
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
        self.mode = "paper"
        self.max_sessions: Optional[int] = None
        self.current_sessions = 0
        self.wallet_id: Optional[str] = None
        self.status_msg = "stopped"

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
    resp.raise_for_status()

    for m in resp.json():
        q = (m.get("question") or "").lower()
        if "btc" in q and "15" in q:
            return m
    return None

def get_latest_yes_no_prices():
    resp = requests.get(f"{DATA_API}/trades", params={"limit": 50}, timeout=10)
    resp.raise_for_status()

    yes, no = None, None
    for t in resp.json():
        side = (t.get("outcome") or "").upper()
        price = float(t.get("price"))
        if side == "YES":
            yes = price
        elif side == "NO":
            no = price
        if yes and no:
            return {"YES": yes, "NO": no}
    return None

# =========================
# BOT LOOP (CORRIGIDO)
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

            end_raw = market.get("endDate")
            session_end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))

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

        now = datetime.now(timezone.utc)
        seconds_left = int((session_end_dt - now).total_seconds())

        if seconds_left <= 0:
            conn = db()
            cur = conn.cursor()
            cur.execute(
                "UPDATE sessions SET end_ts=? WHERE id=?",
                (datetime.utcnow().isoformat(), session_id)
            )
            conn.commit()
            conn.close()

            session_id = None
            session_end_dt = None

            with LOCK:
                STATE.current_sessions += 1
                if STATE.max_sessions and STATE.current_sessions >= STATE.max_sessions:
                    STATE.running = False
                    STATE.status_msg = "completed"
                    break

            continue

        prices = get_latest_yes_no_prices()
        if prices:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO price_snapshots (
                    session_id, ts, seconds_to_expiry, price_yes, price_no
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                datetime.utcnow().isoformat(),
                seconds_left,
                prices["YES"],
                prices["NO"]
            ))
            conn.commit()
            conn.close()

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee Bot")

class StartRequest(BaseModel):
    mode: str
    sessions: int
    wallet_id: Optional[str] = None

@app.post("/start")
def start_bot(req: StartRequest):
    with LOCK:
        STATE.mode = req.mode
        STATE.max_sessions = req.sessions
        STATE.wallet_id = req.wallet_id
        STATE.current_sessions = 0
        STATE.running = True
        STATE.status_msg = "running"
    return {"ok": True}

@app.post("/stop")
def stop_bot():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "stopped"
    return {"ok": True}

@app.get("/status")
def status():
    return {
        "running": STATE.running,
        "mode": STATE.mode,
        "max_sessions": STATE.max_sessions,
        "current_sessions": STATE.current_sessions,
        "wallet_id": STATE.wallet_id,
        "status_msg": STATE.status_msg
    }

# =========================
# START THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
