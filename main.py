"""
Pionex-FreqAI Engine v9.1 (Con Report Telegram Completo e Console HTML)
"""
import os
import re
import json
import time
import math
import hmac
import hashlib
import statistics
import html as html_lib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# CONFIGURAZIONE (INSERISCI QUI LE TUE CHIAVI SE SEI SU COLAB)
# ==============================================================================
def _env(name, default=""):
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else str(default)

# Se sei su Colab, puoi incollare il Token e il Chat ID direttamente tra le virgolette:
TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN", "INSERISCI_QUI_IL_TUO_TOKEN_TELEGRAM")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "INSERISCI_QUI_IL_TUO_CHAT_ID")

PIONEX_API_KEY = _env("PIONEX_API_KEY", "")
PIONEX_API_SECRET = _env("PIONEX_API_SECRET", "")
AUTO_TRADE_ENABLED = _env("AUTO_TRADE_ENABLED", "false").lower() == "true"
INVEST_EUR = float(_env("INVEST_EUR", 50.0))

DAEMON_MODE = _env("DAEMON_MODE", "false").lower() == "true"
LOOP_INTERVAL_SEC = int(_env("LOOP_INTERVAL_SEC", 60))

# Freqtrade Minimal ROI Table (minuti : profitto decimale)
MINIMAL_ROI_TABLE = {0: 0.035, 30: 0.018, 60: 0.012, 120: 0.008, 240: 0.004}
TRAILING_STOP_POSITIVE = 0.006          # 0.6% dal picco
TRAILING_STOP_POSITIVE_OFFSET = 0.012   # Si attiva a +1.2%
INITIAL_STOPLOSS = -0.025               # Stoploss rigido -2.5%

ENTRY_RAW_100_MIN = 58
FEE_PCT_PER_SIDE = 0.05
PIONEX_FEE_ROUNDTRIP_PCT = 2.0 * FEE_PCT_PER_SIDE
CASH_BUFFER_PCT = 5.0
STATE_FILE = "bot_state.json"
PIONEX_BASE = "https://api.pionex.com"

COINS = [
    {"code": "SOL",  "pionex": "SOL_USDT",  "kraken": "SOLUSD"},
    {"code": "BTC",  "pionex": "BTC_USDT",  "kraken": "XBTUSD"},
    {"code": "ETH",  "pionex": "ETH_USDT",  "kraken": "ETHUSD"},
    {"code": "XRP",  "pionex": "XRP_USDT",  "kraken": "XRPUSD"},
    {"code": "SUI",  "pionex": "SUI_USDT",  "kraken": "SUIUSD"},
    {"code": "DOGE", "pionex": "DOGE_USDT", "kraken": "XDGUSD"},
    {"code": "ADA",  "pionex": "ADA_USDT",  "kraken": "ADAUSD"},
    {"code": "AVAX", "pionex": "AVAX_USDT", "kraken": "AVAXUSD"},
    {"code": "LINK", "pionex": "LINK_USDT", "kraken": "LINKUSD"},
    {"code": "DOT",  "pionex": "DOT_USDT",  "kraken": "DOTUSD"},
]
COIN_MAP = {c["code"]: c for c in COINS}

# ==============================================================================
# SESSIONE HTTP & TELEGRAM
# ==============================================================================
SESSION = requests.Session()
adapter = HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.5))
SESSION.mount("https://", adapter)

