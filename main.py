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
        wallet_id TEXT,
        shares INTEGER,
        t2 REAL,
        t3 REAL,
        entered INTEGER DEFAULT 0
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
        self.max_sessions = 1
        self.current_sessions = 0
        self.wallet_id = "pt"
        self.t2 = 0.85
        self.t3 = 0.70
        self.shares = 20
        self.status_msg = "idle"

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

def get_latest_yes_no_prices():
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
    session_end_dt = None
    entered = False

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
                INSERT INTO sessions
                (condition_id, market_question, start_ts, mode, wallet_id, shares, t2, t3)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market["conditionId"],
                market["question"],
                datetime.utcnow().isoformat(),
                STATE.mode,
                STATE.wallet_id,
                STATE.shares,
                STATE.t2,
                STATE.t3
            ))
            session_id = cur.lastrowid
            conn.commit()
            conn.close()
            entered = False

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
            yes, no = prices
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
                yes, no
            ))
            conn.commit()
            conn.close()

            if not entered:
                s = yes + no
                if s <= STATE.t3 or s <= STATE.t2:
                    entered = True
                    conn = db()
                    cur = conn.cursor()
                    cur.execute("UPDATE sessions SET entered=1 WHERE id=?", (session_id,))
                    conn.commit()
                    conn.close()
                    STATE.status_msg = f"ENTROU {STATE.shares} SHARES ({STATE.mode})"

        time.sleep(PRICE_POLL_SECONDS)

# =========================
# API
# =========================
app = FastAPI(title="BumbleBee")

class ControlReq(BaseModel):
    t2: Optional[float] = None
    t3: Optional[float] = None
    shares: Optional[int] = None
    mode: Optional[str] = None
    max_sessions: Optional[int] = None

@app.post("/start")
def start_bot():
    with LOCK:
        STATE.running = True
        STATE.current_sessions = 0
        STATE.status_msg = "bot iniciado"
    return {"ok": True}

@app.post("/stop")
def stop_bot():
    with LOCK:
        STATE.running = False
        STATE.status_msg = "bot parado"
    return {"ok": True}

@app.post("/reset")
def reset_bot():
    with LOCK:
        STATE.running = False
        STATE.current_sessions = 0
        STATE.status_msg = "estado resetado"
    return {"ok": True}

@app.post("/controls")
def update_controls(req: ControlReq):
    with LOCK:
        if req.t2 is not None: STATE.t2 = req.t2
        if req.t3 is not None: STATE.t3 = req.t3
        if req.shares is not None: STATE.shares = req.shares
        if req.mode is not None: STATE.mode = req.mode
        if req.max_sessions is not None: STATE.max_sessions = req.max_sessions
        STATE.status_msg = "controles atualizados"
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
      .box {{ border:2px solid gold; padding:15px; margin-top:15px }}
      .hint {{ font-size:12px; color:#aaa }}
    </style>
    </head>
    <body>

    <h1>BUMBLEBEE — CONTROL DASH</h1>

    <div class="box">
      <label>T2 (%)</label>
      <input id="t2" value="{STATE.t2}">
      <div class="hint">Entrada conservadora (soma YES+NO abaixo deste valor)</div>

      <label>T3 (%)</label>
      <input id="t3" value="{STATE.t3}">
      <div class="hint">Entrada agressiva (edge maior, risco maior)</div>

      <label>Shares</label>
      <input id="shares" value="{STATE.shares}">
      <div class="hint">Quantidade fixa de shares por sessão (1 trade)</div>

      <label>Max Sessions</label>
      <input id="maxs" value="{STATE.max_sessions}">
      <div class="hint">Número de sessões de 15 min antes de parar</div>

      <label>Modo</label>
      <select id="mode">
        <option value="paper">paper</option>
        <option value="real">real</option>
      </select>
      <div class="hint">Paper = simulação | Real = dinheiro</div>

      <button onclick="save()">SALVAR CONFIG</button>
    </div>

    <div class="box">
      <button class="on" onclick="start()">START</button>
      <span class="hint">Inicia o bot com as configurações atuais</span><br>

      <button class="off" onclick="stop()">STOP</button>
      <span class="hint">Para imediatamente após a sessão atual</span><br>

      <button onclick="reset()">RESET</button>
      <span class="hint">Zera sessões e estado (não apaga histórico)</span>
    </div>

    <div class="box">
      <h3>VISOR</h3>
      <p id="visor">{STATE.status_msg}</p>
      <div class="hint">Mostra a última ação executada</div>
    </div>

    <script>
      async function save(){{
        await fetch('/controls', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{
            t2:parseFloat(t2.value),
            t3:parseFloat(t3.value),
            shares:parseInt(shares.value),
            max_sessions:parseInt(maxs.value),
            mode:mode.value
          }})
        }});
        visor.innerText="CONFIGURAÇÕES SALVAS";
      }}
      async function start(){{ await fetch('/start',{{method:'POST'}}); visor.innerText="BOT INICIADO"; }}
      async function stop(){{ await fetch('/stop',{{method:'POST'}}); visor.innerText="BOT PARADO"; }}
      async function reset(){{ await fetch('/reset',{{method:'POST'}}); visor.innerText="RESET EXECUTADO"; }}
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START THREAD
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
