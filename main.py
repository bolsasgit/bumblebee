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
        shares_filled INTEGER DEFAULT 0,
        threshold REAL
    );

    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        ts TEXT,
        s_value REAL,
        shares INTEGER,
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
    running = False
    mode = "paper"
    shares = 20            # POR LADO
    threshold = 0.70
    status_msg = "idle"

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
                (condition_id, market_question, start_ts, mode, shares_target, threshold)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                condition_id,
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.shares,
                STATE.threshold
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()

        seconds_left = int((session_end - datetime.now(timezone.utc)).total_seconds())
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
            condition_id = None
            continue

        prices = get_latest_prices()
        if prices:
            yes, no = prices
            s = yes + no

            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT shares_filled FROM sessions WHERE id=?", (session_id,))
            filled = cur.fetchone()[0]

            if s < STATE.threshold and filled < STATE.shares:
                remaining = STATE.shares - filled

                # EXECUTA SEMPRE NO MELHOR s DO MOMENTO
                cur.execute("""
                    INSERT INTO trades
                    (session_id, ts, s_value, shares, price_yes, price_no)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    datetime.utcnow().isoformat(),
                    s,
                    remaining,
                    yes,
                    no
                ))

                cur.execute("""
                    UPDATE sessions
                    SET shares_filled = shares_filled + ?
                    WHERE id=?
                """, (remaining, session_id))

                conn.commit()
                conn.close()

                STATE.status_msg = f"EXECUTOU {remaining} YES + {remaining} NO @ s={round(s,4)}"

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee")

class ControlReq(BaseModel):
    shares: Optional[int] = None
    threshold: Optional[float] = None
    mode: Optional[str] = None

@app.post("/start")
def start():
    with LOCK:
        STATE.running = True
        STATE.status_msg = "running"
    return {"ok": True}

@app.post("/stop")
def stop():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "stopped"
    return {"ok": True}

@app.post("/controls")
def controls(req: ControlReq):
    with LOCK:
        if req.shares is not None:
            STATE.shares = req.shares
        if req.threshold is not None:
            STATE.threshold = req.threshold
        if req.mode is not None:
            STATE.mode = req.mode
        STATE.status_msg = "controls updated"
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
    <body style="background:#0f0f0f;color:white;font-family:Arial;padding:30px">
      <h1>BUMBLEBEE — STRUCTURAL BOT</h1>

      <div>
        Shares por lado:
        <input id="shares" value="{STATE.shares}">
        <br>
        Threshold (YES+NO):
        <input id="thr" value="{STATE.threshold}">
        <br><br>
        <button onclick="save()">SALVAR</button>
        <button onclick="start()">START</button>
        <button onclick="stop()">STOP</button>
      </div>

      <h3>Status</h3>
      <p id="visor">{STATE.status_msg}</p>

      <script>
        async function save(){{
          await fetch('/controls', {{
            method:'POST',
            headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{
              shares:parseInt(shares.value),
              threshold:parseFloat(thr.value)
            }})
          }});
          visor.innerText="CONFIG SALVA";
        }}
        async function start(){{ await fetch('/start',{{method:'POST'}}); visor.innerText="RODANDO"; }}
        async function stop(){{ await fetch('/stop',{{method:'POST'}}); visor.innerText="PARADO"; }}
      </script>
    </body>
    </html>
    """
    return html

# =========================
# START
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
