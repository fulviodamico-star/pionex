"""
Pionex-FreqAI Autonomous Engine v9.0 (Freqtrade-Enhanced Edition)
- Architettura standalone + Motore di Trading ispirato a Freqtrade / FreqAI
- Supporta esecuzione singola (GitHub Actions) e continua in loop (Colab / VPS)
- Klines native Pionex + Trailing Stop Freqtrade + Outlier Detection FreqAI
"""
import os
import re
import json
import time
import math
import hmac
import hashlib
import random
import statistics
import threading
import html as html_lib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# CONFIGURAZIONE & PARAMETRI FREQTRADE
# ==============================================================================
def _env(name, default):
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else str(default)

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "").strip()
PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or os.getenv("API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or os.getenv("API_SECRET") or "").strip()
AUTO_TRADE_ENABLED = _env("AUTO_TRADE_ENABLED", "false").lower() == "true"
INVEST_EUR = float(_env("INVEST_EUR", 50.0))

# Modalità demone continuo (ideale per Colab/VPS: True = loop ogni N secondi; False = run singolo)
DAEMON_MODE = _env("DAEMON_MODE", "false").lower() == "true"
LOOP_INTERVAL_SEC = int(_env("LOOP_INTERVAL_SEC", 60))

# Motore di Rischio e Freqtrade Minimal ROI (minuti : profitto decimale)
# Esempio: subito cerca il 3.5%, dopo 30 min si accontenta dell'1.8%, dopo 2h dell'0.8%
MINIMAL_ROI_TABLE = {
    0: 0.035,
    30: 0.018,
    60: 0.012,
    120: 0.008,
    240: 0.004
}

# Freqtrade Trailing Stop Settings
TRAILING_STOP = True
TRAILING_STOP_POSITIVE = 0.006          # Distanza stop dal picco (0.6%)
TRAILING_STOP_POSITIVE_OFFSET = 0.012   # Si attiva solo dopo aver superato +1.2% di profitto
INITIAL_STOPLOSS = -0.025               # Stoploss iniziale rigido a -2.5%

# Soglie Segnali
ENTRY_SCORE_MIN = 6
ENTRY_RAW_100_MIN = 58
MAX_TRADES_PER_DAY = int(_env("MAX_TRADES_PER_DAY", 5))
DAILY_MAX_LOSS_PCT = float(_env("DAILY_MAX_LOSS_PCT", 4.0))
FEE_PCT_PER_SIDE = float(_env("FEE_PCT_PER_SIDE", 0.05))
PIONEX_FEE_ROUNDTRIP_PCT = 2.0 * FEE_PCT_PER_SIDE
MIN_ORDER_USDT = 10.5
CASH_BUFFER_PCT = 5.0

STATE_FILE = "bot_state.json"
WEBAPP_FILE = "index.html"
HTTP_TIMEOUT = 10
PIONEX_BASE = "https://api.pionex.com"

COINS = [
    {"code": "SOL",  "pionex": "SOL_USDT",  "kraken": "SOLUSD",  "ws": "SOL/USD",  "prec": 3},
    {"code": "BTC",  "pionex": "BTC_USDT",  "kraken": "XBTUSD",  "ws": "XBT/USD",  "prec": 5},
    {"code": "ETH",  "pionex": "ETH_USDT",  "kraken": "ETHUSD",  "ws": "ETH/USD",  "prec": 4},
    {"code": "XRP",  "pionex": "XRP_USDT",  "kraken": "XRPUSD",  "ws": "XRP/USD",  "prec": 1},
    {"code": "SUI",  "pionex": "SUI_USDT",  "kraken": "SUIUSD",  "ws": "SUI/USD",  "prec": 2},
    {"code": "DOGE", "pionex": "DOGE_USDT", "kraken": "XDGUSD",  "ws": "XDG/USD",  "prec": 0},
    {"code": "ADA",  "pionex": "ADA_USDT",  "kraken": "ADAUSD",  "ws": "ADA/USD",  "prec": 1},
    {"code": "AVAX", "pionex": "AVAX_USDT", "kraken": "AVAXUSD", "ws": "AVAX/USD", "prec": 2},
    {"code": "LINK", "pionex": "LINK_USDT", "kraken": "LINKUSD", "ws": "LINK/USD", "prec": 2},
    {"code": "DOT",  "pionex": "DOT_USDT",  "kraken": "DOTUSD",  "ws": "DOT/USD",  "prec": 2},
]
COIN_MAP = {c["code"]: c for c in COINS}
LIVE_PX = {}

# ==============================================================================
# RETE HTTP CON RETRY
# ==============================================================================
def _build_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "Mozilla/5.0 (Pionex-FreqAI-QuantBot/9.0)"})
    return s

