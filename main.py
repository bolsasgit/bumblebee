import time
import json
import threading
import sqlite3
from typing import Optional, Dict, Any
import requests
from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"

T3_THRESHOLD = 0.70
T2_THRESHOLD = 0.85

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
        wallet_id TEXT,
        had_trade INTEGER DEFAULT 0,
        completed_pair INTEGER DEFAULT 0,
        partial_exposure INTEGER DEFAULT 0,
        liquidated INTEGER DEFAULT 0,
        max_exposure_usd REAL DEFAULT 0,
        final_pnl REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        opened_ts TEXT,
        closed_ts TEXT,
        status TEXT,
        side_opened_first TEXT,
        time_to_second_leg INTEGER,
        capital_yes REAL DEFAULT 0,
        capital_no REAL DEFAULT 0,
        total_cost REAL DEFAULT 0,
        expected_payoff REAL DEFAULT 0,
        pnl REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        session_id INTEGER,
        side TEXT,
        action TEXT,
        price REAL,
        quantity REAL,
        capital REAL,
        ts TEXT
    );

    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        trade_id INTEGER,
        ts TEXT,
        seconds_to_expiry INTEGER,
        price_yes REAL,
        price_no REAL
    );

    CREATE TABLE IF NOT EXISTS liquidations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER,
        session_id INTEGER,
        side_closed TEXT,
        entry_price REAL,
        exit_price REAL,
        time_to_expiry INTEGER,
        pnl REAL,
        ts TEXT
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
    max_sessions: Optional[int] = None
    current_sessions = 0
    wallet_id: Optional[str] = None
    status_msg = "stopped"

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
    last_yes, last_no = None, None
    for t in resp.json():
        o = (t.get("outcome") or "").upper()
        p = float(t.get("price"))
        if o == "YES":
            last_yes = p
        elif o == "NO":
            last_no = p
        if last_yes and last_no:
            return {"YES": last_yes, "NO": last_no}
    return None

# =========================
# PAPER EXECUTION (inalterado)
# =========================
def execute_paper_trade(session_id, prices):
    entry = prices["YES"] + prices["NO"]
    pnl = 1.0 - entry

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO trades (
            session_id, opened_ts, status,
            capital_yes, capital_no,
            total_cost, expected_payoff, pnl
        )
        VALUES (?, ?, 'completed', ?, ?, ?, ?, ?)
    """, (
        session_id,
        datetime.utcnow().isoformat(),
        prices["YES"], prices["NO"],
        entry, 1.0, pnl
    ))

    conn.commit()
    conn.close()

# =========================
# CORE LOOP
# =========================
def bot_loop():
    current_condition = None
    session_id = None
    end_ts = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        market = get_active_btc_15m_market()
        if not market:
            time.sleep(5)
            continue

        condition_id = market["conditionId"]

        if condition_id != current_condition:
            if session_id:
                conn = db()
                cur = conn.cursor()
                cur.execute("UPDATE sessions SET end_ts=? WHERE id=?", (
                    datetime.utcnow().isoformat(), session_id
                ))
                conn.commit()
                conn.close()

                STATE.current_sessions += 1
                if STATE.max_sessions and STATE.current_sessions >= STATE.max_sessions:
                    STATE.running = False
                    STATE.status_msg = "completed"
                    break

            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions (condition_id, market_question, start_ts, mode, wallet_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                condition_id,
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.wallet_id
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

            current_condition = condition_id
            end_ts = market["endDate"]

        prices = get_latest_yes_no_prices()
        if prices:
            now = datetime.utcnow()
            seconds_left = int((datetime.fromisoformat(end_ts.replace("Z","")) - now).total_seconds())

            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO price_snapshots (
                    session_id, ts, seconds_to_expiry, price_yes, price_no
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                now.isoformat(),
                seconds_left,
                prices["YES"],
                prices["NO"]
            ))
            conn.commit()
            conn.close()

            s = prices["YES"] + prices["NO"]
            if s < T2_THRESHOLD:
                execute_paper_trade(session_id, prices)

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee Bot")

class StartRequest(BaseModel):
    mode: str
    sessions: Optional[int]
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
    return STATE.__dict__

# =========================
# START
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
