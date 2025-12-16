import time
import json
import threading
import sqlite3
from typing import Optional, Dict, Any, List
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DB_NAME = "polymarket.db"

# Estratégia
T3_THRESHOLD = 0.70
T2_THRESHOLD = 0.85

# Polling
PRICE_POLL_SECONDS = 5
MARKET_POLL_SECONDS = 10

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
        mode TEXT,                 -- paper | real
        wallet_id TEXT,
        had_opportunity INTEGER,
        tier TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        mode TEXT,                 -- paper | real
        side TEXT,                 -- YES/NO
        entry_price REAL,
        exit_price REAL,
        size REAL,
        pnl REAL,
        tx_id TEXT,
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
    running: bool = False
    mode: str = "paper"            # paper | real
    max_sessions: Optional[int] = None  # None = 24/7
    current_sessions: int = 0
    wallet_id: Optional[str] = None
    status_msg: str = "stopped"

STATE = BotState()

LOCK = threading.Lock()

# =========================
# HELPERS – POLYMARKET
# =========================
def get_active_btc_15m_market() -> Optional[Dict[str, Any]]:
    """Descobre o mercado BTC 15min ativo (Gamma API)."""
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={
            "active": True,
            "closed": False,
            "limit": 50,
            "order": "endDate",
            "ascending": True
        },
        timeout=10
    )
    resp.raise_for_status()
    markets = resp.json()
    for m in markets:
        q = (m.get("question") or "").lower()
        if "btc" in q and "15" in q:
            return m
    return None

def get_yes_no_tokens(market: Dict[str, Any]) -> Optional[Dict[str, str]]:
    clob_ids = market.get("clobTokenIds")
    if not clob_ids:
        return None
    ids = json.loads(clob_ids)
    if len(ids) < 2:
        return None
    return {"YES": ids[0], "NO": ids[1]}

def get_latest_yes_no_prices() -> Optional[Dict[str, float]]:
    """Usa Data API / trades para obter os últimos preços YES/NO."""
    resp = requests.get(
        f"{DATA_API}/trades",
        params={"limit": 50},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    last_yes, last_no = None, None
    for t in data:
        outcome = (t.get("outcome") or "").upper()
        price = float(t.get("price"))
        if outcome == "YES":
            last_yes = price
        elif outcome == "NO":
            last_no = price
        if last_yes is not None and last_no is not None:
            return {"YES": last_yes, "NO": last_no}
    return None

# =========================
# EXECUTION
# =========================
def execute_paper_trade(session_id: int, prices: Dict[str, float], tier: str) -> float:
    """
    Paper trade simples:
    - Compra YES e NO com size = 1
    - Fecha virtualmente no 'fair' (1.0)
    """
    yes = prices["YES"]
    no = prices["NO"]
    size = 1.0

    entry_cost = yes + no
    exit_value = 1.0
    pnl = exit_value - entry_cost

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO trades (session_id, mode, side, entry_price, exit_price, size, pnl, tx_id, ts)
        VALUES (?, 'paper', 'BOTH', ?, ?, ?, ?, NULL, datetime('now'))
    """, (session_id, entry_cost, exit_value, size, pnl))
    conn.commit()
    conn.close()
    return pnl

def execute_real_trade_stub(session_id: int, prices: Dict[str, float], tier: str) -> float:
    """
    Execução REAL:
    - Aqui você liga exatamente os métodos do py-clob-client
    - Mantido como stub seguro (pronto para colar suas chaves)
    """
    # >>> INTEGRE AQUI o auth_client / create_order / post_order <<<
    # Deve retornar o pnl REAL calculado com preços reais e fees.
    return 0.0

# =========================
# CORE LOOP
# =========================
def bot_loop():
    global STATE
    current_condition_id = None
    session_id = None
    market_question = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        # 1) Descobrir mercado ativo
        try:
            market = get_active_btc_15m_market()
        except Exception as e:
            time.sleep(5)
            continue

        if not market:
            time.sleep(5)
            continue

        condition_id = market.get("conditionId")
        market_question = market.get("question")

        # 2) Nova sessão quando o conditionId muda
        if condition_id != current_condition_id:
            # Fecha sessão anterior
            if session_id is not None:
                conn = db()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE sessions
                    SET end_ts=datetime('now')
                    WHERE id=?
                """, (session_id,))
                conn.commit()
                conn.close()

                with LOCK:
                    STATE.current_sessions += 1
                    if STATE.max_sessions is not None and STATE.current_sessions >= STATE.max_sessions:
                        STATE.running = False
                        STATE.status_msg = "completed"
                        break

            # Abre nova sessão
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions (condition_id, market_question, start_ts, mode, wallet_id, had_opportunity, tier)
                VALUES (?, ?, datetime('now'), ?, ?, 0, NULL)
            """, (condition_id, market_question, STATE.mode, STATE.wallet_id))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

            current_condition_id = condition_id

        # 3) Monitorar preços dentro da sessão
        try:
            prices = get_latest_yes_no_prices()
        except Exception:
            prices = None

        if prices:
            s = prices["YES"] + prices["NO"]
            tier = None
            if s < T3_THRESHOLD:
                tier = "T3"
            elif s < T2_THRESHOLD:
                tier = "T2"

            if tier:
                conn = db()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE sessions
                    SET had_opportunity=1, tier=?
                    WHERE id=?
                """, (tier, session_id))
                conn.commit()
                conn.close()

                # Execução
                if STATE.mode == "paper":
                    execute_paper_trade(session_id, prices, tier)
                else:
                    execute_real_trade_stub(session_id, prices, tier)

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee Bot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StartRequest(BaseModel):
    mode: str                 # paper | real
    sessions: Optional[int]   # 10 | 50 | 100 | null (24/7)
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
    return {"ok": True, "status": "started", "mode": STATE.mode, "sessions": STATE.max_sessions}

@app.post("/stop")
def stop_bot():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "stopped"
    return {"ok": True, "status": "stopped"}

@app.get("/status")
def status():
    with LOCK:
        return {
            "running": STATE.running,
            "mode": STATE.mode,
            "current_sessions": STATE.current_sessions,
            "max_sessions": STATE.max_sessions,
            "wallet_id": STATE.wallet_id,
            "status_msg": STATE.status_msg
        }

@app.get("/sessions")
def list_sessions():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, condition_id, market_question, start_ts, end_ts, mode, wallet_id, had_opportunity, tier
        FROM sessions ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

@app.get("/trades")
def list_trades():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, session_id, mode, side, entry_price, exit_price, size, pnl, tx_id, ts
        FROM trades ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

# =========================
# START THREAD
# =========================
thread = threading.Thread(target=bot_loop, daemon=True)
thread.start()