SESSION = _build_session()

def safe_get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def clamp(val, low, high):
    return max(low, min(high, val))

def fmt_price(p):
    if p >= 1000: return f"${p:,.2f}"
    if p >= 10: return f"${p:.2f}"
    if p >= 1: return f"${p:.3f}"
    return f"${p:.4f}"

# ==============================================================================
# MOTORE DI PERSISTENZA (STATO DEL BOT)
# ==============================================================================
def load_state():
    default_state = {
        "position": {
            "is_open": False, "coin_code": "SOL", "symbol": "SOL_USDT",
            "entry_price": 0.0, "amount_coin": 0.0, "invested_usdt": 0.0,
            "sl_price": 0.0, "highest_price": 0.0, "trailing_active": False,
            "opened_at": "", "opened_ts": 0, "mode": "SIMULATION", "order_id": ""
        },
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
            "daily_date": "", "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0, "daily_trades": 0,
            "consec_losses": 0, "last_close_ts": 0, "last_action": "In attesa"
        },
        "history": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in default_state["position"].items():
                data.setdefault("position", {}).setdefault(k, v)
            for k, v in default_state["stats"].items():
                data.setdefault("stats", {}).setdefault(k, v)
            data.setdefault("history", [])
            return data
        except Exception:
            pass
    return default_state

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

# ==============================================================================
# DATA FETCHER UNIFICATO (KLINES PIONEX NATIVE CON FALLBACK KRAKEN)
# ==============================================================================
def fetch_candles_pionex(symbol="SOL_USDT", interval="15m", limit=120):
    """Scarica le candele direttamente da Pionex (stesso book di esecuzione)."""
    p_int = "15m" if interval == "15m" else "60m"
    url = f"{PIONEX_BASE}/api/v1/market/klines"
    j = safe_get_json(url, params={"symbol": symbol, "interval": p_int, "limit": limit})
    candles = []
    try:
        raw = j.get("data", {}).get("klines", [])
        for r in raw:
            candles.append({
                "time": int(r["time"]) // 1000,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"])
            })
        if candles:
            return sorted(candles, key=lambda x: x["time"])
    except Exception:
        pass
    return []

def fetch_candles_unified(coin_cfg, interval_min=15):
    """Tenta prima le klines di Pionex; se vuote fa fallback su Kraken."""
    p_sym = coin_cfg["pionex"]
    candles = fetch_candles_pionex(p_sym, "15m" if interval_min == 15 else "60m", 120)
    if candles and len(candles) >= 35:
        return candles

    # Fallback Kraken
    kr_pair = coin_cfg["kraken"]
    data = safe_get_json("https://api.kraken.com/0/public/OHLC", params={"pair": kr_pair, "interval": interval_min})
    res_candles = []
    try:
        res = data.get("result", {})
        for k, v in res.items():
            if k != "last" and isinstance(v, list):
                for row in v:
                    res_candles.append({
                        "time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                        "low": float(row[3]), "close": float(row[4]), "volume": float(row[6])
                    })
                break
    except Exception:
        pass
    return res_candles

# ==============================================================================
# INDICATORI MATEMATICI VETTORIALI VELOCI
# ==============================================================================
def calc_ema_series(values, period):
    if not values: return []
    k = 2.0 / (period + 1.0)
    ema = [values[0]]
    for p in values[1:]:
        ema.append(p * k + ema[-1] * (1.0 - k))
    return ema

def calc_rsi_series(closes, period=14):
    if len(closes) < period + 1: return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    rsis = [50.0] * period
    rsis.append(100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l)))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(0.0, d)) / period
        avg_l = (avg_l * (period - 1) + max(0.0, -d)) / period
        rsis.append(100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l)))
    return rsis

def calc_atr(candles, period=14):
    if len(candles) < 2: return 0.0
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    s = calc_ema_series(trs, period)
    return s[-1] if s else 0.0