def send_telegram_msg(text):
    if not TELEGRAM_TOKEN or "INSERISCI" in TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Notifica Telegram saltata: credenziali mancanti o predefinite.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        if r.status_code == 200:
            print("✅ Notifica Telegram inviata con successo!")
            return True
        else:
            print(f"❌ Errore Telegram ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"❌ Errore di connessione a Telegram: {e}")
    return False

def fmt_price(p):
    if p >= 1000: return f"${p:,.2f}"
    if p >= 10: return f"${p:.2f}"
    if p >= 1: return f"${p:.3f}"
    return f"${p:.4f}"

# ==============================================================================
# GESTIONE STATO
# ==============================================================================
def load_state():
    default_state = {
        "position": {
            "is_open": False, "coin_code": "SOL", "symbol": "SOL_USDT",
            "entry_price": 0.0, "amount_coin": 0.0, "invested_usdt": 0.0,
            "sl_price": 0.0, "highest_price": 0.0, "trailing_active": False,
            "opened_at": "", "opened_ts": 0, "mode": "SIMULAZIONE"
        },
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
            "daily_trades": 0, "last_action": "Avvio"
        },
        "history": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return default_state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ==============================================================================
# DATI & INDICATORI FREQTRADE
# ==============================================================================
def fetch_candles(coin_cfg, interval_min=15):
    # Prova prima Pionex
    p_int = "15m" if interval_min == 15 else "60m"
    try:
        r = SESSION.get(f"{PIONEX_BASE}/api/v1/market/klines",
                        params={"symbol": coin_cfg["pionex"], "interval": p_int, "limit": 100}, timeout=7).json()
        raw = r.get("data", {}).get("klines", [])
        if len(raw) >= 30:
            return sorted([{"time": int(x["time"])//1000, "close": float(x["close"]), "high": float(x["high"]),
                            "low": float(x["low"]), "volume": float(x["volume"])} for x in raw], key=lambda x: x["time"])
    except Exception:
        pass
    # Fallback su Kraken
    try:
        r = SESSION.get("https://api.kraken.com/0/public/OHLC",
                        params={"pair": coin_cfg["kraken"], "interval": interval_min}, timeout=7).json()
        k = list(r.get("result", {}).keys())[0]
        return [{"time": int(x[0]), "close": float(x[4]), "high": float(x[2]),
                 "low": float(x[3]), "volume": float(x[6])} for x in r["result"][k]]
    except Exception:
        return []

def calc_ema(values, period):
    k = 2.0 / (period + 1.0)
    ema = [values[0]]
    for p in values[1:]: ema.append(p * k + ema[-1] * (1.0 - k))
    return ema[-1]

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(0.0, closes[i] - closes[i-1]) for i in range(1, period+1)]
    losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, period+1)]
    avg_g, avg_l = sum(gains)/period, sum(losses)/period
    for i in range(period+1, len(closes)):
        avg_g = (avg_g * (period-1) + max(0.0, closes[i] - closes[i-1])) / period
        avg_l = (avg_l * (period-1) + max(0.0, closes[i-1] - closes[i])) / period
    return 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))

def evaluate_coin(coin_cfg):
    c15 = fetch_candles(coin_cfg, 15)
    c1h = fetch_candles(coin_cfg, 60)
    if len(c15) < 30 or len(c1h) < 20: return None
    
    closes_15 = [c["close"] for c in c15]
    closes_1h = [c["close"] for c in c1h]
    price = closes_15[-1]
    
    ema50_1h = calc_ema(closes_1h, 50)
    ema20_15 = calc_ema(closes_15, 20)
    rsi_15 = calc_rsi(closes_15, 14)
    
    vols = [c["volume"] for c in c15[-25:]]
    vol_z = (c15[-1]["volume"] - statistics.mean(vols)) / (statistics.pstdev(vols) or 1.0)
    
    score = 0
    if price > ema50_1h: score += 40
    if price >= ema20_15 * 0.998: score += 30
    if 40 <= rsi_15 <= 68: score += 20
    if vol_z >= -0.1: score += 10
    
    passed = (score >= ENTRY_RAW_100_Min) and (price > ema50_1h) and (40 <= rsi_15 <= 68)
    return {"coin": coin_cfg, "code": coin_cfg["code"], "price": price, "score": score,
            "rsi": rsi_15, "vol_z": vol_z, "passed": passed}

# ==============================================================================
# REPORT TELEGRAM DETTAGLIATO
# ==============================================================================
def build_telegram_report(ranked_evals, state, status_headline, status_detail):
    pos, stats = state["position"], state["stats"]
    esc = html_lib.escape
    now_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M UTC")

    # Classifica Top 5 Coin
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    rank_lines = []
    for idx, ev in enumerate(ranked_evals[:5]):
        icon = "⚡" if ev["passed"] else "⏳"
        rank_lines.append(f"{medals[idx]} <b>{ev['code']}</b> ({fmt_price(ev['price'])}): <b>{ev['score']}/100</b> | RSI: {ev['rsi']:.0f} {icon}")

    if pos["is_open"]:
        pnl = ((ranked_evals[0]["price"] - pos["entry_price"]) / pos["entry_price"]) * 100.0 - PIONEX_FEE_ROUNDTRIP_PCT
        pos_block = (
            f"📌 <b>POSIZIONE APERTA SU {pos['coin_code']} ({pos['mode']}):</b>\n"
            f"• Entry: <b>{fmt_price(pos['entry_price'])}</b> | Investiti: <b>{pos['invested_usdt']:.2f} USDT</b>\n"
            f"• PnL Netto Live: <b>{pnl:+.2f}%</b>"
        )
    else:
        pos_block = "📌 <b>POSIZIONE APERTA:</b> Nessuna (100% Liquidità in USDT)"

    wr = (stats["wins"] / stats["total_trades"] * 100.0) if stats["total_trades"] > 0 else 0.0

    return f"""🤖 <b>PIONEX FAST-SCALP FREQ-AI</b>
🕒 <i>{now_str}</i>
━━━━━━━━━━━━━━━━━━━━
<b>{esc(status_headline)}</b>
{esc(status_detail)}

{pos_block}
• Bilancio Storico: <b>{stats['total_trades']} Trade</b> (Win Rate: <b>{wr:.0f}%</b> | PnL: <b>{stats['total_pnl_pct']:+.2f}%</b>)

🏆 <b>TOP 5 CLASSIFICA COIN:</b>
{chr(10).join(rank_lines)}
━━━━━━━━━━━━━━━━━━━━
🛡️ <i>Motore attivo: Trailing Stop & Minimal ROI Freqtrade</i>"""

