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
      body {{
        background:#2b2b2b;
        color:#ffffff;
        font-family: Arial, sans-serif;
        padding:40px;
      }}
      h1 {{
        text-align:center;
        margin-bottom:25px;
        font-size:34px;
      }}
      #visor {{
        text-align:center;
        color:#ff4444;
        font-size:26px;
        margin-bottom:35px;
      }}
      .box {{
        border:2px solid #ffd700;
        padding:22px;
        margin-bottom:25px;
        border-radius:8px;
      }}
      label {{
        font-size:18px;
        display:block;
        margin-top:14px;
      }}
      .tip {{
        font-size:13px;
        color:#cccccc;
        margin-top:4px;
      }}
      input, select, button {{
        padding:12px;
        font-size:18px;
        margin-top:6px;
        margin-right:8px;
      }}
      button {{
        border:none;
        border-radius:6px;
        cursor:pointer;
      }}
      .glow {{
        box-shadow:0 0 14px;
      }}
      table {{
        width:100%;
        border-collapse:collapse;
        margin-top:15px;
        font-size:16px;
      }}
      th, td {{
        border:1px solid #666;
        padding:8px;
        text-align:center;
      }}
      th {{
        background:#3a3a3a;
      }}
      a {{
        color:#f5f5f5;
        text-decoration:none;
        font-size:18px;
        margin-right:15px;
      }}
      a:hover {{
        text-decoration:underline;
      }}
      .footer {{
        text-align:center;
        margin-top:30px;
      }}
    </style>
    </head>
    <body>

    <h1>🐝 BumbleBee v19</h1>

    <div id="visor">{STATE.status_msg}</div>

    <div class="box">
      <label>Shares (por lado)</label>
      <input id="shares" value="{STATE.shares}">
      <div class="tip">Quantidade máxima de shares compradas em YES e em NO durante a sessão</div>

      <label>Preço máximo (YES / NO)</label>
      <input id="mp" value="{STATE.max_price}">
      <div class="tip">O bot compra YES ou NO apenas se o preço for menor ou igual a este valor</div>

      <label>Max Sessions</label>
      <input id="ms">
      <div class="tip">Vazio ou 0 = roda 24/7 | Número = total de sessões antes de parar</div>

      <label>Modo</label>
      <select id="mode">
        <option value="paper">paper</option>
        <option value="real">real</option>
      </select>
      <div class="tip">Paper = simulação | Real = execução futura</div>

      <br><br>
      <button onclick="save(this)">SALVAR</button>
      <span class="tip">Aplica as configurações acima</span><br><br>

      <button onclick="startBot(this)">START</button>
      <span class="tip">Inicia o bot com as configurações atuais</span><br><br>

      <button onclick="stopBot(this)">STOP</button>
      <span class="tip">Para o bot após a execução atual</span>
    </div>

    <div class="box">
      <h2>Sessões</h2>
      <div class="tip">Resumo das últimas sessões (preenchimento e resultado)</div>
      <table>
        <tr><th>ID</th><th>YES</th><th>NO</th><th>Profit</th></tr>
        {''.join(f"<tr><td>{s[0]}</td><td>{s[6]}</td><td>{s[7]}</td><td>{round(s[9],4)}</td></tr>" for s in sessions)}
      </table>
    </div>

    <div class="box">
      <h2>Trades</h2>
      <div class="tip">Execuções individuais dentro das sessões</div>
      <table>
        <tr><th>Side</th><th>Price</th><th>Shares</th><th>Time</th></tr>
        {''.join(f"<tr><td>{t[3]}</td><td>{t[4]}</td><td>{t[5]}</td><td>{t[2]}</td></tr>" for t in trades)}
      </table>
    </div>

    <div class="footer">
      <a href="https://polymarket.com" target="_blank">Polymarket</a>
      <a href="https://kashi.io" target="_blank">Kashi</a>
    </div>

    <script>
      function glow(b) {{
        b.classList.add('glow');
        setTimeout(()=>b.classList.remove('glow'),600);
      }}
      async function save(b) {{
        glow(b);
        await fetch('/controls', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{
            shares:parseInt(shares.value),
            max_price:parseFloat(mp.value),
            max_sessions: ms.value ? parseInt(ms.value) : 0,
            mode:mode.value
          }})
        }});
        visor.innerText="CONFIGURAÇÕES SALVAS";
      }}
      async function startBot(b) {{
        glow(b);
        await fetch('/start',{{method:'POST'}});
        visor.innerText="BOT INICIADO";
      }}
      async function stopBot(b) {{
        glow(b);
        await fetch('/stop',{{method:'POST'}});
        visor.innerText="BOT PARADO";
      }}
    </script>

    </body>
    </html>
    """
    return html

# =========================
# START
# =========================
threading.Thread(target=bot_loop, daemon=True).start()