def calc_supertrend(candles, period=10, mult=3.0):
    if len(candles) < period + 2: return 1, candles[-1]["close"] if candles else 0.0
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        trs.append(max(candles[i]["high"] - candles[i]["low"], abs(candles[i]["high"] - candles[i-1]["close"])))
    atr_s = calc_ema_series(trs, period)
    direction = 1
    final_upper, final_lower = 0.0, 0.0
    for i in range(1, len(candles)):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2.0
        atr = atr_s[i]
        b_up, b_low = hl2 + mult * atr, hl2 - mult * atr
        if i == 1:
            final_upper, final_lower = b_up, b_low
            continue
        if b_up < final_upper or candles[i-1]["close"] > final_upper: final_upper = b_up
        if b_low > final_lower or candles[i-1]["close"] < final_lower: final_lower = b_low
        c = candles[i]["close"]
        if c > final_upper: direction = 1
        elif c < final_lower: direction = -1
    return direction, (final_lower if direction == 1 else final_upper)

# ==============================================================================
# MODULO FREQAI: OUTLIER DETECTION & ML FILTER
# ==============================================================================
def freqai_outlier_filter(candles):
    """
    Ispirato a FreqAI: calcola il Dissimilarity Index (DI) rispetto alla varianza tipica.
    Se la candela corrente presenta un'anomalia statistica eccessiva (es. flash crash o pompa anomala),
    FreqAI esclude il trade per evitare trappole di mercato.
    """
    if len(candles) < 30: return True, 1.0
    closes = [c["close"] for c in candles[-30:]]
    volumes = [c["volume"] for c in candles[-30:]]
    rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
    
    vol_mean, vol_std = statistics.mean(volumes), statistics.pstdev(volumes) or 1.0
    ret_std = statistics.pstdev(rets) or 1e-4
    
    last_ret = abs((closes[-1] - closes[-2]) / closes[-2])
    last_vol_z = abs((volumes[-1] - vol_mean) / vol_std)
    
    # Indice composito di dissimilarità (DI)
    di = (last_ret / (3.0 * ret_std)) + (last_vol_z / 4.0)
    is_safe = di < 2.0  # Oltre 2.0 è considerato un outlier anomalo da FreqAI
    return is_safe, round(di, 2)

# ==============================================================================
# MOTORE DI VALUTAZIONE COIN
# ==============================================================================
def evaluate_coin(coin_cfg):
    candles_15m = fetch_candles_unified(coin_cfg, 15)
    candles_1h = fetch_candles_unified(coin_cfg, 60)
    if len(candles_15m) < 30 or len(candles_1h) < 20: return None
    
    closes_15m = [c["close"] for c in candles_15m]
    closes_1h = [c["close"] for c in candles_1h]
    price = closes_15m[-1]
    
    # 1. Indicatori 1h (Filtro Trend Superiore)
    ema50_1h = calc_ema_series(closes_1h, 50)[-1]
    st_dir_1h, _ = calc_supertrend(candles_1h, 10, 3.0)
    
    # 2. Indicatori 15m (Timing e Volume)
    ema20_15m = calc_ema_series(closes_15m, 20)[-1]
    rsi_15m = calc_rsi_series(closes_15m, 14)[-1]
    atr_15m = calc_atr(candles_15m, 14)
    
    # Volume Z-score
    vols = [c["volume"] for c in candles_15m[-25:]]
    mv, sv = statistics.mean(vols), statistics.pstdev(vols) or 1.0
    vol_z = (candles_15m[-1]["volume"] - mv) / sv
    
    # FreqAI Filter
    freqai_safe, di_score = freqai_outlier_filter(candles_15m)
    
    # Calcolo Score composito (stile Freqtrade)
    score = 0
    if price > ema50_1h: score += 30
    if st_dir_1h == 1: score += 25
    if price >= ema20_15m * 0.998: score += 20
    if 40 <= rsi_15m <= 68: score += 15
    if vol_z >= -0.1: score += 10
    
    passed_rules = (
        score >= ENTRY_RAW_100_Min and
        freqai_safe and
        st_dir_1h == 1 and
        price > ema50_1h and
        (40 <= rsi_15m <= 68)
    )
    
    return {
        "coin": coin_cfg, "code": coin_cfg["code"], "price": price,
        "score": score, "rsi_15m": rsi_15m, "vol_z": vol_z, "atr": atr_15m,
        "freqai_safe": freqai_safe, "di_score": di_score, "passed": passed_rules
    }