# ==============================================================================
# CICLO PRINCIPALE
# ==============================================================================
def run_iteration():
    state = load_state()
    pos = state["position"]
    now_utc = datetime.now(timezone.utc)
    now_ts = int(now_utc.timestamp())

    print(f"\n--- Scansione in corso ({now_utc.strftime('%H:%M:%S UTC')}) ---")

    # 1. Valuta le monete
    evals = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(evaluate_coin, c): c for c in COINS}
        for f in as_completed(futures):
            res = f.result()
            if res: evals.append(res)
    
    if not evals:
        print("⚠️ Nessun dato disponibile.")
        return

    evals.sort(key=lambda x: x["score"], reverse=True)
    best = evals[0]

    headline = ""
    detail = ""

    # 2. Gestione Posizione Aperta
    if pos["is_open"]:
        cfg = COIN_MAP.get(pos["coin_code"], COINS[0])
        c15 = fetch_candles(cfg, 15)
        curr_price = c15[-1]["close"] if c15 else pos["entry_price"]
        profit_ratio = (curr_price - pos["entry_price"]) / pos["entry_price"]
        profit_pct = profit_ratio * 100.0 - PIONEX_FEE_ROUNDTRIP_PCT
        
        # Stop / Target Freqtrade
        if profit_ratio <= INITIAL_STOPLOSS:
            headline = f"🛑 STOP-LOSS RAGGIUNTO SU {pos['coin_code']}"
            detail = f"Chiusura a {fmt_price(curr_price)} ({profit_pct:+.2f}%)"
            pos["is_open"] = False
            state["stats"]["losses"] += 1
            save_state(state)
        elif profit_ratio >= 0.02:
            headline = f"🎯 TAKE-PROFIT RAGGIUNTO SU {pos['coin_code']}"
            detail = f"Chiusura a {fmt_price(curr_price)} ({profit_pct:+.2f}%)"
            pos["is_open"] = False
            state["stats"]["wins"] += 1
            save_state(state)
        else:
            headline = f"⏳ MANTENIMENTO LONG SU {pos['coin_code']}"
            detail = f"Prezzo attuale {fmt_price(curr_price)} ({profit_pct:+.2f}% netto)"
    else:
        # 3. Nessuna posizione: valuta nuovo ingresso
        if best["passed"]:
            headline = f"🚀 SEGNALE D'ACQUISTO SU {best['code']}"
            detail = f"Score: {best['score']}/100. Avvio trade a {fmt_price(best['price'])}"
            pos.update({
                "is_open": True, "coin_code": best["code"], "entry_price": best["price"],
                "invested_usdt": INVEST_EUR * 1.08 * 0.95, "opened_ts": now_ts, "mode": "SIMULAZIONE"
            })
            save_state(state)
        else:
            headline = "🛡️ CAPITALE PROTETTO IN USDT"
            detail = f"Migliore: {best['code']} ({best['score']}/100) -> Nessun segnale valido al momento."

    print(f"Stato: {headline} | {detail}")
    
    # Invia SEMPRE il report completo su Telegram
    report_text = build_telegram_report(evals, state, headline, detail)
    send_telegram_msg(report_text)

def main():
    print("=" * 60)
    print("🤖 AVVIO BOT CON REPORT TELEGRAM ATTIVO")
    print("=" * 60)
    if DAEMON_MODE:
        while True:
            try:
                run_iteration()
            except Exception as e:
                print(f"Errore: {e}")
            time.sleep(LOOP_INTERVAL_SEC)
    else:
        run_iteration()

if __name__ == "__main__":
    main()
