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
# BUMBLEBEE v18
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
    shares = 20              # por lado
    threshold = 0.70
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
            s = yes + no

            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT shares_filled FROM sessions WHERE id=?", (session_id,))
            filled = cur.fetchone()[0]

            if s < STATE.threshold and filled < STATE.shares:
                remaining = STATE.shares - filled

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

                STATE.status_msg = f"EXECUTOU {remaining} YES + {remaining} NO | s={round(s,4)}"

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee v18")

class ControlReq(BaseModel):
    shares: Optional[int] = None
    threshold: Optional[float] = None
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

@app.post("/reset")
def reset():
    with LOCK:
        STATE.running = False
        STATE.current_sessions = 0
        STATE.status_msg = "RESET EXECUTADO"
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
        if req.max_sessions is not None:
            STATE.max_sessions = req.max_sessions if req.max_sessions > 0 else None
        STATE.status_msg = "CONFIGURAÇÕES ATUALIZADAS"
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
      body {{ background:#0f0f0f; color:#fff; font-family:Arial; padding:30px }}
      input, select, button {{ padding:10px; font-size:15px; margin:5px }}
      button {{ border:none; border-radius:6px; cursor:pointer }}
      .on {{ background:#00c853 }}
      .off {{ background:#d50000 }}
      .glow {{ box-shadow:0 0 15px }}
      .box {{ border:2px solid gold; padding:15px; margin-top:15px }}
      .hint {{ font-size:12px; color:#aaa }}
      #visor {{ text-align:center; color:#ff4444; font-size:22px; margin-bottom:20px }}
    </style>
    </head>
    <body>

    <div id="visor">{STATE.status_msg}</div>

    <div class="box">
      <label>Shares</label>
      <input id="shares" value="{STATE.shares}">
      <div class="hint">Quantidade de shares por lado (YES + NO)</div>

      <label>Threshold</label>
      <input id="thr" value="{STATE.threshold}">
      <div class="hint">Executa quando YES + NO &lt; threshold</div>

      <label>Max Sessions</label>
      <input id="maxs">
      <div class="hint">Vazio = 24/7 | Número = limite de sessões</div>

      <label>Modo</label>
      <select id="mode">
        <option value="paper">paper</option>
        <option value="real">real</option>
      </select>

      <button onclick="save(this)">SALVAR</button>
    </div>

    <div class="box">
      <button class="on" onclick="startBot(this)">START</button>
      <span class="hint">Inicia o bot</span><br>

      <button class="off" onclick="stopBot(this)">STOP</button>
      <span class="hint">Para o bot</span><br>

      <button onclick="resetBot(this)">RESET</button>
      <span class="hint">Reseta estado interno</span>
    </div>

    <script>
      function glow(btn,color){{
        btn.classList.add('glow');
        btn.style.boxShadow = '0 0 15px '+color;
        setTimeout(()=>btn.classList.remove('glow'),800);
      }}

      async function save(btn){{
        glow(btn,'#ffd700');
        await fetch('/controls', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{
            shares:parseInt(shares.value),
            threshold:parseFloat(thr.value),
            max_sessions: maxs.value ? parseInt(maxs.value) : 0,
            mode:mode.value
          }})
        }});
        visor.innerText="CONFIGURAÇÕES SALVAS";
      }}

      async function startBot(btn){{ glow(btn,'#00ff00'); await fetch('/start',{{method:'POST'}}); visor.innerText="BOT INICIADO"; }}
      async function stopBot(btn){{ glow(btn,'#ff0000'); await fetch('/stop',{{method:'POST'}}); visor.innerText="BOT PARADO"; }}
      async function resetBot(btn){{ glow(btn,'#ffffff'); await fetch('/reset',{{method:'POST'}}); visor.innerText="RESET EXECUTADO"; }}
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