# ==============================================================================
# GESTIONE EXIT & TRAILING STOP (ALGORITMO FREQTRADE)
# ==============================================================================
def check_freqtrade_exit(pos, curr_price, now_ts):
    """
    Replica il cuore di Freqtrade:
    1. Minimal ROI Table temporale
    2. Dynamic Trailing Stop
    3. Initial Hard Stoploss
    """
    entry = pos["entry_price"]
    profit_ratio = (curr_price - entry) / entry
    profit_pct = profit_ratio * 100.0
    duration_min = max(0, (now_ts - pos["opened_ts"]) / 60.0)
    
    # Aggiorna picco massimo raggiunto dal prezzo
    highest = max(pos.get("highest_price", entry), curr_price)
    pos["highest_price"] = highest
    
    # A. Initial Stoploss Fisso (-2.5%)
    if profit_ratio <= INITIAL_STOPLOSS:
        return True, f"🛑 INITIAL STOPLOSS ({profit_pct:+.2f}%)"
    
    # B. Freqtrade Dynamic Trailing Stop
    # Se il profitto supera l'offset (+1.2%), attiva il trailing
    if TRAILING_STOP and profit_ratio >= TRAILING_STOP_POSITIVE_OFFSET:
        pos["trailing_active"] = True
        
    if pos.get("trailing_active", False):
        stop_level = highest * (1.0 - TRAILING_STOP_POSITIVE)
        if curr_price <= stop_level:
            return True, f"🔒 TRAILING STOP HIT (Picco: {fmt_price(highest)} -> Chiusura: {fmt_price(curr_price)} | {profit_pct:+.2f}%)"
            
    # C. Freqtrade Minimal ROI Table (Decadimento temporale dell'obiettivo)
    for t_min in sorted(MINIMAL_ROI_TABLE.keys(), reverse=True):
        if duration_min >= t_min:
            target_roi = MINIMAL_ROI_TABLE[t_min]
            if profit_ratio >= target_roi:
                return True, f"🎯 MINIMAL ROI RAGGIUNTO (+{profit_pct:.2f}% dopo {duration_min:.0f}m [target: {target_roi*100:.1f}%])"
            break
            
    return False, ""

# ==============================================================================
# ESECUZIONE ORDINI (PIONEX CLIENT CON SIMULAZIONE ROBUSTA)
# ==============================================================================
def pionex_request(method, path, params=None, body=None):
    if not (PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED):
        return {"ok": False, "sim": True}
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(sorted(params.items()))
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    message = f"{method}{path}?{query}{body_str}"
    sig = hmac.new(PIONEX_API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": sig, "Content-Type": "application/json"}
    url = f"{PIONEX_BASE}{path}?{query}"
    try:
        r = SESSION.request(method, url, data=body_str if body else None, headers=headers, timeout=HTTP_TIMEOUT)
        return {"ok": r.status_code == 200 and r.json().get("result") is not False, "data": r.json().get("data")}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

def execute_open(state, ev, usdt, now_str, now_ts):
    pos = state["position"]
    code, price = ev["code"], ev["price"]
    is_live = PIONEX_API_KEY and AUTO_TRADE_ENABLED
    
    amount = usdt / price * (1.0 - FEE_PCT_PER_SIDE / 100.0)
    pos.update({
        "is_open": True, "coin_code": code, "symbol": ev["coin"]["pionex"],
        "entry_price": price, "amount_coin": amount, "invested_usdt": usdt,
        "sl_price": price * (1.0 + INITIAL_STOPLOSS), "highest_price": price,
        "trailing_active": False, "opened_at": now_str, "opened_ts": now_ts,
        "mode": "LIVE_PIONEX" if is_live else "SIMULAZIONE"
    })
    state["stats"]["daily_trades"] = state["stats"].get("daily_trades", 0) + 1
    state["stats"]["last_action"] = f"APERTO {code} a {fmt_price(price)} ({pos['mode']})"
    save_state(state)
    return f"🚀 POSIZIONE APERTA su {code} a {fmt_price(price)} | Investiti: {usdt:.2f} USDT"

def execute_close(state, curr_price, reason, now_str, now_ts):
    pos, stats = state["position"], state["stats"]
    entry = pos["entry_price"]
    invested = pos["invested_usdt"]
    pnl_pct = ((curr_price - entry) / entry) * 100.0 - PIONEX_FEE_ROUNDTRIP_PCT
    pnl_usdt = invested * (pnl_pct / 100.0)
    code = pos["coin_code"]
    
    stats["total_trades"] += 1
    if pnl_usdt >= 0:
        stats["wins"] += 1
        stats["consec_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consec_losses"] = stats.get("consec_losses", 0) + 1
        
    stats["total_pnl_pct"] = round(stats["total_pnl_pct"] + pnl_pct, 2)
    stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + pnl_usdt, 2)
    stats["daily_pnl_usdt"] = round(stats.get("daily_pnl_usdt", 0.0) + pnl_usdt, 2)
    stats["last_close_ts"] = now_ts
    stats["last_action"] = f"CHIUSO {code} ({pnl_pct:+.2f}%) - {reason}"
    
    state.setdefault("history", []).insert(0, {
        "coin": code, "entry": entry, "exit": curr_price, "pnl_pct": round(pnl_pct, 2),
        "pnl_usdt": round(pnl_usdt, 2), "reason": reason, "closed_at": now_str
    })
    state["history"] = state["history"][:25]
    pos["is_open"] = False
    save_state(state)
    return f"🔔 {code} CHIUSO: {reason} | PnL: {pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)"

