# ======================================================
# 🐝 BumbleBee v19.3 beta
# ======================================================

import time
import threading
import sqlite3
import requests
from datetime import datetime, timezone
from typing import Optional, List, Dict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

DB_NAME = "polymarket.db"
PRICE_POLL_SECONDS = 5
MARKET_REFRESH_SECONDS = 30

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
        self.mode = "paper"
        self.shares = 20
        self.max_price = 0.6
        self.max_sessions = None
        self.current_sessions = 0
        self.status_msg = "IDLE"
        self.start_time = None

        self.active_markets: List[Dict] = []
        self.last_market_refresh = 0
        self.current_market: Optional[Dict] = None

STATE = BotState()
LOCK = threading.Lock()

# =========================
# MARKET DETECTION
# =========================
def load_candidate_markets():
    r = requests.get(
        f"{GAMMA_API}/markets",
        params={"active": True, "closed": False, "limit": 200},
        timeout=10
    )
    out = []
    for m in r.json():
        q = (m.get("question") or "").lower()
        if "btc" in q and ("15" in q or "15m" in q or "15-min" in q):
            out.append(m)
    return out

def refresh_markets_if_needed():
    now = time.time()
    if now - STATE.last_market_refresh < MARKET_REFRESH_SECONDS:
        return
    try:
        markets = load_candidate_markets()
        if markets:
            STATE.active_markets = markets
            STATE.last_market_refresh = now
    except:
        STATE.status_msg = "ERRO AO CARREGAR MERCADOS"

def select_active_market():
    if not STATE.active_markets:
        return None
    def end_ts(m):
        try:
            return datetime.fromisoformat(m["endDate"].replace("Z","+00:00"))
        except:
            return datetime.max.replace(tzinfo=timezone.utc)
    return sorted(STATE.active_markets, key=end_ts)[0]

def get_latest_prices():
    r = requests.get(f"{DATA_API}/trades", params={"limit": 50}, timeout=10)
    yes = no = None
    for t in r.json():
        side = (t.get("outcome") or "").upper()
        price = float(t.get("price"))
        if side == "YES": yes = price
        elif side == "NO": no = price
        if yes is not None and no is not None:
            return yes, no
    return None

# =========================
# BOT LOOP
# =========================
def bot_loop():
    session_id = None
    session_end = None
    session_condition = None

    while True:
        with LOCK:
            if not STATE.running:
                time.sleep(1)
                continue

        refresh_markets_if_needed()
        market = select_active_market()

        if session_id is None:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sessions
                (condition_id, market_question, start_ts, mode, shares_target, max_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                market["conditionId"] if market else None,
                market["question"] if market else "PENDING MARKET",
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.shares,
                STATE.max_price
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()
            STATE.status_msg = "SESSÃO INICIADA"

        if market and market.get("conditionId") != session_condition:
            session_condition = market["conditionId"]
            session_end = datetime.fromisoformat(
                market["endDate"].replace("Z","+00:00")
            )
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                UPDATE sessions
                SET condition_id=?, market_question=?
                WHERE id=?
            """, (market["conditionId"], market["question"], session_id))
            conn.commit()
            conn.close()
            STATE.status_msg = "MERCADO ASSOCIADO"

        if not market:
            time.sleep(PRICE_POLL_SECONDS)
            continue

        if datetime.now(timezone.utc) >= session_end:
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
            cost_yes, cost_no, sy, sn = cur.fetchone()
            cost_yes = cost_yes or 0
            cost_no  = cost_no  or 0
            sy = sy or 0
            sn = sn or 0
            profit = min(sy, sn) - (cost_yes + cost_no)

            cur.execute("""
                UPDATE sessions SET end_ts=?, profit=? WHERE id=?
            """, (datetime.utcnow().isoformat(), profit, session_id))
            conn.commit()
            conn.close()

            with LOCK:
                STATE.current_sessions += 1

            session_id = None
            session_condition = None
            session_end = None
            continue

        prices = get_latest_prices()
        if prices:
            yes, no = prices
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT shares_yes, shares_no FROM sessions WHERE id=?", (session_id,))
            sy, sn = cur.fetchone()

            if yes <= STATE.max_price and sy < STATE.shares:
                buy = STATE.shares - sy
                cur.execute("""
                    INSERT INTO trades VALUES (NULL,?,?,?,?,?)
                """, (session_id, datetime.utcnow().isoformat(), "YES", yes, buy))
                cur.execute("UPDATE sessions SET shares_yes=shares_yes+? WHERE id=?",
                            (buy, session_id))
                STATE.status_msg = f"BUY YES {buy} @ {yes}"

            if no <= STATE.max_price and sn < STATE.shares:
                buy = STATE.shares - sn
                cur.execute("""
                    INSERT INTO trades VALUES (NULL,?,?,?,?,?)
                """, (session_id, datetime.utcnow().isoformat(), "NO", no, buy))
                cur.execute("UPDATE sessions SET shares_no=shares_no+? WHERE id=?",
                            (buy, session_id))
                STATE.status_msg = f"BUY NO {buy} @ {no}"

            conn.commit()
            conn.close()

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee v19.3 beta")

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
        STATE.start_time = time.time()
        STATE.status_msg = "BOT INICIADO"
    return {"ok": True}

@app.get("/status")
def status():
    elapsed = int(time.time() - STATE.start_time) if STATE.start_time else 0
    h = elapsed // 3600
    m = (elapsed % 3600) // 60
    s = elapsed % 60
    return {
        **STATE.__dict__,
        "elapsed": f"{h:02d}:{m:02d}:{s:02d}"
    }

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT profit FROM sessions WHERE profit IS NOT NULL")
    total_profit = sum(r[0] for r in cur.fetchall() if r[0] is not None)
    cur.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 5")
    sessions = cur.fetchall()
    conn.close()

    html = f"""
    <html>
    <head>
    <style>
      body {{ background:#2b2b2b;color:#fff;font-family:Arial;padding:20px;font-size:14px }}
      h1 {{ text-align:center;font-size:26px }}
      .top {{ display:flex;justify-content:space-between;align-items:center }}
      .box {{ border:2px solid #ffd700;padding:12px;border-radius:8px }}
      input,select,button {{ font-size:14px;padding:6px }}
      table {{ width:100%;border-collapse:collapse;font-size:13px }}
      th,td {{ border:1px solid #666;padding:6px;text-align:center }}
    </style>
    </head>
    <body>

    <h1>🐝 BumbleBee v19.3 beta</h1>

    <div class="top box">
      <div>
        Shares <input id="shares" value="{STATE.shares}">
        Max Price <input id="mp" value="{STATE.max_price}">
        Mode <select id="mode"><option>paper</option><option>real</option></select>
        <button onclick="start()">START</button>
      </div>
      <div>
        ⏱️ Elapsed: <b id="elapsed">--</b><br>
        💰 Total Profit: <b>{round(total_profit,4)}</b>
      </div>
    </div>

    <script>
      async function refresh(){{
        const r = await fetch('/status');
        const s = await r.json();
        document.getElementById("elapsed").innerText = s.elapsed;
      }}
      async function start(){{
        await fetch('/start',{{method:'POST'}});
      }}
      setInterval(refresh,2000);
      refresh();
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