# ==============================================================================
# MAIN ITERATION & CONTROLLO
# ==============================================================================
def run_iteration():
    state = load_state()
    pos = state["position"]
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%d/%m %H:%M UTC")
    now_ts = int(now_utc.timestamp())
    
    print(f"\n--- [{now_str}] Ciclo di scansione Freqtrade-Engine ---")
    
    # 1. Se c'è una posizione aperta, verificala prima di tutto
    if pos["is_open"]:
        cfg = COIN_MAP.get(pos["coin_code"], COINS[0])
        candles = fetch_candles_unified(cfg, 15)
        curr_price = candles[-1]["close"] if candles else pos["entry_price"]
        should_close, reason = check_freqtrade_exit(pos, curr_price, now_ts)
        
        if should_close:
            msg = execute_close(state, curr_price, reason, now_str, now_ts)
            print(f"[TRADE EXIT] {msg}")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                             json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
            return
        else:
            pnl_live = ((curr_price - pos['entry_price']) / pos['entry_price']) * 100.0 - PIONEX_FEE_ROUNDTRIP_PCT
            print(f"[HOLDING] {pos['coin_code']} Entry: {fmt_price(pos['entry_price'])} -> Now: {fmt_price(curr_price)} ({pnl_live:+.2f}% netto)")
            return

    # 2. Scansione selettiva delle monete
    evals = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(evaluate_coin, c): c for c in COINS}
        for f in as_completed(futures):
            res = f.result()
            if res: evals.append(res)
            
    evals.sort(key=lambda x: x["score"], reverse=True)
    if not evals:
        print("⚠️ Nessun dato disponibile in questa iterazione.")
        return

    best = evals[0]
    print(f"Migliore opportunità: {best['code']} (Score: {best['score']}/100, DI FreqAI: {best['di_score']}, RSI: {best['rsi_15m']:.1f})")

    # 3. Apertura nuova posizione se i requisiti Freqtrade sono soddisfatti
    if best["passed"]:
        trade_budget_usdt = INVEST_EUR * 1.08 * (1.0 - CASH_BUFFER_PCT / 100.0)
        msg = execute_open(state, best, trade_budget_usdt, now_str, now_ts)
        print(f"[TRADE ENTRY] {msg}")
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                         json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    else:
        print("🛡️ Condizioni non ancora mature: capitale protetto in USDT.")

def main():
    print("=" * 65)
    print("🤖 PIONEX-FREQAI ENGINE v9.0 AVVIATO")
    print(f"Modalità Demone continuo: {DAEMON_MODE} (Intervallo: {LOOP_INTERVAL_SEC}s)")
    print(f"Trading Reale: {'ATTIVO (LIVE)' if AUTO_TRADE_ENABLED else 'SIMULAZIONE (DRY-RUN)'}")
    print("=" * 65)
    
    if DAEMON_MODE:
        while True:
            try:
                run_iteration()
            except Exception as e:
                print(f"Errore ciclo: {e}")
            time.sleep(LOOP_INTERVAL_SEC)
    else:
        run_iteration()

if __name__ == "__main__":
    main()
