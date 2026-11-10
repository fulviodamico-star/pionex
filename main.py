import os
import re
import sys
import time
import math
import json
import hmac
import hashlib
import random
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ==========================================
# CONFIGURAZIONE AMBIENTE E PARAMETRI BASE
# ==========================================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or "").strip()

AUTO_TRADE_ENABLED = (os.getenv("AUTO_TRADE_ENABLED") or "false").strip().lower() == "true"

INVEST_EUR = float(os.getenv("INVEST_EUR") or "50")
PIONEX_FEE = 0.0010          # 0.10% maker/taker media su Pionex
SLIPPAGE_BUFFER = 0.0006     # 0.06% margine microstruttura

# ==========================================
# PARAMETRI RISK & BUDGET MANAGER (SKILLS #2 & #6)
# ==========================================
MAX_LOSS_PCT_PER_TRADE = 3.6  # Max perdita su singolo trade (% del budget allocato)
DAILY_MAX_LOSS_PCT = 6.0      # Circuit breaker: perdita max giornaliera (%)
MAX_TRADES_PER_DAY = 3        # Anti-overtrading: max aperture giornaliere
MIN_REWARD_RISK_R = 1.35      # Rapporto minimo Reward/Risk (comprensivo di Grid Yield)
CASH_BUFFER_PCT = 5.0         # Riserva liquidità minima di sicurezza

ACTIVE_LOWER = float(os.getenv("ACTIVE_LOWER") or "0")
ACTIVE_UPPER = float(os.getenv("ACTIVE_UPPER") or "0")
ACTIVE_GRIDS = int(float(os.getenv("ACTIVE_GRIDS") or "0"))

STATE_FILE = "bot_state.json"
WEBAPP_FILE = "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/xml, */*"
}

# ==========================================
# UNIVERSO MULTI-COIN (TOP 10 LIQUID CRYPTO)
# ==========================================
COIN_UNIVERSE = [
    {"name": "SOL",  "kraken": "SOLUSD",  "ws": "SOL/USD",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT"},
    {"name": "BTC",  "kraken": "XBTUSD",  "ws": "XBT/USD",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT"},
    {"name": "ETH",  "kraken": "ETHUSD",  "ws": "ETH/USD",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT"},
    {"name": "XRP",  "kraken": "XRPUSD",  "ws": "XRP/USD",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT"},
    {"name": "SUI",  "kraken": "SUIUSD",  "ws": "SUI/USD",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT"},
    {"name": "DOGE", "kraken": "XDGUSD",  "ws": "XDG/USD",  "futures": "PF_DOGEUSD", "pionex": "DOGE_USDT"},
    {"name": "ADA",  "kraken": "ADAUSD",  "ws": "ADA/USD",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT"},
    {"name": "AVAX", "kraken": "AVAXUSD", "ws": "AVAX/USD", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT"},
    {"name": "LINK", "kraken": "LINKUSD", "ws": "LINK/USD", "futures": "PF_LINKUSD", "pionex": "LINK_USDT"},
    {"name": "DOT",  "kraken": "DOTUSD",  "ws": "DOT/USD",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT"}
]

# ==========================================
# CALENDARIO FESTIVITÀ USA & NYSE BELL (SKILL #1)
# ==========================================
NYSE_HOLIDAYS_2026_2027 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"
}


def check_nyse_market_bell():
    now_utc = datetime.now(timezone.utc)
    ny_offset = timedelta(hours=-4) if 3 <= now_utc.month <= 10 else timedelta(hours=-5)
    ny_time = now_utc + ny_offset
    date_str = ny_time.strftime("%Y-%m-%d")

    if ny_time.weekday() >= 5 or date_str in NYSE_HOLIDAYS_2026_2027:
        return {"status": "CHIUSO (Weekend/Festivo USA)", "is_bell_window": False}

    minutes_now = ny_time.hour * 60 + ny_time.minute
    open_min = 9 * 60 + 30
    close_min = 16 * 60

    if abs(minutes_now - open_min) <= 35:
        return {"status": "🔔 OPENING BELL WALL STREET (Alta Volatilità ETF)", "is_bell_window": True}
    elif open_min < minutes_now < close_min:
        return {"status": "APERTO (Sessione USA Attiva)", "is_bell_window": False}
    else:
        return {"status": "AFTER-HOURS / SESSIONE ASIATICA-EUROPEA", "is_bell_window": False}


# ==========================================
# FORMATTAZIONE DINAMICA PREZZI MULTI-COIN
# ==========================================
def price_decimals(price):
    p = abs(float(price))
    if p == 0:
        return 2
    if p >= 1000:
        return 1
    if p >= 10:
        return 2
    if p >= 1:
        return 3
    return 4


def round_p(price, ref_price=None):
    ref = ref_price if ref_price is not None else price
    return round(float(price), price_decimals(ref))


def fmt_p(price, ref_price=None):
    ref = ref_price if ref_price is not None else price
    d = price_decimals(ref)
    return f"{float(price):.{d}f}"


# ==========================================
# GESTIONE STATO PERSISTENTE (BOT MEMORY)
# ==========================================
def load_bot_state():
    default_state = {
        "active": False,
        "mode": "PAPER",
        "coin": "SOL",
        "symbol": "SOL_USDT",
        "entry_price": 0.0,
        "lower": 0.0,
        "upper": 0.0,
        "stop_loss": 0.0,
        "initial_stop_loss": 0.0,
        "one_r_usd": 0.0,
        "level_1r": 0.0,
        "level_2r": 0.0,
        "alert_1r_sent": False,
        "alert_2r_sent": False,
        "trailing_active": False,
        "grids": 0,
        "step_usd": 0.0,
        "net_step_pct": 0.0,
        "qty": 0.0,
        "invested_usdt": 0.0,
        "opened_at": "",
        "peak_price": 0.0,
        "Accumulated_grid_usdt": 0.0,
        "grid_fills_count": 0,
        "last_sim_price": 0.0,
        "total_historical_pnl_usdt": 0.0,
        "closed_trades_count": 0,
        "daily_date": "",
        "daily_realized_pnl_usdt": 0.0,
        "daily_trades_opened": 0,
        "trade_history": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
        except Exception as e:
            print(f"[WARN] Impossibile leggere {STATE_FILE}: {e}")
    return default_state


def save_bot_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Impossibile salvare {STATE_FILE}: {e}")


# ==========================================
# CONNETTORE API PIONEX (HMAC-SHA256)
# ==========================================
def pionex_request(method, path, params=None, body=None):
    if not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return {"result": False, "message": "API Key o Secret Pionex mancanti"}

    ts = str(int(time.time() * 1000))
    q = dict(params) if params else {}
    q["timestamp"] = ts

    sorted_items = sorted(q.items(), key=lambda x: x[0])
    query_str = urllib.parse.urlencode(sorted_items)
    path_url = f"{path}?{query_str}"

    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    msg = f"{method.upper()}{path_url}{body_str}"
    signature = hmac.new(PIONEX_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
        "Content-Type": "application/json",
        "User-Agent": "NeuralQuantGridBot/7.1"
    }
    url = f"https://api.pionex.com{path_url}"
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=12)
        elif method.upper() == "POST":
            r = requests.post(url, headers=headers, data=body_str, timeout=12)
        elif method.upper() == "DELETE":
            r = requests.delete(url, headers=headers, data=body_str, timeout=12)
        else:
            return {"result": False, "message": "Metodo HTTP non supportato"}
        return r.json()
    except Exception as e:
        return {"result": False, "message": str(e)}


def execute_autonomous_buy(symbol, amount_usdt, current_price):
    if not AUTO_TRADE_ENABLED or not PIONEX_API_KEY or not PIONEX_API_SECRET:
        sim_qty = round((amount_usdt * (1.0 - PIONEX_FEE)) / current_price, 5)
        return True, "PAPER", sim_qty, f"Simulazione Paper Trading (Acquistati virtualmente {sim_qty} {symbol.split('_')[0]})"

    body = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "amount": f"{amount_usdt:.2f}"
    }
    res = pionex_request("POST", "/api/v1/trade/order", body=body)
    if res.get("result") is True:
        order_id = res.get("data", {}).get("orderId", "OK")
        est_qty = round((amount_usdt * (1.0 - PIONEX_FEE)) / current_price, 5)
        return True, "LIVE_PIONEX", est_qty, f"Ordine Market BUY eseguito su Pionex (ID: {order_id})"
    else:
        err_msg = res.get("message", "Errore sconosciuto API Pionex")
        sim_qty = round((amount_usdt * (1.0 - PIONEX_FEE)) / current_price, 5)
        return True, "PAPER_FALLBACK", sim_qty, f"API Pionex non attiva ({err_msg}) -> Avviato in Paper Mode"


def execute_autonomous_sell(symbol, qty):
    if not AUTO_TRADE_ENABLED or not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return True, f"Simulazione Paper Trading (Venduti virtualmente {qty} {symbol.split('_')[0]})"

    qty_decimals = 4 if qty < 10 else 2
    body = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "size": f"{qty:.{qty_decimals}f}"
    }
    res = pionex_request("POST", "/api/v1/trade/order", body=body)
    if res.get("result") is True:
        order_id = res.get("data", {}).get("orderId", "OK")
        return True, f"Ordine Market SELL eseguito su Pionex (ID: {order_id})"
    else:
        err_msg = res.get("message", "Errore API Pionex")
        return True, f"Chiusura registrata in Paper Mode (Info API: {err_msg})"


# ==========================================
# UTILITÀ MATEMATICHE E SERIE STORICHE
# ==========================================
def clamp(val, low, high):
    return max(low, min(high, val))


def mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def stdev(vals):
    if len(vals) < 2:
        return 0.0
    m = mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def build_4h_from_1h(h1, l1, c1):
    h4, l4, c4 = [], [], []
    n = len(c1)
    for i in range(0, n - 3, 4):
        h4.append(max(h1[i:i + 4]))
        l4.append(min(l1[i:i + 4]))
        c4.append(c1[i + 3])
    if not c4:
        return h1, l1, c1
    return h4, l4, c4


def rsi_series(closes, period=14):
    n = period
    if len(closes) <= n:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n

    rs = avg_gain / avg_loss if avg_loss > 1e-12 else 100.0
    first_rsi = 100.0 - (100.0 / (1.0 + rs))
    out = [50.0] * n + [first_rsi]

    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        if avg_loss < 1e-12:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def stoch_rsi_k(closes, rsi_period=14, stoch_period=14, smooth_k=3):
    r = rsi_series(closes, rsi_period)
    if len(r) < stoch_period + smooth_k:
        return 50.0
    raw_k = []
    for i in range(stoch_period - 1, len(r)):
        w = r[i - stoch_period + 1:i + 1]
        low_r, high_r = min(w), max(w)
        if high_r - low_r < 1e-9:
            raw_k.append(50.0)
        else:
            raw_k.append((r[i] - low_r) / (high_r - low_r) * 100.0)
    return mean(raw_k[-smooth_k:])


def macd_metrics(closes):
    if len(closes) < 35:
        return 0.0, 0.0, 0.0
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    sig_line = ema_series(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    slope = hist[-1] - hist[-2] if len(hist) >= 2 else 0.0
    return macd_line[-1], hist[-1], slope


def atr_series(highs, lows, closes, period=14):
    if len(closes) < 2:
        return [0.0] * len(closes)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    if len(trs) < period:
        return [mean(trs)] * len(trs)
    out = [mean(trs[:period])] * period
    prev = out[-1]
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out.append(prev)
    return out


def adx_di_metrics(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 2:
        return 20.0, 20.0, 20.0
    tr_list, pdm_list, mdm_list = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
        pdm_list.append(pdm)
        mdm_list.append(mdm)

    n = period
    sm_tr = sum(tr_list[:n])
    sm_pdm = sum(pdm_list[:n])
    sm_mdm = sum(mdm_list[:n])

    dx_list = []
    plus_di, minus_di = 20.0, 20.0
    for i in range(n, len(tr_list)):
        sm_tr = sm_tr - (sm_tr / n) + tr_list[i]
        sm_pdm = sm_pdm - (sm_pdm / n) + pdm_list[i]
        sm_mdm = sm_mdm - (sm_mdm / n) + mdm_list[i]
        if sm_tr <= 1e-12:
            continue
        plus_di = 100.0 * (sm_pdm / sm_tr)
        minus_di = 100.0 * (sm_mdm / sm_tr)
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denom if denom > 1e-12 else 0.0
        dx_list.append(dx)

    if not dx_list:
        return 20.0, plus_di, minus_di
    adx = mean(dx_list[:n])
    for d in dx_list[n:]:
        adx = (adx * (n - 1) + d) / n
    return adx, plus_di, minus_di


def bollinger_keltner_metrics(highs, lows, closes, period=20):
    if len(closes) < period:
        c = closes[-1] if closes else 1.0
        return c * 0.95, c, c * 1.05, 0.5, 0.05, False
    w = closes[-period:]
    mid = mean(w)
    sd = stdev(w)
    bb_up = mid + 2.0 * sd
    bb_low = mid - 2.0 * sd
    pct_b = (closes[-1] - bb_low) / (bb_up - bb_low) if (bb_up - bb_low) > 1e-12 else 0.5
    bw = (bb_up - bb_low) / mid if mid > 1e-12 else 0.0

    atrs = atr_series(highs, lows, closes, period)
    kc_up = mid + 1.5 * atrs[-1]
    kc_low = mid - 1.5 * atrs[-1]
    squeeze_on = (bb_low > kc_low) and (bb_up < kc_up)
    return bb_low, mid, bb_up, pct_b, bw, squeeze_on


# ==========================================
# ECONOMETRIA E MICROSTRUTTURA AVANZATA
# ==========================================
def calculate_hurst_exponent(closes):
    if len(closes) < 100:
        return 0.50
    log_p = [math.log(max(c, 1e-8)) for c in closes[-240:]]
    lags = [2, 4, 8, 16, 32, 48]
    x_vals, y_vals = [], []
    for lag in lags:
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        s = stdev(diffs)
        if s > 1e-12:
            x_vals.append(math.log(lag))
            y_vals.append(math.log(s))
    if len(x_vals) < 3:
        return 0.50
    mx, my = mean(x_vals), mean(y_vals)
    num = sum((x - mx) * (y - my) for x, y in zip(x_vals, y_vals))
    den = sum((x - mx) ** 2 for x in x_vals)
    h = num / den if den > 1e-12 else 0.50
    return clamp(h, 0.20, 0.85)


def calculate_variance_ratio(closes, lag=6):
    if len(closes) < lag * 12:
        return 1.0
    log_p = [math.log(max(c, 1e-8)) for c in closes[-240:]]
    r1 = [log_p[i] - log_p[i - 1] for i in range(1, len(log_p))]
    rk = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
    var1 = stdev(r1) ** 2
    vark = stdev(rk) ** 2
    if var1 <= 1e-15:
        return 1.0
    vr = vark / (lag * var1)
    return clamp(vr, 0.30, 2.20)


def calculate_vwap_24h(highs, lows, closes, volumes):
    n = min(24, len(closes))
    if n == 0:
        return closes[-1] if closes else 0.0
    num, den = 0.0, 0.0
    for i in range(-n, 0):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        v = volumes[i]
        num += tp * v
        den += v
    return num / den if den > 1e-12 else closes[-1]


# ==========================================
# RETE NEURALE (MLP) + LORENTZIAN k-NN
# ==========================================
def extract_ml_feature_matrix(highs, lows, closes, volumes):
    r_series = rsi_series(closes, 14)
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    atrs = atr_series(highs, lows, closes, 14)

    features = []
    for i in range(len(closes)):
        if i < 55:
            features.append([0.0] * 6)
            continue
        c = closes[i]
        atr_v = max(atrs[i], c * 0.002)
        f1 = (r_series[i] - 50.0) / 25.0
        f2 = clamp((c - e20[i]) / (2.0 * atr_v), -2.5, 2.5)
        f3 = clamp((e20[i] - e50[i]) / (2.5 * atr_v), -2.5, 2.5)
        ret4 = (c / closes[i - 4] - 1.0) if closes[i - 4] > 0 else 0.0
        f4 = clamp(ret4 / 0.03, -2.5, 2.5)
        vol_ma = mean(volumes[i - 20:i])
        f5 = clamp((volumes[i] / vol_ma - 1.0) if vol_ma > 0 else 0.0, -2.0, 2.5)
        w20 = closes[i - 19:i + 1]
        sd20 = stdev(w20)
        f6 = clamp((c - mean(w20)) / (2.0 * sd20) if sd20 > 1e-9 else 0.0, -2.5, 2.5)
        features.append([f1, f2, f3, f4, f5, f6])

    return features, atrs


def run_neural_and_lorentzian_ai(highs, lows, closes, volumes):
    if len(closes) < 150:
        return {
            "ensemble_prob": 0.50,
            "mlp_prob": 0.50,
            "knn_prob": 0.50,
            "val_accuracy": 50.0,
            "samples": 0,
            "signal": "NEUTRALE"
        }

    features, atrs = extract_ml_feature_matrix(highs, lows, closes, volumes)
    horizon = 8
    X_data, Y_data = [], []

    for i in range(55, len(closes) - horizon):
        c_now = closes[i]
        atr_now = max(atrs[i], c_now * 0.003)
        future_high = max(highs[i + 1:i + 1 + horizon])
        future_low = min(lows[i + 1:i + 1 + horizon])
        future_close = closes[i + horizon]

        favorable_grid_move = (
            (future_low > c_now - 1.45 * atr_now) and
            (future_high >= c_now + 0.45 * atr_now or future_close >= c_now - 0.25 * atr_now)
        )
        Y_data.append(1.0 if favorable_grid_move else 0.0)
        X_data.append(features[i])

    current_x = features[-1]
    n_samples = len(X_data)
    if n_samples < 60:
        return {
            "ensemble_prob": 0.50,
            "mlp_prob": 0.50,
            "knn_prob": 0.50,
            "val_accuracy": 50.0,
            "samples": n_samples,
            "signal": "NEUTRALE"
        }

    distances = []
    for x_hist, y_hist in zip(X_data, Y_data):
        d = sum(math.log(1.0 + abs(a - b)) for a, b in zip(current_x, x_hist))
        distances.append((d, y_hist))
    distances.sort(key=lambda t: t[0])
    k_neighbors = distances[:15]
    weight_sum, weighted_y = 0.0, 0.0
    for d, y_val in k_neighbors:
        w = 1.0 / (d + 0.15)
        weight_sum += w
        weighted_y += w * y_val
    knn_prob = weighted_y / weight_sum if weight_sum > 0 else 0.50

    rng = random.Random(42)
    n_in, n_hid = 6, 5
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(n_in)] for _ in range(n_hid)]
    b1 = [0.0 for _ in range(n_hid)]
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(n_hid)]
    b2 = 0.0

    def sigmoid(z):
        z = clamp(z, -12.0, 12.0)
        return 1.0 / (1.0 + math.exp(-z))

    def forward_mlp(x_vec):
        h = []
        for j in range(n_hid):
            act = sum(W1[j][i] * x_vec[i] for i in range(n_in)) + b1[j]
            h.append(math.tanh(clamp(act, -8.0, 8.0)))
        out_lin = sum(W2[j] * h[j] for j in range(n_hid)) + b2
        return h, sigmoid(out_lin)

    split_idx = max(40, int(n_samples * 0.80))
    train_X, train_Y = X_data[:split_idx], Y_data[:split_idx]
    val_X, val_Y = X_data[split_idx:], Y_data[split_idx:]

    lr = 0.045
    l2_reg = 0.0008
    epochs = 30

    for _ in range(epochs):
        for x_vec, y_true in zip(train_X, train_Y):
            h, y_hat = forward_mlp(x_vec)
            dz2 = y_hat - y_true
            dW2 = [dz2 * h[j] + l2_reg * W2[j] for j in range(n_hid)]
            db2 = dz2
            dh = [dz2 * W2[j] for j in range(n_hid)]
            dz1 = [dh[j] * (1.0 - h[j] ** 2) for j in range(n_hid)]

            for j in range(n_hid):
                W2[j] -= lr * dW2[j]
                b1[j] -= lr * dz1[j]
                for i in range(n_in):
                    W1[j][i] -= lr * (dz1[j] * x_vec[i] + l2_reg * W1[j][i])
            b2 -= lr * db2

    if val_X:
        correct = 0
        for xv, yv in zip(val_X, val_Y):
            _, p_val = forward_mlp(xv)
            pred = 1.0 if p_val >= 0.50 else 0.0
            if pred == yv:
                correct += 1
        val_acc = (correct / len(val_X)) * 100.0
    else:
        val_acc = 55.0

    _, mlp_prob = forward_mlp(current_x)
    ensemble_prob = clamp(0.55 * mlp_prob + 0.45 * knn_prob, 0.05, 0.95)

    if ensemble_prob >= 0.65:
        sig = "BULLISH / RANGE FAVOREVOLE FORTE"
    elif ensemble_prob >= 0.54:
        sig = "FAVOREVOLE"
    elif ensemble_prob >= 0.46:
        sig = "NEUTRALE"
    else:
        sig = "RIBASSISTA / RISCHIO BREAKDOWN"

    return {
        "ensemble_prob": round(ensemble_prob, 4),
        "mlp_prob": round(mlp_prob, 4),
        "knn_prob": round(knn_prob, 4),
        "val_accuracy": round(val_acc, 1),
        "samples": n_samples,
        "signal": sig
    }


# ==========================================
# RACCOLTA DATI MULTI-FONTE (CON RETRY ANTI-BLOCCO)
# ==========================================
def fetch_eur_usd():
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=EURUSD", headers=HEADERS, timeout=10)
        d = r.json().get("result", {})
        for k in d:
            return float(d[k]["c"][0])
    except Exception:
        pass
    return 1.08


def fetch_kraken_ohlc(pair, interval=60):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            res = r.json().get("result", {})
            for k, v in res.items():
                if k != "last" and isinstance(v, list) and len(v) > 10:
                    return v
        except Exception:
            pass
        time.sleep(0.45 * (attempt + 1))
    return []


def fetch_kraken_orderbook_depth(pair, count=50):
    url = f"https://api.kraken.com/0/public/Depth?pair={pair}&count={count}"
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            res = r.json().get("result", {})
            for k, v in res.items():
                bids = v.get("bids", [])
                asks = v.get("asks", [])
                if not bids or not asks:
                    break
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
                mid = (best_bid + best_ask) / 2.0
                spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 5.0

                bid_vol_weighted = 0.0
                for b in bids:
                    p, q = float(b[0]), float(b[1])
                    dist = max(0.0005, abs(mid - p) / mid)
                    bid_vol_weighted += (p * q) / (1.0 + dist * 100.0)

                ask_vol_weighted = 0.0
                for a in asks:
                    p, q = float(a[0]), float(a[1])
                    dist = max(0.0005, abs(p - mid) / mid)
                    ask_vol_weighted += (p * q) / (1.0 + dist * 100.0)

                tot = bid_vol_weighted + ask_vol_weighted
                imbalance = (bid_vol_weighted - ask_vol_weighted) / tot if tot > 0 else 0.0
                ratio = bid_vol_weighted / ask_vol_weighted if ask_vol_weighted > 0 else 1.0
                return {
                    "imbalance": round(imbalance, 3),
                    "bid_ask_ratio": round(ratio, 2),
                    "spread_bps": round(spread_bps, 2),
                    "ok": True
                }
        except Exception:
            time.sleep(0.3)
    return {"imbalance": 0.0, "bid_ask_ratio": 1.0, "spread_bps": 2.0, "ok": False}


def fetch_all_kraken_futures_map():
    out = {}
    try:
        r = requests.get("https://futures.kraken.com/derivatives/api/v3/tickers", headers=HEADERS, timeout=10)
        tickers = r.json().get("tickers", [])
        for t in tickers:
            sym = t.get("symbol", "")
            if sym.startswith("PF_"):
                fr_raw = float(t.get("fundingRate", 0.0))
                mark_p = float(t.get("markPrice", 1.0) or 1.0)
                if abs(fr_raw) > 0.005 and mark_p > 1.0:
                    fr_pct_8h = (fr_raw / mark_p) * 100.0
                else:
                    fr_pct_8h = fr_raw * 100.0
                oi = float(t.get("openInterest", 0.0))
                out[sym] = {
                    "funding_8h_pct": round(fr_pct_8h, 4),
                    "open_interest": round(oi, 1),
                    "ok": True
                }
    except Exception:
        pass
    return out


def fetch_fear_and_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", headers=HEADERS, timeout=10)
        data = r.json().get("data", [])
        if data:
            return int(data[0].get("value", 50)), data[0].get("value_classification", "Neutral")
    except Exception:
        pass
    return 50, "Neutral"


def fetch_usd_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    upcoming = []
    source_status = "ForexFactory Live"

    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            events = r.json()
            for ev in events:
                if ev.get("country") != "USD":
                    continue
                impact = ev.get("impact", "")
                if impact not in ("High", "Medium"):
                    continue
                date_str = ev.get("date", "")
                try:
                    ev_dt = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                    diff_h = (ev_dt - now_utc).total_seconds() / 3600.0
                    if -1.0 <= diff_h <= 48.0:
                        upcoming.append({
                            "title": ev.get("title", "USD Event"),
                            "impact": impact,
                            "hours_away": round(diff_h, 1),
                            "time_utc": ev_dt.strftime("%d/%m %H:%M UTC")
                        })
                except Exception:
                    continue
            upcoming.sort(key=lambda x: x["hours_away"])
            return upcoming, source_status
    except Exception:
        pass

    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", headers=HEADERS, timeout=10)
        if r.status_code == 200 and r.text.strip().startswith("<?xml"):
            root = ET.fromstring(r.text)
            for ev in root.findall("event"):
                country = (ev.findtext("country") or "").strip()
                impact = (ev.findtext("impact") or "").strip()
                if country != "USD" or impact not in ("High", "Medium"):
                    continue
                d_str = (ev.findtext("date") or "").strip()
                t_str = (ev.findtext("time") or "").strip()
                title = (ev.findtext("title") or "USD Event").strip()
                if not d_str or not t_str or "All Day" in t_str or "Tentative" in t_str:
                    continue
                try:
                    dt_naive = datetime.strptime(f"{d_str} {t_str}", "%m-%d-%Y %I:%M%p")
                    ev_dt = dt_naive.replace(tzinfo=timezone.utc) + timedelta(hours=4)
                    diff_h = (ev_dt - now_utc).total_seconds() / 3600.0
                    if -1.0 <= diff_h <= 48.0:
                        upcoming.append({
                            "title": title,
                            "impact": impact,
                            "hours_away": round(diff_h, 1),
                            "time_utc": ev_dt.strftime("%d/%m %H:%M UTC")
                        })
                except Exception:
                    continue
            upcoming.sort(key=lambda x: x["hours_away"])
            return upcoming, "XML Live"
    except Exception:
        pass

    return [], "Nessun evento USD imminente"


# ==========================================
# CALCOLO LIVELLI STRUTTURALI (SUPPORTI/RESISTENZE)
# ==========================================
def find_structural_levels(highs, lows, closes, atr_val):
    current = closes[-1]
    supports, resistances = [], []

    for i in range(4, len(closes) - 4):
        if lows[i] <= min(lows[i - 4:i]) and lows[i] <= min(lows[i + 1:i + 5]):
            if lows[i] < current:
                supports.append(lows[i])
        if highs[i] >= max(highs[i - 4:i]) and highs[i] >= max(highs[i + 1:i + 5]):
            if highs[i] > current:
                resistances.append(highs[i])

    def cluster_levels(levels, threshold):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - mean(clusters[-1])) <= threshold:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        scored = [(mean(c), len(c)) for c in clusters]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in scored]

    sup_clustered = cluster_levels(supports, atr_val * 0.6)
    res_clustered = cluster_levels(resistances, atr_val * 0.6)

    near_sups = sorted([s for s in sup_clustered if current - 6.0 * atr_val <= s <= current - 1.2 * atr_val])
    near_ress = sorted([r for r in res_clustered if current + 1.2 * atr_val <= r <= current + 6.0 * atr_val])

    best_sup = near_sups[0] if near_sups else (current - 3.0 * atr_val)
    best_res = near_ress[-1] if near_ress else (current + 3.0 * atr_val)
    return best_sup, best_res


# ==========================================
# SKILL #3 & #8: CHART SCANNER & ENTRY TIMER (15m/1H)
# ==========================================
def run_chart_scanner_and_entry_timer(h15, l15, c15, v15, h1, l1, c1, v1, lower_bound, upper_bound):
    price = c1[-1]
    rsi_1h = rsi_series(c1, 14)[-1]
    stoch_15m = stoch_rsi_k(c15, 14, 14, 3) if len(c15) >= 35 else 50.0
    e20_1h = ema_series(c1, 20)[-1]
    e50_1h = ema_series(c1, 50)[-1]
    _, _, macd_slope = macd_metrics(c1)

    vol_avg_15m = mean(v15[-20:]) if len(v15) >= 20 else (v15[-1] if v15 else 1.0)
    vol_ratio_15m = v15[-2] / vol_avg_15m if (len(v15) >= 2 and vol_avg_15m > 0) else 1.0

    pos_in_range = (price - lower_bound) / (upper_bound - lower_bound) if (upper_bound > lower_bound) else 0.5
    if pos_in_range <= 0.38 and (rsi_1h < 45 or stoch_15m < 35):
        pattern = "Oversold Support Bounce (Rimbalzo su Supporto)"
        pattern_bonus = 3
    elif price > e20_1h >= e50_1h and macd_slope >= 0:
        pattern = "Bull Flag / Trend Pullback (Continuazione)"
        pattern_bonus = 3
    elif len(c1) >= 2 and c1[-1] > e20_1h and c1[-2] <= e20_1h:
        pattern = "EMA20 Bullish Reclaim (Recupero Media Rapida)"
        pattern_bonus = 3
    elif 0.22 <= pos_in_range <= 0.72:
        pattern = "Mean-Reverting Grid Equilibrium (Oscillazione Centrale)"
        pattern_bonus = 2
    else:
        pattern = "Estensione Laterale / No Setup Primario"
        pattern_bonus = 0

    if len(c15) >= 5:
        closed_bar_close = c15[-2]
        closed_bar_open = c15[-3]
        prev_bar_high = h15[-3]
        prev_bar_low = l15[-3]
        micro_range_low = min(l15[-5:-1])

        bar_green = closed_bar_close >= closed_bar_open
        higher_low = l15[-2] >= micro_range_low * 0.9985
        breakout_micro = closed_bar_close > prev_bar_high
        not_falling_knife = not (closed_bar_close < prev_bar_low and v15[-2] > vol_avg_15m * 1.35)

        if breakout_micro and vol_ratio_15m >= 0.85:
            triggered = True
            timer_state = "TRIGGERED ✅ (Breakout Micro-Range 15m Confermato)"
        elif bar_green and higher_low and not_falling_knife and stoch_15m < 85:
            triggered = True
            timer_state = "TRIGGERED ✅ (Candela 15m Verde + Higher Low)"
        elif not_falling_knife and 36 <= rsi_1h <= 62 and macd_slope >= 0:
            triggered = True
            timer_state = "TRIGGERED ✅ (Equilibrio Dinamico + MACD Positivo)"
        else:
            triggered = False
            timer_state = "WAITING ⏳ (Attesa Chiusura 15m Rialzista)"
    else:
        triggered = True
        timer_state = "TRIGGERED ✅ (Conferma Struttura 1H)"

    return {
        "pattern": pattern,
        "pattern_bonus": pattern_bonus,
        "triggered": triggered,
        "timer_state": timer_state,
        "vol_ratio_15m": round(vol_ratio_15m, 2)
    }


# ==========================================
# SKILL #4: EXIT PLANNER (1R, 2R, RR RATIO, SPREAD CHECK)
# ==========================================
def run_exit_planner(entry_price, lower_bound, upper_bound, stop_loss, step_usd, net_step_pct, atr_1h, spread_bps):
    one_r_usd = max(entry_price - stop_loss, entry_price * 0.008)
    one_r_pct = (one_r_usd / entry_price) * 100.0 if entry_price > 0 else 1.5

    upside_to_upper_usd = max(0.0, upper_bound - entry_price)
    est_daily_fills = max(1.5, (atr_1h * 24.0 * 0.65) / step_usd) if step_usd > 0 else 2.0
    expected_grid_yield_usd = entry_price * (net_step_pct / 100.0) * min(est_daily_fills * 1.5, 8.0)

    total_reward_usd = upside_to_upper_usd + expected_grid_yield_usd
    rr_ratio = total_reward_usd / one_r_usd if one_r_usd > 0 else 1.0

    level_1r = round_p(entry_price + one_r_usd, entry_price)
    level_2r = round_p(entry_price + 2.0 * one_r_usd, entry_price)

    spread_usd = entry_price * (spread_bps / 10000.0)
    spread_ok = one_r_usd >= (spread_usd * 5.0)

    return {
        "one_r_usd": round_p(one_r_usd, entry_price),
        "one_r_pct": round(one_r_pct, 2),
        "level_1r": level_1r,
        "level_2r": level_2r,
        "rr_ratio": round(rr_ratio, 2),
        "spread_ok": spread_ok
    }


# ==========================================
# SKILL #9: TIME MACHINE (1.500 MONTE CARLO SIMS + REPLAY)
# ==========================================
def run_time_machine(highs, lows, closes, entry_price, lower_bound, upper_bound, stop_loss, target_price, horizon_h=24, n_sims=1500):
    if len(closes) < 80 or entry_price <= 0:
        return {
            "target_first_prob": 40.0,
            "stop_first_prob": 20.0,
            "in_range_prob": 40.0,
            "win_or_grid_prob": 80.0,
            "expected_r": 0.45,
            "hist_win_rate": 65.0,
            "sims_count": 0
        }

    up_target_pct = (target_price - entry_price) / entry_price
    down_stop_pct = (stop_loss - entry_price) / entry_price

    bar_shocks = []
    for i in range(max(1, len(closes) - 240), len(closes)):
        prev_c = closes[i - 1]
        if prev_c <= 0:
            continue
        r_c = (closes[i] - prev_c) / prev_c
        r_h = (highs[i] - prev_c) / prev_c
        r_l = (lows[i] - prev_c) / prev_c
        bar_shocks.append((r_c, r_h, r_l))

    if not bar_shocks:
        return {
            "target_first_prob": 35.0,
            "stop_first_prob": 25.0,
            "in_range_prob": 40.0,
            "win_or_grid_prob": 75.0,
            "expected_r": 0.30,
            "hist_win_rate": 60.0,
            "sims_count": 0
        }

    rng = random.Random(int(entry_price * 1000) + len(closes))
    hits_target = 0
    hits_stop = 0
    stays_range = 0
    total_r_accum = 0.0

    n_shocks = len(bar_shocks)
    for _ in range(n_sims):
        cum_ret = 0.0
        outcome = "RANGE"
        for _ in range(horizon_h):
            rc, rh, rl = bar_shocks[rng.randrange(n_shocks)]
            high_exc = cum_ret + rh
            low_exc = cum_ret + rl
            if low_exc <= down_stop_pct:
                outcome = "STOP"
                break
            if high_exc >= up_target_pct:
                outcome = "TARGET"
                break
            cum_ret += rc

        if outcome == "TARGET":
            hits_target += 1
            total_r_accum += 1.45
        elif outcome == "STOP":
            hits_stop += 1
            total_r_accum -= 1.00
        else:
            stays_range += 1
            total_r_accum += 0.55

    hist_wins, hist_tot = 0, 0
    for idx in range(50, len(closes) - horizon_h):
        c0 = closes[idx]
        if c0 <= 0:
            continue
        min_low_pct = (min(lows[idx + 1:idx + 1 + horizon_h]) - c0) / c0
        if min_low_pct > down_stop_pct:
            hist_wins += 1
        hist_tot += 1

    target_prob = (hits_target / n_sims) * 100.0
    stop_prob = (hits_stop / n_sims) * 100.0
    range_prob = (stays_range / n_sims) * 100.0
    win_or_grid = target_prob + range_prob
    exp_r = total_r_accum / n_sims
    hist_wr = (hist_wins / hist_tot * 100.0) if hist_tot > 0 else win_or_grid

    return {
        "target_first_prob": round(target_prob, 1),
        "stop_first_prob": round(stop_prob, 1),
        "in_range_prob": round(range_prob, 1),
        "win_or_grid_prob": round(win_or_grid, 1),
        "expected_r": round(exp_r, 2),
        "hist_win_rate": round(hist_wr, 1),
        "sims_count": n_sims
    }


# ==========================================
# SKILL #2 & #6: BUDGET & RISK MANAGER (PASS / BLOCK)
# ==========================================
def run_budget_and_risk_manager(bot_state, invest_usdt, entry_price, stop_loss, level_2r, rr_ratio, spread_ok, macro_check_urgent, time_machine_exp_r):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = bot_state.get("daily_realized_pnl_usdt", 0.0) if bot_state.get("daily_date") == today_str else 0.0
    daily_trades = bot_state.get("daily_trades_opened", 0) if bot_state.get("daily_date") == today_str else 0

    risk_pct = ((entry_price - stop_loss) / entry_price * 100.0) if entry_price > 0 else 0.0
    max_loss_usd = invest_usdt * (risk_pct / 100.0)
    daily_loss_pct = (abs(daily_pnl) / invest_usdt * 100.0) if (daily_pnl < 0 and invest_usdt > 0) else 0.0

    reasons = []
    if daily_pnl < 0 and daily_loss_pct >= DAILY_MAX_LOSS_PCT:
        reasons.append(f"Daily Loss Limit raggiunto (-{daily_loss_pct:.1f}% su max {DAILY_MAX_LOSS_PCT}%)")
    if daily_trades >= MAX_TRADES_PER_DAY:
        reasons.append(f"Max Trade Giornalieri raggiunto ({daily_trades}/{MAX_TRADES_PER_DAY})")
    if risk_pct > MAX_LOSS_PCT_PER_TRADE:
        reasons.append(f"Rischio Stop Loss eccessivo ({risk_pct:.2f}% oltre max {MAX_LOSS_PCT_PER_TRADE}%)")
    if rr_ratio < MIN_REWARD_RISK_R:
        reasons.append(f"Reward/Risk basso ({rr_ratio:.2f}R sotto min {MIN_REWARD_RISK_R}R)")
    if not spread_ok:
        reasons.append("Spread Bid/Ask troppo largo rispetto a 1R")
    if macro_check_urgent:
        reasons.append("Evento Macro USD High-Impact entro 3 ore")
    if time_machine_exp_r < 0.05:
        reasons.append(f"Time Machine Expected R basso ({time_machine_exp_r:+.2f}R)")

    passed = len(reasons) == 0
    status_str = "PASS ✅" if passed else f"BLOCK ⛔ ({'; '.join(reasons)})"

    qty_est = round((invest_usdt * (1.0 - PIONEX_FEE)) / entry_price, 5) if entry_price > 0 else 0.0
    bracket_order = {
        "entry_type": "MARKET / LIMIT",
        "entry_price": entry_price,
        "qty": qty_est,
        "stop_loss": stop_loss,
        "take_profit": level_2r,
        "max_risk_usdt": round(max_loss_usd, 2),
        "risk_pct": round(risk_pct, 2)
    }

    return {
        "passed": passed,
        "status": status_str,
        "reasons": reasons,
        "risk_pct": round(risk_pct, 2),
        "max_loss_usd": round(max_loss_usd, 2),
        "daily_pnl_usdt": round(daily_pnl, 2),
        "daily_trades": daily_trades,
        "bracket_order": bracket_order
    }


# ==========================================
# MOTORE DI VALUTAZIONE SINGOLA COIN (10 PILASTRI + 5 SKILLS)
# ==========================================
def analyze_single_coin(coin_cfg, global_ctx, bot_state):
    coin_name = coin_cfg["name"]
    k_pair = coin_cfg["kraken"]
    ws_pair = coin_cfg["ws"]
    f_sym = coin_cfg["futures"]
    p_sym = coin_cfg["pionex"]

    ohlc_1h = fetch_kraken_ohlc(k_pair, 60)
    if len(ohlc_1h) < 120:
        raise ValueError(f"Dati OHLC 1H insufficienti per {coin_name}")

    ohlc_15m = fetch_kraken_ohlc(k_pair, 15)

    h1 = [float(x[2]) for x in ohlc_1h]
    l1 = [float(x[3]) for x in ohlc_1h]
    c1 = [float(x[4]) for x in ohlc_1h]
    v1 = [float(x[6]) for x in ohlc_1h]

    # Costruisce il 4H direttamente dalle 720 ore 1H per dimezzare le chiamate API a Kraken
    h4, l4, c4 = build_4h_from_1h(h1, l1, c1)

    c15 = [float(x[4]) for x in ohlc_15m] if len(ohlc_15m) > 30 else c1
    h15 = [float(x[2]) for x in ohlc_15m] if len(ohlc_15m) > 30 else h1
    l15 = [float(x[3]) for x in ohlc_15m] if len(ohlc_15m) > 30 else l1
    v15 = [float(x[6]) for x in ohlc_15m] if len(ohlc_15m) > 30 else v1

    ob_data = fetch_kraken_orderbook_depth(k_pair, 50)
    deriv_data = global_ctx["futures_map"].get(f_sym, {"funding_8h_pct": 0.0, "open_interest": 0.0, "ok": False})

    price = c1[-1]
    invest_usdt = global_ctx["invest_usdt"]

    ema20_1h = ema_series(c1, 20)[-1]
    ema50_1h = ema_series(c1, 50)[-1]
    ema200_1h = ema_series(c1, 200)[-1]
    ema50_4h = ema_series(c4, 50)[-1]
    ema200_4h = ema_series(c4, 200)[-1]

    rsi_1h = rsi_series(c1, 14)[-1]
    rsi_4h = rsi_series(c4, 14)[-1]
    stoch_k_15m = stoch_rsi_k(c15, 14, 14, 3)
    macd_val, macd_hist, macd_slope = macd_metrics(c1)

    atrs_1h = atr_series(h1, l1, c1, 14)
    atr_1h = atrs_1h[-1]
    atr_pct = (atr_1h / price) * 100.0
    atr_baseline = mean(atrs_1h[-120:]) if len(atrs_1h) >= 120 else atr_1h
    vol_ratio = atr_1h / atr_baseline if atr_baseline > 0 else 1.0

    adx_1h, plus_di_1h, minus_di_1h = adx_di_metrics(h1, l1, c1, 14)
    adx_4h, plus_di_4h, minus_di_4h = adx_di_metrics(h4, l4, c4, 14)
    bb_low, bb_mid, bb_up, pct_b, bb_width, squeeze_on = bollinger_keltner_metrics(h1, l1, c1, 20)

    hurst = calculate_hurst_exponent(c1)
    var_ratio = calculate_variance_ratio(c1, lag=6)
    vwap_24h = calculate_vwap_24h(h1, l1, c1, v1)

    ai_res = run_neural_and_lorentzian_ai(h1, l1, c1, v1)
    ai_prob = ai_res["ensemble_prob"]

    btc_ret_24h = global_ctx["btc_ret_24h"]
    coin_ret_24h = (c1[-1] / c1[-25] - 1.0) * 100.0 if len(c1) >= 25 else 0.0
    rel_strength_btc = coin_ret_24h - btc_ret_24h

    struct_sup, struct_res = find_structural_levels(h1[-180:], l1[-180:], c1[-180:], atr_1h)

    if ai_prob >= 0.58 and price >= ema50_1h:
        down_mult, up_mult = 2.7, 3.3
    elif ai_prob < 0.46 or price < ema50_1h:
        down_mult, up_mult = 3.2, 2.5
    else:
        down_mult, up_mult = 2.9, 2.9

    raw_lower = 0.55 * (price - down_mult * atr_1h) + 0.45 * struct_sup
    raw_upper = 0.55 * (price + up_mult * atr_1h) + 0.45 * struct_res

    lower_bound = round_p(min(raw_lower, price * 0.962), price)
    upper_bound = round_p(max(raw_upper, price * 1.038), price)
    width_usd = upper_bound - lower_bound

    raw_sl = lower_bound - 0.55 * atr_1h
    stop_loss = round_p(max(raw_sl, price * 0.967), price)

    target_step_pct = clamp(atr_pct * 0.58, 0.52, 1.35)
    ideal_grids = int(round((width_usd / price * 100.0) / target_step_pct))
    max_grids_by_capital = max(5, int(invest_usdt // 3.8))
    optimal_grids = clamp(ideal_grids, 6, min(20, max_grids_by_capital))

    step_usd = round_p(width_usd / optimal_grids, price)
    gross_step_pct = (step_usd / price) * 100.0
    net_step_pct = gross_step_pct - (2.0 * PIONEX_FEE * 100.0) - (SLIPPAGE_BUFFER * 100.0)
    usd_per_grid = round(invest_usdt / optimal_grids, 2)

    scanner_timer = run_chart_scanner_and_entry_timer(
        h15, l15, c15, v15, h1, l1, c1, v1, lower_bound, upper_bound
    )

    exit_plan = run_exit_planner(
        price, lower_bound, upper_bound, stop_loss,
        step_usd, net_step_pct, atr_1h, ob_data["spread_bps"]
    )

    time_machine = run_time_machine(
        h1, l1, c1, price, lower_bound, upper_bound,
        stop_loss, exit_plan["level_1r"], horizon_h=24, n_sims=1500
    )

    risk_manager = run_budget_and_risk_manager(
        bot_state, invest_usdt, price, stop_loss,
        exit_plan["level_2r"], exit_plan["rr_ratio"],
        exit_plan["spread_ok"], global_ctx["high_impact_urgent"],
        time_machine["expected_r"]
    )

    breakdown = {}

    if ai_prob >= 0.66:
        s_ai = 14
    elif ai_prob >= 0.59:
        s_ai = 12
    elif ai_prob >= 0.53:
        s_ai = 10
    elif ai_prob >= 0.48:
        s_ai = 7
    elif ai_prob >= 0.43:
        s_ai = 4
    else:
        s_ai = 1
    breakdown["1. Neural MLP + Lorentzian k-NN"] = f"{s_ai}/14 (Prob {ai_prob*100:.1f}%)"

    tm_exp_r = time_machine["expected_r"]
    tm_stop_p = time_machine["stop_first_prob"]
    if tm_exp_r >= 0.42 and tm_stop_p <= 18.0:
        s_tm = 12
    elif tm_exp_r >= 0.28 and tm_stop_p <= 25.0:
        s_tm = 10
    elif tm_exp_r >= 0.15 and tm_stop_p <= 32.0:
        s_tm = 7
    elif tm_exp_r >= 0.05:
        s_tm = 4
    else:
        s_tm = 1
    breakdown["2. Time Machine (1.500 Sims)"] = f"{s_tm}/12 (Exp {tm_exp_r:+.2f}R | Win/Grid {time_machine['win_or_grid_prob']:.0f}%)"

    s_mtf = 0
    if price > ema50_4h and ema50_4h >= ema200_4h * 0.995:
        s_mtf += 6
    elif price > ema50_4h or price > ema200_4h:
        s_mtf += 4
    elif price > ema50_4h * 0.975:
        s_mtf += 2

    if price > ema50_1h and ema20_1h >= ema50_1h:
        s_mtf += 4
    elif price > ema50_1h * 0.988:
        s_mtf += 3
    else:
        s_mtf += 1

    if adx_1h < 28:
        s_mtf += 3
    elif plus_di_1h > minus_di_1h:
        s_mtf += 2
    elif adx_1h > 38 and minus_di_1h > plus_di_1h:
        s_mtf -= 2
    s_mtf = int(clamp(s_mtf, 0, 13))
    breakdown["3. Trend Multi-Timeframe (4H/1H)"] = f"{s_mtf}/13 (ADX {adx_1h:.1f})"

    s_regime = 0
    if hurst < 0.48:
        s_regime += 5
    elif hurst <= 0.55:
        s_regime += 4
    else:
        s_regime += 2

    if var_ratio < 0.95:
        s_regime += 3
    elif var_ratio <= 1.12:
        s_regime += 2
    else:
        s_regime += 1

    if 0.65 <= vol_ratio <= 1.45:
        s_regime += 3
    elif 0.50 <= vol_ratio <= 1.75:
        s_regime += 2
    if squeeze_on and ai_prob < 0.48:
        s_regime -= 2
    s_regime = int(clamp(s_regime, 0, 11))
    breakdown["4. Econometria (Hurst & VarRatio)"] = f"{s_regime}/11 (H={hurst:.2f})"

    pos_in_grid = (price - lower_bound) / width_usd if width_usd > 0 else 0.5
    s_pos = 0
    if 0.20 <= pos_in_grid <= 0.68:
        s_pos += 6
    elif 0.12 <= pos_in_grid <= 0.78:
        s_pos += 4
    else:
        s_pos += 1

    vwap_dist_pct = (price - vwap_24h) / vwap_24h * 100.0 if vwap_24h > 0 else 0.0
    if -2.5 <= vwap_dist_pct <= 1.8:
        s_pos += 3
    elif -4.0 <= vwap_dist_pct <= 3.2:
        s_pos += 2
    else:
        s_pos += 1

    if 0.15 <= pct_b <= 0.78:
        s_pos += 3
    elif 0.05 <= pct_b <= 0.88:
        s_pos += 2
    s_pos = int(clamp(s_pos, 0, 12))
    breakdown["5. Posizione Range & VWAP 24h"] = f"{s_pos}/12 (Pos {pos_in_grid*100:.0f}%)"

    s_mom = scanner_timer["pattern_bonus"]
    if scanner_timer["triggered"]:
        s_mom += 4
    if 38 <= rsi_1h <= 64:
        s_mom += 2
    elif 30 <= rsi_1h <= 70:
        s_mom += 1
    if macd_slope >= 0 or macd_hist >= 0:
        s_mom += 2
    s_mom = int(clamp(s_mom, 0, 11))
    breakdown["6. Chart Scanner & Entry Timer"] = f"{s_mom}/11 ({'TRIG ✅' if scanner_timer['triggered'] else 'WAIT ⏳'})"

    s_micro = 0
    imb = ob_data["imbalance"]
    if imb >= 0.12:
        s_micro += 5
    elif imb >= -0.05:
        s_micro += 4
    elif imb >= -0.18:
        s_micro += 2
    else:
        s_micro += 1

    vol_ma20 = mean(v1[-20:])
    recent_3_red = all(c1[-k] < c1[-k - 1] for k in range(1, 4))
    heavy_dump_vol = mean(v1[-3:]) > vol_ma20 * 1.45
    if recent_3_red and heavy_dump_vol:
        s_micro += 0
    elif v1[-1] >= vol_ma20 * 0.75:
        s_micro += 4
    else:
        s_micro += 3
    s_micro = int(clamp(s_micro, 0, 9))
    breakdown["7. Orderbook Depth L2 & Volumi"] = f"{s_micro}/9 (Imb {imb:+.2f})"

    s_btc = 0
    if global_ctx["btc_safe"]:
        s_btc += 3
    elif global_ctx["btc_price"] > global_ctx["btc_ema50_1h"] * 0.98:
        s_btc += 2
    else:
        s_btc += 1

    if rel_strength_btc >= 0.0:
        s_btc += 3
    elif rel_strength_btc >= -1.8:
        s_btc += 2
    else:
        s_btc += 1
    s_btc = int(clamp(s_btc, 0, 6))
    breakdown["8. Alpha vs BTC & Safety Leader"] = f"{s_btc}/6 (Alpha {rel_strength_btc:+.1f}%)"

    s_sent = 0
    fr = deriv_data["funding_8h_pct"]
    if -0.025 <= fr <= 0.020:
        s_sent += 3
    elif -0.060 <= fr <= 0.045:
        s_sent += 2
    else:
        s_sent += 1

    fng_val = global_ctx["fng_val"]
    if 22 <= fng_val <= 74:
        s_sent += 2
    else:
        s_sent += 1
    s_sent = int(clamp(s_sent, 0, 5))
    breakdown["9. Derivati Funding & Sentiment"] = f"{s_sent}/5 (FR {fr:+.4f}%)"

    s_eff = 0
    if net_step_pct >= 0.45:
        s_eff += 3
    elif net_step_pct >= 0.30:
        s_eff += 2
    elif net_step_pct >= 0.18:
        s_eff += 1

    if exit_plan["rr_ratio"] >= 1.80:
        s_eff += 4
    elif exit_plan["rr_ratio"] >= MIN_REWARD_RISK_R:
        s_eff += 3
    elif exit_plan["rr_ratio"] >= 1.10:
        s_eff += 1
    s_eff = int(clamp(s_eff, 0, 7))
    breakdown["10. Exit Planner & R/R Ratio"] = f"{s_eff}/7 (R/R {exit_plan['rr_ratio']:.2f}R | Net {net_step_pct:.2f}%)"

    raw_total_100 = s_ai + s_tm + s_mtf + s_regime + s_pos + s_mom + s_micro + s_btc + s_sent + s_eff

    penalties = []
    if global_ctx["high_impact_urgent"]:
        raw_total_100 -= 15
        penalties.append("Evento Macro USD High Impact entro 3h (-15 pt)")
    elif global_ctx["high_impact_today"]:
        raw_total_100 -= 5
        penalties.append("Evento Macro USD High Impact entro 12h (-5 pt)")

    if price < ema200_4h * 0.96 and adx_4h > 32 and minus_di_4h > plus_di_4h:
        raw_total_100 -= 12
        penalties.append("Downtrend Direzionale Forte su 4H (-12 pt)")

    if not scanner_timer["triggered"]:
        raw_total_100 -= 5
        penalties.append("Entry Timer 15m in attesa di conferma (-5 pt)")

    raw_total_100 = int(clamp(raw_total_100, 10, 100))
    final_score = int(clamp(round(raw_total_100 / 10.0), 1, 10))

    if final_score >= 7 and (ai_prob < 0.47 or net_step_pct < 0.18):
        final_score = 6

    if not risk_manager["passed"] and final_score >= 6:
        final_score = 5

    return {
        "coin": coin_name,
        "kraken_pair": k_pair,
        "ws_pair": ws_pair,
        "pionex_symbol": p_sym,
        "price": price,
        "decimals": price_decimals(price),
        "change_24h": round(coin_ret_24h, 2),
        "rel_btc_24h": round(rel_strength_btc, 2),
        "raw_score": raw_total_100,
        "final_score": final_score,
        "ai_res": ai_res,
        "ai_prob": ai_prob,
        "hurst": round(hurst, 3),
        "var_ratio": round(var_ratio, 2),
        "atr_1h": atr_1h,
        "atr_pct": round(atr_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "adx_1h": round(adx_1h, 1),
        "adx_4h": round(adx_4h, 1),
        "rsi_1h": round(rsi_1h, 1),
        "rsi_4h": round(rsi_4h, 1),
        "stoch_15m": round(stoch_k_15m, 1),
        "macd_slope": macd_slope,
        "vwap_24h": vwap_24h,
        "pct_b": round(pct_b, 2),
        "squeeze_on": squeeze_on,
        "ema20_1h": ema20_1h,
        "ema50_1h": ema50_1h,
        "ema200_1h": ema200_1h,
        "ema50_4h": ema50_4h,
        "ema200_4h": ema200_4h,
        "ob_data": ob_data,
        "deriv_data": deriv_data,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "stop_loss": stop_loss,
        "optimal_grids": optimal_grids,
        "step_usd": step_usd,
        "gross_step_pct": round(gross_step_pct, 2),
        "net_step_pct": round(net_step_pct, 2),
        "usd_per_grid": usd_per_grid,
        "scanner_timer": scanner_timer,
        "exit_plan": exit_plan,
        "time_machine": time_machine,
        "risk_manager": risk_manager,
        "breakdown": breakdown,
        "penalties": penalties
    }


# ==========================================
# GESTORE AUTONOMO DEL CICLO DI VITA DEL TRADE (+1R / +2R ALERTS)
# ==========================================
def manage_autonomous_grid_bot(bot_state, coins_map, best_coin_data, invest_usdt, high_impact_urgent):
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%d/%m/%Y %H:%M UTC")
    today_str = now_dt.strftime("%Y-%m-%d")

    if bot_state.get("daily_date") != today_str:
        bot_state["daily_date"] = today_str
        bot_state["daily_realized_pnl_usdt"] = 0.0
        bot_state["daily_trades_opened"] = 0

    action_log = {
        "event": "NONE",
        "title": "NESSUNA OPERAZIONE ESEGUITA",
        "details": "",
        "pnl_usdt": 0.0,
        "pnl_pct": 0.0,
        "price_alert": ""
    }

    if bot_state.get("active"):
        active_coin_name = bot_state.get("coin", "SOL")
        coin_data = coins_map.get(active_coin_name, best_coin_data)

        price = coin_data["price"]
        ai_prob = coin_data["ai_prob"]
        final_score = coin_data["final_score"]
        atr_1h = coin_data["atr_1h"]

        entry = bot_state["entry_price"]
        lower = bot_state["lower"]
        upper = bot_state["upper"]
        sl = bot_state["stop_loss"]
        step_usd = bot_state["step_usd"]
        net_step_pct = bot_state["net_step_pct"]
        grids = bot_state["grids"]
        qty = bot_state["qty"]
        invested = bot_state["invested_usdt"]
        symbol = bot_state.get("symbol", f"{active_coin_name}_USDT")

        one_r_usd = bot_state.get("one_r_usd") or max(entry - sl, entry * 0.01)
        level_1r = bot_state.get("level_1r") or round_p(entry + one_r_usd, entry)
        level_2r = bot_state.get("level_2r") or round_p(entry + 2.0 * one_r_usd, entry)

        last_p = bot_state.get("last_sim_price", entry)
        price_move = abs(price - last_p)
        if step_usd > 0 and lower <= price <= upper:
            oscillation = max(price_move, atr_1h * 0.32)
            crossed_grids = int(oscillation // step_usd)
            if crossed_grids > 0:
                per_grid_usdt = invested / max(grids, 1)
                profit_add = crossed_grids * per_grid_usdt * (net_step_pct / 100.0) * 0.55
                bot_state["Accumulated_grid_usdt"] = round(bot_state.get("Accumulated_grid_usdt", 0.0) + profit_add, 4)
                bot_state["grid_fills_count"] = bot_state.get("grid_fills_count", 0) + crossed_grids

        bot_state["last_sim_price"] = price

        alerts_triggered = []
        if price > bot_state.get("peak_price", entry):
            bot_state["peak_price"] = price

        if price >= level_1r and not bot_state.get("alert_1r_sent", False):
            new_be_sl = round_p(max(sl, entry * 1.001), entry)
            bot_state["stop_loss"] = new_be_sl
            bot_state["alert_1r_sent"] = True
            bot_state["trailing_active"] = True
            sl = new_be_sl
            alerts_triggered.append(f"🔔 ALERT +1R RAGGIUNTO ({fmt_p(level_1r)}$): Stop Loss spostato a Break-Even ({fmt_p(new_be_sl)}$)")

        if price >= level_2r and not bot_state.get("alert_2r_sent", False):
            lock_sl = round_p(max(sl, entry + 0.85 * one_r_usd, price - 1.5 * atr_1h), entry)
            bot_state["stop_loss"] = lock_sl
            bot_state["alert_2r_sent"] = True
            bot_state["trailing_active"] = True
            sl = lock_sl
            alerts_triggered.append(f"🔔 ALERT +2R RAGGIUNTO ({fmt_p(level_2r)}$): Profitto blindato con Trailing Stop ({fmt_p(lock_sl)}$)")

        if bot_state.get("trailing_active", False) and price > level_1r:
            candidate_sl = round_p(price - 1.8 * atr_1h, entry)
            if candidate_sl > sl and candidate_sl < price * 0.992:
                bot_state["stop_loss"] = candidate_sl
                sl = candidate_sl

        action_log["price_alert"] = " | ".join(alerts_triggered)

        unrealized_spot_usdt = (price - entry) * qty * 0.55
        grid_usdt = bot_state.get("Accumulated_grid_usdt", 0.0)
        total_pnl_usdt = round(unrealized_spot_usdt + grid_usdt, 2)
        total_pnl_pct = round((total_pnl_usdt / invested) * 100.0, 2) if invested > 0 else 0.0

        close_reason = None
        if price <= sl:
            if bot_state.get("trailing_active"):
                close_reason = f"TRAILING / BREAK-EVEN STOP COLPITO su {active_coin_name} ({fmt_p(price)}$ sotto {fmt_p(sl)}$)"
            else:
                close_reason = f"STOP LOSS DI EMERGENZA COLPITO su {active_coin_name} ({fmt_p(price)}$ sotto {fmt_p(sl)}$)"
        elif price >= upper * 1.004:
            close_reason = f"TAKE PROFIT SUPERIORE RAGGIUNTO su {active_coin_name} ({fmt_p(price)}$ oltre Upper {fmt_p(upper)}$)"
        elif final_score <= 3 and ai_prob < 0.42:
            close_reason = f"USCITA PREVENTIVA AI su {active_coin_name} (Score {final_score}/10 e Prob AI {ai_prob*100:.1f}%)"
        elif high_impact_urgent and total_pnl_pct > 0.35:
            close_reason = f"PROTEZIONE PROFITTO PRE-NEWS MACRO USD su {active_coin_name} (+{total_pnl_pct:.2f}%)"

        if close_reason:
            ok, exec_msg = execute_autonomous_sell(symbol, qty)
            bot_state["active"] = False
            bot_state["total_historical_pnl_usdt"] = round(bot_state.get("total_historical_pnl_usdt", 0.0) + total_pnl_usdt, 2)
            bot_state["closed_trades_count"] = bot_state.get("closed_trades_count", 0) + 1
            bot_state["daily_realized_pnl_usdt"] = round(bot_state.get("daily_realized_pnl_usdt", 0.0) + total_pnl_usdt, 2)

            history = bot_state.get("trade_history", [])
            history.insert(0, {
                "coin": active_coin_name,
                "opened_at": bot_state.get("opened_at", "-"),
                "closed_at": now_str,
                "entry_price": entry,
                "exit_price": price,
                "lower": lower,
                "upper": upper,
                "grids": grids,
                "fills": bot_state.get("grid_fills_count", 0),
                "pnl_usdt": total_pnl_usdt,
                "pnl_pct": total_pnl_pct,
                "reason": close_reason,
                "mode": bot_state.get("mode", "PAPER")
            })
            bot_state["trade_history"] = history[:25]
            save_bot_state(bot_state)

            action_log.update({
                "event": "CLOSED",
                "title": f"🔴 POSIZIONE {active_coin_name} CHIUSA AUTONOMAMENTE",
                "details": f"Motivo: {close_reason}\nEsecuzione: {exec_msg}",
                "pnl_usdt": total_pnl_usdt,
                "pnl_pct": total_pnl_pct
            })
            return action_log
        else:
            save_bot_state(bot_state)
            trail_str = " [Stop a Break-Even/Trailing Attivo 🛡️]" if bot_state.get("trailing_active") else ""
            action_log.update({
                "event": "HOLDING",
                "title": f"🟢 POSIZIONE {active_coin_name} ATTIVA E MONITORATA{trail_str}",
                "details": (
                    f"Coin: {active_coin_name} ({symbol}) | Ingresso: {fmt_p(entry)}$ ({bot_state['opened_at']})\n"
                    f"Range Attivo: {fmt_p(lower)}$ - {fmt_p(upper)}$ | Stop Attuale: {fmt_p(sl)}$\n"
                    f"Livelli R: +1R a {fmt_p(level_1r)}$ | +2R a {fmt_p(level_2r)}$\n"
                    f"Cicli Griglia: {bot_state.get('grid_fills_count', 0)} fills (+{grid_usdt:.2f} USDT)"
                ),
                "pnl_usdt": total_pnl_usdt,
                "pnl_pct": total_pnl_pct
            })
            return action_log

    best_coin = best_coin_data["coin"]
    best_sym = best_coin_data["pionex_symbol"]
    best_score = best_coin_data["final_score"]
    best_prob = best_coin_data["ai_prob"]
    best_price = best_coin_data["price"]
    best_lower = best_coin_data["lower_bound"]
    best_upper = best_coin_data["upper_bound"]
    best_sl = best_coin_data["stop_loss"]
    best_grids = best_coin_data["optimal_grids"]
    best_step = best_coin_data["step_usd"]
    best_net_pct = best_coin_data["net_step_pct"]
    best_exit = best_coin_data["exit_plan"]
    best_risk = best_coin_data["risk_manager"]
    best_timer = best_coin_data["scanner_timer"]

    if (
        best_score >= 7 and
        best_prob >= 0.52 and
        best_risk["passed"] and
        best_timer["triggered"] and
        not high_impact_urgent
    ):
        ok, mode_used, qty_bought, exec_msg = execute_autonomous_buy(best_sym, invest_usdt, best_price)
        if ok:
            bot_state.update({
                "active": True,
                "mode": mode_used,
                "coin": best_coin,
                "symbol": best_sym,
                "entry_price": best_price,
                "lower": best_lower,
                "upper": best_upper,
                "stop_loss": best_sl,
                "initial_stop_loss": best_sl,
                "one_r_usd": best_exit["one_r_usd"],
                "level_1r": best_exit["level_1r"],
                "level_2r": best_exit["level_2r"],
                "alert_1r_sent": False,
                "alert_2r_sent": False,
                "trailing_active": False,
                "grids": best_grids,
                "step_usd": best_step,
                "net_step_pct": best_net_pct,
                "qty": qty_bought,
                "invested_usdt": invest_usdt,
                "opened_at": now_str,
                "peak_price": best_price,
                "Accumulated_grid_usdt": 0.0,
                "grid_fills_count": 0,
                "last_sim_price": best_price,
                "daily_trades_opened": bot_state.get("daily_trades_opened", 0) + 1
            })
            save_bot_state(bot_state)
            action_log.update({
                "event": "OPENED",
                "title": f"🚀 NUOVA POSIZIONE APERTA SU {best_coin} ({mode_used})",
                "details": (
                    f"Coin Scelta dall'AI: {best_coin} a {fmt_p(best_price)}$ (Score {best_score}/10 | AI {best_prob*100:.1f}%)\n"
                    f"Setup: {best_timer['pattern']} | {best_timer['timer_state']}\n"
                    f"Griglia Attivata: {fmt_p(best_lower)}$ - {fmt_p(best_upper)}$ ({best_grids} griglie)\n"
                    f"Bracket Risk: Stop {fmt_p(best_sl)}$ | +1R {fmt_p(best_exit['level_1r'])}$ | +2R {fmt_p(best_exit['level_2r'])}$\n"
                    f"Dettaglio: {exec_msg}"
                ),
                "pnl_usdt": 0.0,
                "pnl_pct": 0.0
            })
            return action_log

    action_log.update({
        "event": "STANDBY",
        "title": "⏸️ BOT IN STANDBY (CAPITALE PROTETTO IN USDT)",
        "details": (
            f"Migliore Coin attuale: {best_coin} con Punteggio {best_score}/10 ({best_coin_data['raw_score']}/100) "
            f"| Risk Check: {best_risk['status']}. "
            f"Il bot aprirà automaticamente appena una delle 10 coin raggiunge almeno 7/10 con Entry Timer TRIGGERED e Risk PASS."
        ),
        "pnl_usdt": 0.0,
        "pnl_pct": 0.0
    })
    return action_log


# ==========================================
# GENERAZIONE CONSOLE WEB INTERATTIVA LIVE (index.html)
# ==========================================
def generate_webapp_dashboard(ctx):
    snapshot_payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC"),
        "invest_usdt": ctx["invest_usdt"],
        "nyse_bell": ctx["nyse_bell"],
        "bot_state": ctx["bot_state"],
        "auto_action": ctx["auto_action"],
        "best_coin": ctx["best_coin"],
        "ranking": ctx["ranking"]
    }

    json_str = json.dumps(snapshot_payload, ensure_ascii=False)

    html_template = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Console Web Live - Multi-Coin Neural Quant AI</title>
    <style>
        :root {
            --bg: #070b14;
            --card: #111827;
            --card-alt: #1e293b;
            --border: #26334d;
            --text: #f8fafc;
            --muted: #94a3b8;
            --accent: #38bdf8;
            --green: #10b981;
            --yellow: #f59e0b;
            --red: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding: 14px;
            line-height: 1.45;
        }
        .container { max-width: 1320px; margin: 0 auto; }
        .topbar {
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            background: var(--card);
            border: 1px solid var(--border);
            padding: 16px 20px;
            border-radius: 14px;
            margin-bottom: 16px;
        }
        .topbar h1 { font-size: 1.25rem; color: var(--accent); display: flex; align-items: center; gap: 8px; }
        .live-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 700;
            background: rgba(16, 185, 129, 0.12);
            color: var(--green);
            border: 1px solid var(--green);
        }
        .dot-pulse {
            width: 9px;
            height: 9px;
            background: var(--green);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--green);
            animation: pulse 1.4s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.95); opacity: 0.7; }
            50% { transform: scale(1.25); opacity: 1; }
            100% { transform: scale(0.95); opacity: 0.7; }
        }
        .advisor-banner {
            border-radius: 14px;
            padding: 18px 22px;
            margin-bottom: 18px;
            border: 2px solid var(--border);
            background: var(--card);
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
        }
        .advisor-title { font-size: 1.15rem; font-weight: 800; margin-bottom: 4px; }
        .advisor-desc { font-size: 0.9rem; color: #e2e8f0; }
        .btn-group { display: flex; flex-wrap: wrap; gap: 10px; }
        button {
            cursor: pointer;
            border: none;
            border-radius: 9px;
            padding: 10px 15px;
            font-weight: 700;
            font-size: 0.85rem;
            transition: all 0.15s;
            color: #fff;
        }
        button:hover { opacity: 0.92; transform: translateY(-1px); }
        .btn-green { background: var(--green); color: #052e16; }
        .btn-red { background: var(--red); }
        .btn-blue { background: var(--accent); color: #082f49; }
        .btn-yellow { background: var(--yellow); color: #451a03; }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 14px;
            margin-bottom: 18px;
        }
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 16px;
        }
        .card-label { font-size: 0.76rem; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
        .card-val { font-size: 1.48rem; font-weight: 800; }
        .card-sub { font-size: 0.8rem; color: var(--muted); margin-top: 4px; }
        .main-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
            gap: 18px;
            margin-bottom: 18px;
        }
        .section-title {
            font-size: 0.98rem;
            font-weight: 700;
            color: var(--accent);
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
            margin-bottom: 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .metric-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px dashed rgba(255,255,255,0.06);
            font-size: 0.86rem;
        }
        .m-label { color: var(--muted); }
        .m-val { font-weight: 600; text-align: right; }
        .range-bar-wrap {
            margin: 14px 0;
            background: #1e293b;
            height: 22px;
            border-radius: 11px;
            position: relative;
            overflow: hidden;
            border: 1px solid #334155;
        }
        .range-fill {
            height: 100%;
            background: linear-gradient(90deg, #ef4444 0%, #10b981 50%, #38bdf8 100%);
            opacity: 0.38;
            width: 100%;
        }
        .range-marker {
            position: absolute;
            top: 2px;
            bottom: 2px;
            width: 6px;
            background: #ffffff;
            border-radius: 3px;
            box-shadow: 0 0 10px #fff;
            transition: left 0.3s ease;
        }
        .form-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-bottom: 12px;
        }
        .form-group label {
            display: block;
            font-size: 0.75rem;
            color: var(--muted);
            margin-bottom: 4px;
        }
        .form-group input, .form-group select {
            width: 100%;
            padding: 8px 10px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: #0b1120;
            color: var(--text);
            font-weight: 600;
            font-size: 0.88rem;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.83rem;
        }
        th, td {
            text-align: left;
            padding: 9px 8px;
            border-bottom: 1px solid var(--border);
        }
        th { color: var(--muted); font-size: 0.73rem; text-transform: uppercase; }
        .flash-up { background-color: rgba(16, 185, 129, 0.22) !important; transition: background-color 0.2s; }
        .flash-down { background-color: rgba(239, 68, 68, 0.22) !important; transition: background-color 0.2s; }
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #0284c7;
            color: #fff;
            padding: 12px 20px;
            border-radius: 10px;
            font-weight: 700;
            box-shadow: 0 8px 25px rgba(0,0,0,0.5);
            display: none;
            z-index: 9999;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <h1>🎛️ Console Operativa Web Live — Multi-Coin Neural Quant AI</h1>
            <div style="font-size:0.8rem;color:var(--muted);">
                Snapshot Bot: <span id="snap-time">-</span> | NYSE: <span id="nyse-status">-</span>
            </div>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
            <div class="live-pill" id="ws-pill">
                <span class="dot-pulse" id="ws-dot"></span>
                <span id="ws-status">CONNESSIONE STREAM KRAKEN...</span>
            </div>
            <button class="btn-outline" onclick="forceRefreshPrices()">🔄 Aggiorna Ora</button>
        </div>
    </div>

    <div class="advisor-banner" id="advisor-banner">
        <div>
            <div style="font-size:0.75rem;text-transform:uppercase;letter-spacing:1px;color:var(--muted);">
                🧭 CONSIGLIO OPERATIVO IN TEMPO REALE (AUTO + MANUALE)
            </div>
            <div class="advisor-title" id="advisor-title">Analisi in corso...</div>
            <div class="advisor-desc" id="advisor-desc">Attendere caricamento prezzi live...</div>
        </div>
        <div class="btn-group" id="advisor-actions"></div>
    </div>

    <div class="kpi-grid">
        <div class="card">
            <div class="card-label">Stato Posizione Attiva</div>
            <div class="card-val" id="kpi-pos-status">-</div>
            <div class="card-sub" id="kpi-pos-sub">-</div>
        </div>
        <div class="card">
            <div class="card-label">PnL Tempo Reale (Spot + Grid)</div>
            <div class="card-val" id="kpi-live-pnl">+0.00 USDT (0.00%)</div>
            <div class="card-sub" id="kpi-live-fills">Cicli Fills: 0 | Prezzo Live: -</div>
        </div>
        <div class="card">
            <div class="card-label">Migliore Opportunità AI (#1 su 10)</div>
            <div class="card-val" id="kpi-best-coin">-</div>
            <div class="card-sub" id="kpi-best-sub">-</div>
        </div>
        <div class="card">
            <div class="card-label">Bilancio Storico Posizioni Chiuse</div>
            <div class="card-val" id="kpi-hist-pnl">+0.00 USDT</div>
            <div class="card-sub" id="kpi-hist-count">Trade chiusi totali: 0</div>
        </div>
    </div>

    <div class="main-grid">
        <div class="card">
            <div class="section-title">
                <span>🟢 Monitor Posizione Aperta (Live Tick-by-Tick)</span>
                <span id="active-mode-badge" style="font-size:0.78rem;padding:2px 8px;border-radius:6px;background:#1e293b;">-</span>
            </div>
            <div id="active-pos-container"></div>
        </div>

        <div class="card">
            <div class="section-title">
                <span>🛠️ Console Apertura Manuale & Parametri Pionex</span>
                <button class="btn-outline" style="padding:4px 9px;font-size:0.75rem;" onclick="loadBestCoinIntoForm()">Carica Top Pick AI</button>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label>Criptovaluta (10 Coin)</label>
                    <select id="man-coin" onchange="onManualCoinChange()"></select>
                </div>
                <div class="form-group">
                    <label>Prezzo Live Attuale ($)</label>
                    <input type="number" id="man-price" step="any" readonly>
                </div>
                <div class="form-group">
                    <label>Lower Bound Griglia ($)</label>
                    <input type="number" id="man-lower" step="any">
                </div>
                <div class="form-group">
                    <label>Upper Bound Griglia ($)</label>
                    <input type="number" id="man-upper" step="any">
                </div>
                <div class="form-group">
                    <label>Stop Loss Emergenza -1R ($)</label>
                    <input type="number" id="man-sl" step="any">
                </div>
                <div class="form-group">
                    <label>Target +1R Break-Even ($)</label>
                    <input type="number" id="man-1r" step="any">
                </div>
                <div class="form-group">
                    <label>Numero Griglie Ottimali</label>
                    <input type="number" id="man-grids" step="1">
                </div>
                <div class="form-group">
                    <label>Capitale Investito (USDT)</label>
                    <input type="number" id="man-invest" step="1">
                </div>
            </div>
            <div class="btn-group" style="margin-top:8px;">
                <button class="btn-green" onclick="openManualPosition()">🚀 Apri / Registra Posizione Live</button>
                <button class="btn-blue" onclick="copyPionexParams()">📋 Copia Parametri per App Pionex</button>
                <button class="btn-outline" onclick="resetLocalConsoleState()">🔄 Sincronizza col Bot GitHub</button>
            </div>
            <div style="font-size:0.77rem;color:var(--muted);margin-top:10px;">
                💡 <b>Come usarla:</b> Se apri manualmente un bot su Pionex o vuoi testare una coin in tempo reale, clicca su <b>"Apri / Registra Posizione Live"</b>: la console calcolerà il PnL al secondo e ti avviserà quando spostare lo Stop o chiudere!
            </div>
        </div>
    </div>

    <div class="card" style="margin-bottom:18px;overflow-x:auto;">
        <div class="section-title">
            <span>📊 Scanner Live delle 10 Criptovalute (Prezzi in Tempo Reale da Kraken)</span>
            <span style="font-size:0.76rem;color:var(--muted);">Clicca "Usa" per caricare i parametri nel pannello manuale</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Rank & Coin</th>
                    <th>Prezzo Live ($)</th>
                    <th>Var 24h</th>
                    <th>Score AI</th>
                    <th>Prob AI</th>
                    <th>Time Machine (24h)</th>
                    <th>Entry Timer 15m</th>
                    <th>Risk Gate</th>
                    <th>Range Griglia Consigliato</th>
                    <th>Azione</th>
                </tr>
            </thead>
            <tbody id="ranking-tbody"></tbody>
        </table>
    </div>

    <div class="card" style="overflow-x:auto;">
        <div class="section-title">
            <span>📜 Registro Posizioni Chiuse (Automatiche Bot + Manuali Console)</span>
            <span id="closed-summary-badge" style="font-size:0.78rem;color:var(--muted);">-</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Coin & Modo</th>
                    <th>Apertura</th>
                    <th>Chiusura</th>
                    <th>Ingresso → Uscita</th>
                    <th>Range Griglia</th>
                    <th>Fills</th>
                    <th>PnL Realizzato</th>
                    <th>Motivo Chiusura</th>
                </tr>
            </thead>
            <tbody id="history-tbody"></tbody>
        </table>
    </div>
</div>

<div class="toast" id="toast-msg">Operazione completata!</div>

<script>
const SNAPSHOT = __SNAPSHOT_JSON__;
const STORAGE_KEY = "pionex_ai_console_state_v7";

let livePrices = {};
let coinsByName = {};
SNAPSHOT.ranking.forEach(c => {
    livePrices[c.coin] = c.price;
    coinsByName[c.coin] = c;
});

function loadConsoleState() {
    const savedRaw = localStorage.getItem(STORAGE_KEY);
    if (savedRaw) {
        try {
            const parsed = JSON.parse(savedRaw);
            if (!parsed.manual_override) {
                return JSON.parse(JSON.stringify(SNAPSHOT.bot_state));
            }
            return parsed;
        } catch (e) {}
    }
    return JSON.parse(JSON.stringify(SNAPSHOT.bot_state));
}

let appState = loadConsoleState();

function saveConsoleState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(appState));
}

function showToast(msg) {
    const t = document.getElementById("toast-msg");
    t.textContent = msg;
    t.style.display = "block";
    setTimeout(() => { t.style.display = "none"; }, 3200);
}

function fmtPrice(p, coinName) {
    const ref = coinsByName[coinName];
    const dec = ref ? ref.decimals : (Math.abs(p) >= 1000 ? 1 : (Math.abs(p) >= 10 ? 2 : (Math.abs(p) >= 1 ? 3 : 4)));
    return Number(p).toFixed(dec);
}

function initManualForm() {
    const sel = document.getElementById("man-coin");
    sel.innerHTML = "";
    SNAPSHOT.ranking.forEach(c => {
        const opt = document.createElement("option");
        opt.value = c.coin;
        opt.textContent = `${c.coin} (${c.pionex_symbol}) — Score ${c.final_score}/10`;
        sel.appendChild(opt);
    });
    loadBestCoinIntoForm();
}

function loadBestCoinIntoForm() {
    const best = SNAPSHOT.best_coin.coin;
    document.getElementById("man-coin").value = best;
    fillFormWithCoin(best);
}

function selectCoinForManual(coinName) {
    document.getElementById("man-coin").value = coinName;
    fillFormWithCoin(coinName);
    showToast(`Parametri di ${coinName} caricati nella Console Manuale!`);
    window.scrollTo({ top: 180, behavior: "smooth" });
}

function onManualCoinChange() {
    const coinName = document.getElementById("man-coin").value;
    fillFormWithCoin(coinName);
}

function fillFormWithCoin(coinName) {
    const c = coinsByName[coinName] || SNAPSHOT.best_coin;
    const curP = livePrices[coinName] || c.price;
    document.getElementById("man-price").value = fmtPrice(curP, coinName);
    document.getElementById("man-lower").value = c.lower_bound;
    document.getElementById("man-upper").value = c.upper_bound;
    document.getElementById("man-sl").value = c.stop_loss;
    document.getElementById("man-1r").value = c.exit_plan.level_1r;
    document.getElementById("man-grids").value = c.optimal_grids;
    document.getElementById("man-invest").value = SNAPSHOT.invest_usdt;
}

function openManualPosition() {
    const coinName = document.getElementById("man-coin").value;
    const c = coinsByName[coinName] || SNAPSHOT.best_coin;
    const entryP = parseFloat(livePrices[coinName] || document.getElementById("man-price").value);
    const lowerP = parseFloat(document.getElementById("man-lower").value);
    const upperP = parseFloat(document.getElementById("man-upper").value);
    const slP = parseFloat(document.getElementById("man-sl").value);
    const lv1R = parseFloat(document.getElementById("man-1r").value);
    const gridsN = parseInt(document.getElementById("man-grids").value, 10) || 10;
    const invUsdt = parseFloat(document.getElementById("man-invest").value) || SNAPSHOT.invest_usdt;

    if (appState.active) {
        if (!confirm(`Hai già una posizione attiva su ${appState.coin}. Vuoi chiuderla e sostituirla con ${coinName}?`)) {
            return;
        }
        closeActivePosition("Sostituita manualmente con nuova posizione");
    }

    const oneR = Math.max(entryP - slP, entryP * 0.01);
    const stepUsd = (upperP - lowerP) / Math.max(gridsN, 1);
    const qty = (invUsdt * 0.999) / entryP;
    const nowStr = new Date().toLocaleString("it-IT", { timeZone: "UTC" }) + " UTC";

    appState.active = true;
    appState.manual_override = true;
    appState.mode = "MANUALE_LIVE";
    appState.coin = coinName;
    appState.symbol = `${coinName}_USDT`;
    appState.entry_price = entryP;
    appState.lower = lowerP;
    appState.upper = upperP;
    appState.stop_loss = slP;
    appState.initial_stop_loss = slP;
    appState.one_r_usd = oneR;
    appState.level_1r = lv1R || (entryP + oneR);
    appState.level_2r = entryP + 2 * oneR;
    appState.trailing_active = false;
    appState.grids = gridsN;
    appState.step_usd = stepUsd;
    appState.net_step_pct = c.net_step_pct || 0.45;
    appState.qty = qty;
    appState.invested_usdt = invUsdt;
    appState.opened_at = nowStr;
    appState.peak_price = entryP;
    appState.Accumulated_grid_usdt = 0.0;
    appState.grid_fills_count = 0;
    appState.last_sim_price = entryP;

    saveConsoleState();
    renderAll();
    showToast(`🚀 Posizione su ${coinName} aperta e monitorata in tempo reale!`);
}

function moveStopToBreakEven() {
    if (!appState.active) return;
    const newSl = Number(fmtPrice(appState.entry_price * 1.001, appState.coin));
    appState.stop_loss = newSl;
    appState.trailing_active = true;
    appState.manual_override = true;
    saveConsoleState();
    renderAll();
    showToast(`🛡️ Stop Loss spostato a Pareggio (${newSl}$) su ${appState.coin}!`);
}

function closeActivePosition(customReason) {
    if (!appState.active) return;
    const coin = appState.coin;
    const curP = livePrices[coin] || appState.entry_price;
    const metrics = computeLivePositionMetrics();
    const nowStr = new Date().toLocaleString("it-IT", { timeZone: "UTC" }) + " UTC";
    const reason = customReason || `Chiusura Manuale da Console Web a ${fmtPrice(curP, coin)}$`;

    appState.active = false;
    appState.manual_override = true;
    appState.total_historical_pnl_usdt = Number(((appState.total_historical_pnl_usdt || 0) + metrics.pnlUsdt).toFixed(2));
    appState.closed_trades_count = (appState.closed_trades_count || 0) + 1;

    if (!appState.trade_history) appState.trade_history = [];
    appState.trade_history.unshift({
        coin: coin,
        opened_at: appState.opened_at || "-",
        closed_at: nowStr,
        entry_price: appState.entry_price,
        exit_price: curP,
        lower: appState.lower,
        upper: appState.upper,
        grids: appState.grids,
        fills: appState.grid_fills_count || 0,
        pnl_usdt: metrics.pnlUsdt,
        pnl_pct: metrics.pnlPct,
        reason: reason,
        mode: appState.mode || "MANUALE"
    });

    saveConsoleState();
    renderAll();
    showToast(`🔴 Posizione ${coin} chiusa a ${fmtPrice(curP, coin)}$ (PnL: ${metrics.pnlUsdt >= 0 ? '+' : ''}${metrics.pnlUsdt.toFixed(2)} USDT)`);
}

function resetLocalConsoleState() {
    localStorage.removeItem(STORAGE_KEY);
    appState = JSON.parse(JSON.stringify(SNAPSHOT.bot_state));
    renderAll();
    showToast("🔄 Console ri-sincronizzata con l'ultimo stato del Bot GitHub!");
}

function copyPionexParams() {
    const coinName = document.getElementById("man-coin").value;
    const lowerP = document.getElementById("man-lower").value;
    const upperP = document.getElementById("man-upper").value;
    const gridsN = document.getElementById("man-grids").value;
    const slP = document.getElementById("man-sl").value;
    const lv1r = document.getElementById("man-1r").value;
    const inv = document.getElementById("man-invest").value;

    const text = `PIONEX GRID BOT — ${coinName}/USDT\nLower Price: ${lowerP}\nUpper Price: ${upperP}\nGrids: ${gridsN} (Arithmetic)\nInvestimento: ${inv} USDT\nStop Loss (-1R): ${slP}\nTarget +1R (Break-Even): ${lv1r}`;
    navigator.clipboard.writeText(text).then(() => {
        showToast(`📋 Parametri Pionex per ${coinName} copiati negli appunti!`);
    });
}

function computeLivePositionMetrics() {
    if (!appState.active) {
        return { pnlUsdt: 0, pnlPct: 0, curPrice: 0, posPct: 50, distSlPct: 0 };
    }
    const coin = appState.coin;
    const curPrice = livePrices[coin] || appState.entry_price;
    const entry = appState.entry_price;
    const qty = appState.qty || ((appState.invested_usdt || 50) / entry);
    const invested = appState.invested_usdt || SNAPSHOT.invest_usdt;

    const lastSim = appState.last_sim_price || entry;
    const stepUsd = appState.step_usd || ((appState.upper - appState.lower) / Math.max(appState.grids || 10, 1));
    if (stepUsd > 0 && curPrice >= appState.lower && curPrice <= appState.upper) {
        const crossed = Math.floor(Math.abs(curPrice - lastSim) / stepUsd);
        if (crossed > 0) {
            const perGrid = invested / Math.max(appState.grids || 10, 1);
            const addProfit = crossed * perGrid * ((appState.net_step_pct || 0.4) / 100.0) * 0.55;
            appState.Accumulated_grid_usdt = Number(((appState.Accumulated_grid_usdt || 0) + addProfit).toFixed(4));
            appState.grid_fills_count = (appState.grid_fills_count || 0) + crossed;
            appState.last_sim_price = curPrice;
            saveConsoleState();
        }
    }

    const spotPnl = (curPrice - entry) * qty * 0.55;
    const gridPnl = appState.Accumulated_grid_usdt || 0.0;
    const pnlUsdt = Number((spotPnl + gridPnl).toFixed(2));
    const pnlPct = invested > 0 ? Number(((pnlUsdt / invested) * 100.0).toFixed(2)) : 0.0;
    const posPct = (appState.upper > appState.lower)
        ? Math.max(2, Math.min(98, ((curPrice - appState.lower) / (appState.upper - appState.lower)) * 100.0))
        : 50.0;
    const distSlPct = curPrice > 0 ? Number((((curPrice - appState.stop_loss) / curPrice) * 100.0).toFixed(2)) : 0.0;

    return { pnlUsdt, pnlPct, curPrice, posPct, distSlPct, gridPnl };
}

function renderAll() {
    document.getElementById("snap-time").textContent = SNAPSHOT.generated_at;
    document.getElementById("nyse-status").textContent = SNAPSHOT.nyse_bell.status;

    const best = SNAPSHOT.best_coin;
    const bestLiveP = livePrices[best.coin] || best.price;
    const posMetrics = computeLivePositionMetrics();

    const banner = document.getElementById("advisor-banner");
    const advTitle = document.getElementById("advisor-title");
    const advDesc = document.getElementById("advisor-desc");
    const advActions = document.getElementById("advisor-actions");
    advActions.innerHTML = "";

    if (appState.active) {
        const coin = appState.coin;
        const curP = posMetrics.curPrice;
        const sl = appState.stop_loss;
        const upper = appState.upper;
        const lv1r = appState.level_1r || (appState.entry_price * 1.02);

        if (curP <= sl) {
            banner.style.borderColor = "var(--red)";
            banner.style.background = "rgba(239, 68, 68, 0.12)";
            advTitle.style.color = "var(--red)";
            advTitle.textContent = `🚨 AZIONE MANUALE RICHIESTA: CHIUDI SUBITO ${coin}! (STOP LOSS TOCCATO)`;
            advDesc.textContent = `Il prezzo live (${fmtPrice(curP, coin)}$) è sceso sotto lo Stop Loss (${fmtPrice(sl, coin)}$). Chiudi la posizione per proteggere il capitale.`;
            advActions.innerHTML = `<button class="btn-red" onclick="closeActivePosition('Stop Loss toccato in Tempo Reale')">🔴 Chiudi Subito ${coin}</button>`;
        } else if (curP >= upper * 1.003) {
            banner.style.borderColor = "var(--green)";
            banner.style.background = "rgba(16, 185, 129, 0.14)";
            advTitle.style.color = "var(--green)";
            advTitle.textContent = `🎯 TAKE PROFIT RAGGIUNTO SU ${coin}: INCASSA IL PROFITTO!`;
            advDesc.textContent = `Il prezzo live (${fmtPrice(curP, coin)}$) ha superato il tetto superiore della griglia (${fmtPrice(upper, coin)}$). Tutte le griglie hanno venduto in profitto.`;
            advActions.innerHTML = `<button class="btn-green" onclick="closeActivePosition('Take Profit Superiore raggiunto Live')">🎯 Incassa e Chiudi ${coin}</button>`;
        } else if (curP >= lv1r && !appState.trailing_active) {
            banner.style.borderColor = "var(--accent)";
            banner.style.background = "rgba(56, 189, 248, 0.12)";
            advTitle.style.color = "var(--accent)";
            advTitle.textContent = `🛡️ TRAGUARDO +1R RAGGIUNTO SU ${coin}: SPOSTA LO STOP A PAREGGIO!`;
            advDesc.textContent = `Il prezzo live (${fmtPrice(curP, coin)}$) ha superato +1R (${fmtPrice(lv1r, coin)}$). Azzeriamo il rischio portando lo Stop Loss al prezzo di ingresso.`;
            advActions.innerHTML = `<button class="btn-blue" onclick="moveStopToBreakEven()">🛡️ Sposta Stop a Break-Even</button>`;
        } else {
            banner.style.borderColor = "var(--green)";
            banner.style.background = "rgba(16, 185, 129, 0.07)";
            advTitle.style.color = "var(--green)";
            advTitle.textContent = `🟢 POSIZIONE ${coin} ATTIVA E NEL RANGE: NESSUNA AZIONE RICHIESTA`;
            advDesc.textContent = `Prezzo live ${fmtPrice(curP, coin)}$ dentro il canale [${fmtPrice(appState.lower, coin)}$ - ${fmtPrice(appState.upper, coin)}$]. Distanza dallo Stop Loss: ${posMetrics.distSlPct >= 0 ? '+' : ''}${posMetrics.distSlPct}%.`;
            advActions.innerHTML = `
                <button class="btn-yellow" onclick="moveStopToBreakEven()">🛡️ Stop a Pareggio</button>
                <button class="btn-red" onclick="closeActivePosition()">🔴 Chiudi Manualmente</button>
            `;
        }
    } else {
        if (best.final_score >= 7 && best.risk_manager.passed && best.scanner_timer.triggered) {
            banner.style.borderColor = "var(--green)";
            banner.style.background = "rgba(16, 185, 129, 0.12)";
            advTitle.style.color = "var(--green)";
            advTitle.textContent = `🚀 SEGNALE VERDE: PUOI APRIRE SU ${best.coin}/USDT (Voto ${best.final_score}/10)`;
            advDesc.textContent = `Setup "${best.scanner_timer.pattern}" confermato | Time Machine: ${best.time_machine.expected_r >= 0 ? '+' : ''}${best.time_machine.expected_r}R | Range: ${fmtPrice(best.lower_bound, best.coin)}$ - ${fmtPrice(best.upper_bound, best.coin)}$ (${best.optimal_grids} griglie).`;
            advActions.innerHTML = `
                <button class="btn-green" onclick="selectCoinForManual('${best.coin}');openManualPosition();">🚀 Apri Ora ${best.coin}</button>
                <button class="btn-blue" onclick="selectCoinForManual('${best.coin}');copyPionexParams();">📋 Copia Parametri Pionex</button>
            `;
        } else {
            banner.style.borderColor = "var(--yellow)";
            banner.style.background = "rgba(245, 158, 11, 0.08)";
            advTitle.style.color = "var(--yellow)";
            advTitle.textContent = `⏸️ ATTESA CONSIGLIATA: NON APRIRE MANUALMENTE ADESSO (Resta in USDT)`;
            advDesc.textContent = `La migliore coin attuale è ${best.coin} (${fmtPrice(bestLiveP, best.coin)}$) con punteggio ${best.final_score}/10 (${best.raw_score}/100) e Timer "${best.scanner_timer.timer_state}". Attendi almeno 7/10 con Risk PASS.`;
            advActions.innerHTML = `
                <button class="btn-outline" onclick="selectCoinForManual('${best.coin}')">🔍 Carica comunque ${best.coin}</button>
            `;
        }
    }

    if (appState.active) {
        document.getElementById("kpi-pos-status").innerHTML = `<span style="color:var(--green);">🟢 ${appState.coin}/USDT</span>`;
        document.getElementById("kpi-pos-sub").textContent = `Ingresso: ${fmtPrice(appState.entry_price, appState.coin)}$ | Stop: ${fmtPrice(appState.stop_loss, appState.coin)}$`;
        const pCol = posMetrics.pnlUsdt >= 0 ? "var(--green)" : "var(--red)";
        document.getElementById("kpi-live-pnl").innerHTML = `<span style="color:${pCol};">${posMetrics.pnlUsdt >= 0 ? '+' : ''}${posMetrics.pnlUsdt.toFixed(2)} USDT (${posMetrics.pnlPct >= 0 ? '+' : ''}${posMetrics.pnlPct.toFixed(2)}%)</span>`;
        document.getElementById("kpi-live-fills").textContent = `Fills: ${appState.grid_fills_count || 0} | Live: ${fmtPrice(posMetrics.curPrice, appState.coin)}$`;
    } else {
        document.getElementById("kpi-pos-status").innerHTML = `<span style="color:var(--yellow);">⏸️ NESSUNA (100% USDT)</span>`;
        document.getElementById("kpi-pos-sub").textContent = `Capitale protetto pronto: ${SNAPSHOT.invest_usdt} USDT`;
        document.getElementById("kpi-live-pnl").innerHTML = `<span style="color:var(--muted);">0.00 USDT (0.00%)</span>`;
        document.getElementById("kpi-live-fills").textContent = `In attesa di segnale >= 7/10`;
    }

    document.getElementById("kpi-best-coin").textContent = `${best.coin} (${fmtPrice(bestLiveP, best.coin)}$)`;
    document.getElementById("kpi-best-sub").textContent = `Score: ${best.final_score}/10 (${best.raw_score}pt) | TM: ${best.time_machine.expected_r >= 0 ? '+' : ''}${best.time_machine.expected_r}R`;

    const histPnl = appState.total_historical_pnl_usdt || 0.0;
    const hCol = histPnl >= 0 ? "var(--green)" : "var(--red)";
    document.getElementById("kpi-hist-pnl").innerHTML = `<span style="color:${hCol};">${histPnl >= 0 ? '+' : ''}${histPnl.toFixed(2)} USDT</span>`;
    document.getElementById("kpi-hist-count").textContent = `Trade chiusi totali: ${appState.closed_trades_count || 0}`;

    const posBox = document.getElementById("active-pos-container");
    document.getElementById("active-mode-badge").textContent = appState.active ? (appState.mode || "PAPER") : "STANDBY";

    if (appState.active) {
        const cName = appState.coin;
        const pCol = posMetrics.pnlUsdt >= 0 ? "var(--green)" : "var(--red)";
        posBox.innerHTML = `
            <div style="display:flex;justify-content:space-between;font-size:0.84rem;font-weight:700;">
                <span style="color:var(--red);">Lower: ${fmtPrice(appState.lower, cName)}$</span>
                <span style="color:var(--accent);">Prezzo Live: ${fmtPrice(posMetrics.curPrice, cName)}$ (${posMetrics.posPct.toFixed(0)}%)</span>
                <span style="color:var(--green);">Upper: ${fmtPrice(appState.upper, cName)}$</span>
            </div>
            <div class="range-bar-wrap">
                <div class="range-fill"></div>
                <div class="range-marker" style="left:calc(${posMetrics.posPct.toFixed(1)}% - 3px);"></div>
            </div>
            <div class="metric-row"><span class="m-label">Coppia & Data Apertura</span><span class="m-val">${cName}/USDT (${appState.opened_at || '-'})</span></div>
            <div class="metric-row"><span class="m-label">Prezzo Ingresso → Prezzo Live</span><span class="m-val">${fmtPrice(appState.entry_price, cName)}$ → <b>${fmtPrice(posMetrics.curPrice, cName)}$</b></span></div>
            <div class="metric-row"><span class="m-label">PnL Totale Live (Spot + Grid)</span><span class="m-val" style="color:${pCol};font-weight:800;">${posMetrics.pnlUsdt >= 0 ? '+' : ''}${posMetrics.pnlUsdt.toFixed(2)} USDT (${posMetrics.pnlPct >= 0 ? '+' : ''}${posMetrics.pnlPct.toFixed(2)}%)</span></div>
            <div class="metric-row"><span class="m-label">Profitto Cicli Griglia Accumulato</span><span class="m-val" style="color:var(--green);">+${(posMetrics.gridPnl || 0).toFixed(3)} USDT (${appState.grid_fills_count || 0} fills)</span></div>
            <div class="metric-row"><span class="m-label">Stop Loss Attuale (Distanza Live)</span><span class="m-val" style="color:var(--red);">${fmtPrice(appState.stop_loss, cName)}$ (${posMetrics.distSlPct >= 0 ? '+' : ''}${posMetrics.distSlPct}%)</span></div>
            <div class="metric-row"><span class="m-label">Target +1R (Break-Even) / +2R (Trailing)</span><span class="m-val">${fmtPrice(appState.level_1r || appState.entry_price*1.02, cName)}$ / ${fmtPrice(appState.level_2r || appState.entry_price*1.04, cName)}$</span></div>
            <div class="btn-group" style="margin-top:14px;">
                <button class="btn-red" onclick="closeActivePosition()">🔴 Chiudi Posizione Ora (${fmtPrice(posMetrics.curPrice, cName)}$)</button>
                <button class="btn-yellow" onclick="moveStopToBreakEven()">🛡️ Sposta Stop a Pareggio</button>
            </div>
        `;
    } else {
        posBox.innerHTML = `
            <div style="text-align:center;padding:28px 12px;color:var(--muted);">
                <div style="font-size:2rem;margin-bottom:8px;">⏸️</div>
                <div style="font-weight:700;color:var(--text);margin-bottom:6px;">Nessuna Posizione Aperta al Momento</div>
                <div style="font-size:0.84rem;margin-bottom:16px;">
                    Il Bot tiene i tuoi <b>${SNAPSHOT.invest_usdt} USDT</b> al sicuro. Puoi attendere l'apertura automatica del Bot oppure aprire/simulare una posizione dal pannello qui a destra.
                </div>
                <button class="btn-green" onclick="loadBestCoinIntoForm();openManualPosition();">🚀 Simula Apertura su Top Pick (${best.coin})</button>
            </div>
        `;
    }

    const selCoin = document.getElementById("man-coin").value;
    if (selCoin && livePrices[selCoin]) {
        document.getElementById("man-price").value = fmtPrice(livePrices[selCoin], selCoin);
    }

    const rankBody = document.getElementById("ranking-tbody");
    rankBody.innerHTML = "";
    SNAPSHOT.ranking.forEach((c, idx) => {
        const curP = livePrices[c.coin] || c.price;
        const sc = c.final_score;
        const scCol = sc >= 7 ? "var(--green)" : (sc === 6 ? "var(--yellow)" : "var(--red)");
        const chgCol = c.change_24h >= 0 ? "var(--green)" : "var(--red)";
        const rkBadge = c.risk_manager.passed
            ? `<span style="color:var(--green);font-weight:700;">PASS ✅</span>`
            : `<span style="color:var(--red);font-weight:700;">BLOCK ⛔</span>`;
        const trBadge = c.scanner_timer.triggered
            ? `<span style="color:var(--green);font-weight:700;">TRIG ✅</span>`
            : `<span style="color:var(--yellow);">WAIT ⏳</span>`;

        const tr = document.createElement("tr");
        tr.id = `row-${c.coin}`;
        if (appState.active && appState.coin === c.coin) {
            tr.style.background = "rgba(16, 185, 129, 0.10)";
        } else if (idx === 0) {
            tr.style.background = "rgba(56, 189, 248, 0.07)";
        }

        tr.innerHTML = `
            <td><b>#${idx + 1} ${c.coin}</b></td>
            <td id="cell-price-${c.coin}" style="font-weight:800;color:var(--accent);">${fmtPrice(curP, c.coin)}$</td>
            <td style="color:${chgCol};font-weight:600;">${c.change_24h >= 0 ? '+' : ''}${c.change_24h.toFixed(2)}%</td>
            <td><span style="background:${scCol}22;color:${scCol};padding:3px 8px;border-radius:6px;font-weight:700;">${sc}/10 (${c.raw_score})</span></td>
            <td>${(c.ai_prob * 100).toFixed(1)}%</td>
            <td><b>${c.time_machine.expected_r >= 0 ? '+' : ''}${c.time_machine.expected_r}R</b> (${c.time_machine.win_or_grid_prob.toFixed(0)}%)</td>
            <td>${trBadge}</td>
            <td>${rkBadge}</td>
            <td>${fmtPrice(c.lower_bound, c.coin)}$ - ${fmtPrice(c.upper_bound, c.coin)}$ (${c.optimal_grids}g)</td>
            <td><button class="btn-outline" style="padding:4px 10px;font-size:0.76rem;" onclick="selectCoinForManual('${c.coin}')">Usa</button></td>
        `;
        rankBody.appendChild(tr);
    });

    const histBody = document.getElementById("history-tbody");
    const histList = appState.trade_history || [];
    document.getElementById("closed-summary-badge").textContent = `${histList.length} operazioni registrate`;

    if (histList.length === 0) {
        histBody.innerHTML = `
            <tr>
                <td colspan="8" style="text-align:center;color:var(--muted);padding:20px;">
                    Nessuna posizione chiusa ancora registrata. Appena il bot o tu chiuderete una posizione, apparirà qui con il PnL realizzato.
                </td>
            </tr>
        `;
    } else {
        histBody.innerHTML = "";
        histList.forEach(h => {
            const pCol = (h.pnl_usdt || 0) >= 0 ? "var(--green)" : "var(--red)";
            const cName = h.coin || "SOL";
            const tr = document.createElement("tr");
            tr.innerHTML = `
                <td><b>${cName}</b> <span style="font-size:0.72rem;color:var(--muted);">(${h.mode || 'PAPER'})</span></td>
                <td>${h.opened_at || '-'}</td>
                <td>${h.closed_at || '-'}</td>
                <td>${fmtPrice(h.entry_price || 0, cName)}$ → <b>${fmtPrice(h.exit_price || 0, cName)}$</b></td>
                <td>${fmtPrice(h.lower || 0, cName)}$ - ${fmtPrice(h.upper || 0, cName)}$</td>
                <td>${h.fills || 0}</td>
                <td style="color:${pCol};font-weight:800;">${(h.pnl_usdt || 0) >= 0 ? '+' : ''}${Number(h.pnl_usdt || 0).toFixed(2)} USDT (${(h.pnl_pct || 0) >= 0 ? '+' : ''}${Number(h.pnl_pct || 0).toFixed(2)}%)</td>
                <td>${h.reason || '-'}</td>
            `;
            histBody.appendChild(tr);
        });
    }
}

const wsMap = {};
SNAPSHOT.ranking.forEach(c => { wsMap[c.ws_pair] = c.coin; });

function updateCoinPriceLive(coinName, newPrice) {
    if (!newPrice || isNaN(newPrice)) return;
    const oldPrice = livePrices[coinName] || newPrice;
    livePrices[coinName] = newPrice;

    renderAll();

    const cell = document.getElementById(`cell-price-${coinName}`);
    if (cell && newPrice !== oldPrice) {
        const cls = newPrice > oldPrice ? "flash-up" : "flash-down";
        cell.classList.add(cls);
        setTimeout(() => cell.classList.remove(cls), 450);
    }
}

function connectKrakenWebSocket() {
    try {
        const ws = new WebSocket("wss://ws.kraken.com");
        const pairs = SNAPSHOT.ranking.map(c => c.ws_pair);

        ws.onopen = () => {
            document.getElementById("ws-status").textContent = "LIVE STREAM KRAKEN ATTIVO (Tick-by-Tick)";
            ws.send(JSON.stringify({
                event: "subscribe",
                pair: pairs,
                subscription: { name: "ticker" }
            }));
        };

        ws.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                if (Array.isArray(data) && data.length >= 4) {
                    const pairName = data[data.length - 1];
                    const tickerObj = data[1];
                    if (tickerObj && tickerObj.c && tickerObj.c[0]) {
                        const coinName = wsMap[pairName];
                        if (coinName) {
                            updateCoinPriceLive(coinName, parseFloat(tickerObj.c[0]));
                        }
                    }
                }
            } catch (e) {}
        };

        ws.onclose = () => {
            document.getElementById("ws-status").textContent = "POLLING REST KRAKEN ATTIVO (5s)";
            setTimeout(connectKrakenWebSocket, 6000);
        };
    } catch (err) {
        document.getElementById("ws-status").textContent = "POLLING REST KRAKEN ATTIVO";
    }
}

async function forceRefreshPrices() {
    try {
        const pairsStr = SNAPSHOT.ranking.map(c => c.kraken_pair).join(",");
        const r = await fetch(`https://api.kraken.com/0/public/Ticker?pair=${pairsStr}`);
        const j = await r.json();
        if (j && j.result) {
            Object.keys(j.result).forEach(k => {
                const closeP = parseFloat(j.result[k].c[0]);
                SNAPSHOT.ranking.forEach(c => {
                    if (k.includes(c.kraken_pair) || k.includes(c.coin)) {
                        updateCoinPriceLive(c.coin, closeP);
                    }
                });
            });
        }
    } catch (e) {}
}

initManualForm();
renderAll();
connectKrakenWebSocket();
setInterval(forceRefreshPrices, 8000);
</script>
</body>
</html>
"""

    final_html = html_template.replace("__SNAPSHOT_JSON__", json_str)
    try:
        with open(WEBAPP_FILE, "w", encoding="utf-8") as f:
            f.write(final_html)
    except Exception as e:
        print(f"[WARN] Impossibile generare {WEBAPP_FILE}: {e}")


# ==========================================
# INVIO GARANTITO REPORT + CONSOLE WEB SU TELEGRAM
# ==========================================
def sanitize_telegram_html(text):
    placeholders = {
        "<b>": "___B_OPEN___", "</b>": "___B_CLOSE___",
        "<i>": "___I_OPEN___", "</i>": "___I_CLOSE___",
        "<code>": "___C_OPEN___", "</code>": "___C_CLOSE___"
    }
    for tag, ph in placeholders.items():
        text = text.replace(tag, ph)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag, ph in placeholders.items():
        text = text.replace(ph, tag)
    return text


def send_telegram_report(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN o TELEGRAM_CHAT_ID non configurati nei Secrets!")
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    safe_msg = sanitize_telegram_html(msg)
    if len(safe_msg) > 4000:
        safe_msg = safe_msg[:3990] + "\n..."
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": safe_msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=18)
        print(f"[TELEGRAM MSG STATUS] {r.status_code}")
        if r.status_code != 200:
            print(f"[WARN TELEGRAM HTML] {r.text} -> Invio fallback testo puro")
            clean_text = re.sub(r"<[^>]+>", "", msg)[:4000]
            r2 = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": clean_text}, timeout=18)
            print(f"[TELEGRAM FALLBACK STATUS] {r2.status_code}")
    except Exception as e:
        print(f"[ECCEZIONE TELEGRAM MSG] {e}")


def send_telegram_webapp(caption_text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not os.path.exists(WEBAPP_FILE):
        print("⚠️ Impossibile inviare il file WebApp: token/chat_id o index.html mancante.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    safe_caption = sanitize_telegram_html(caption_text)[:950]
    try:
        with open(WEBAPP_FILE, "rb") as f:
            files = {"document": ("Console_Live_AI_Bot.html", f, "text/html")}
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": safe_caption,
                "parse_mode": "HTML"
            }
            r = requests.post(url, data=data, files=files, timeout=25)
            print(f"[TELEGRAM FILE STATUS] {r.status_code}")
            if r.status_code != 200:
                print(f"[WARN TELEGRAM FILE] {r.text} -> Riprovo senza HTML")
                with open(WEBAPP_FILE, "rb") as f2:
                    files2 = {"document": ("Console_Live_AI_Bot.html", f2, "text/html")}
                    clean_cap = re.sub(r"<[^>]+>", "", caption_text)[:950]
                    r2 = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": clean_cap}, files=files2, timeout=25)
                    print(f"[TELEGRAM FILE FALLBACK STATUS] {r2.status_code}")
    except Exception as e:
        print(f"[ECCEZIONE TELEGRAM FILE] {e}")


# ==========================================
# ESECUZIONE PRINCIPALE MULTI-COIN SCANNER
# ==========================================
def main():
    eur_usd = fetch_eur_usd()
    invest_usdt = round(INVEST_EUR * eur_usd, 2)

    futures_map = fetch_all_kraken_futures_map()
    fng_val, fng_class = fetch_fear_and_greed()
    macro_events, macro_source = fetch_usd_macro_calendar()
    nyse_bell = check_nyse_market_bell()

    high_impact_urgent = any(ev["impact"] == "High" and -0.5 <= ev["hours_away"] <= 3.0 for ev in macro_events)
    high_impact_today = any(ev["impact"] == "High" and 3.0 < ev["hours_away"] <= 12.0 for ev in macro_events)

    ohlc_btc = fetch_kraken_ohlc("XBTUSD", 60)
    c_btc = [float(x[4]) for x in ohlc_btc] if ohlc_btc else [85000.0] * 60
    btc_price = c_btc[-1]
    btc_ema50_1h = ema_series(c_btc, 50)[-1]
    btc_rsi_1h = rsi_series(c_btc, 14)[-1]
    btc_ret_24h = (c_btc[-1] / c_btc[-25] - 1.0) * 100.0 if len(c_btc) >= 25 else 0.0
    btc_safe = (btc_price >= btc_ema50_1h * 0.992) and (btc_rsi_1h >= 40.0)

    global_ctx = {
        "eur_usd": eur_usd,
        "invest_usdt": invest_usdt,
        "futures_map": futures_map,
        "fng_val": fng_val,
        "fng_class": fng_class,
        "macro_events": macro_events,
        "macro_source": macro_source,
        "high_impact_urgent": high_impact_urgent,
        "high_impact_today": high_impact_today,
        "btc_price": btc_price,
        "btc_ema50_1h": btc_ema50_1h,
        "btc_rsi_1h": btc_rsi_1h,
        "btc_ret_24h": btc_ret_24h,
        "btc_safe": btc_safe
    }

    bot_state = load_bot_state()

    coins_results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_coin = {executor.submit(analyze_single_coin, cfg, global_ctx, bot_state): cfg for cfg in COIN_UNIVERSE}
        for fut in as_completed(future_to_coin):
            cfg = future_to_coin[fut]
            try:
                res = fut.result()
                coins_results.append(res)
            except Exception as e:
                print(f"[WARN] Errore analisi {cfg['name']}: {e}")

    if not coins_results:
        print("Errore critico: impossibile scaricare i dati per nessuna criptovaluta.")
        sys.exit(1)

    coins_results.sort(
        key=lambda x: (
            1 if x["risk_manager"]["passed"] else 0,
            x["final_score"],
            x["raw_score"],
            x["time_machine"]["expected_r"],
            x["ai_prob"]
        ),
        reverse=True
    )
    best_coin = coins_results[0]
    coins_map = {c["coin"]: c for c in coins_results}

    auto_action = manage_autonomous_grid_bot(
        bot_state=bot_state,
        coins_map=coins_map,
        best_coin_data=best_coin,
        invest_usdt=invest_usdt,
        high_impact_urgent=high_impact_urgent
    )

    generate_webapp_dashboard({
        "bot_state": bot_state,
        "auto_action": auto_action,
        "best_coin": best_coin,
        "ranking": coins_results,
        "coins_map": coins_map,
        "invest_usdt": invest_usdt,
        "nyse_bell": nyse_bell
    })

    rank_lines = []
    for idx, c in enumerate(coins_results, start=1):
        icon = "🟢" if c["final_score"] >= 7 else ("🟡" if c["final_score"] == 6 else "🔴")
        rk_ico = "✅" if c["risk_manager"]["passed"] else "⛔"
        tm_r = c["time_machine"]["expected_r"]
        rank_lines.append(
            f"{idx}. {icon} <b>{c['coin']}</b> ({fmt_p(c['price'])}$): <b>{c['final_score']}/10</b> ({c['raw_score']}pt) "
            f"| TM {tm_r:+.2f}R | Risk {rk_ico}"
        )
    ranking_block = "\n".join(rank_lines)

    if best_coin["final_score"] >= 8:
        semaforo = f"🟢 <b>SEMAFORO VERDE FORTE SU {best_coin['coin']} (OTTIMO PER APRIRE)</b>"
    elif best_coin["final_score"] == 7:
        semaforo = f"🟢 <b>SEMAFORO VERDE SU {best_coin['coin']} (CONDIZIONI FAVOREVOLI)</b>"
    elif best_coin["final_score"] == 6:
        semaforo = f"🟡 <b>SEMAFORO GIALLO (MIGLIORE: {best_coin['coin']} 6/10 - ATTESA)</b>"
    else:
        semaforo = "🔴 <b>SEMAFORO ROSSO SU TUTTO IL PANIERE (RESTARE IN USDT)</b>"

    if macro_events:
        ev_lines = []
        for ev in macro_events[:3]:
            ico = "🔴" if ev["impact"] == "High" else "🟠"
            ev_lines.append(f"  {ico} {ev['title']} ({ev['time_utc']} | tra {ev['hours_away']:+.1f}h)")
        macro_block = "\n".join(ev_lines)
    else:
        macro_block = "  ✅ Nessun evento USD ad alto impatto nelle prossime 48h"

    bd_lines = [f"• {k}: <b>{v}</b>" for k, v in best_coin["breakdown"].items()]
    breakdown_text = "\n".join(bd_lines)
    pen_text = ("\n⚠️ <b>Penalità:</b> " + "; ".join(best_coin["penalties"])) if best_coin["penalties"] else ""
    alert_banner = f"\n{auto_action['price_alert']}\n" if auto_action.get("price_alert") else ""

    tm_b = best_coin["time_machine"]
    ex_b = best_coin["exit_plan"]
    rk_b = best_coin["risk_manager"]
    st_b = best_coin["scanner_timer"]

    if bot_state.get("active"):
        manual_advice = f"🟢 <b>MANTIENI POSIZIONE SU {bot_state.get('coin')}</b> (Stop Loss attivo a {fmt_p(bot_state.get('stop_loss', 0))}$)"
    elif best_coin["final_score"] >= 7 and rk_b["passed"] and st_b["triggered"]:
        manual_advice = f"🚀 <b>PUOI APRIRE MANUALMENTE SU {best_coin['coin']}/USDT</b> (Range {fmt_p(best_coin['lower_bound'])}$ - {fmt_p(best_coin['upper_bound'])}$, {best_coin['optimal_grids']} griglie)"
    else:
        manual_advice = f"⏸️ <b>NON APRIRE MANUALMENTE ADESSO</b> (Migliore coin {best_coin['coin']} è a {best_coin['final_score']}/10, attendi almeno 7/10)"

    report_msg = (
        f"🧠 <b>MULTI-COIN NEURAL QUANT AI + CONSOLE LIVE</b>\n"
        f"{semaforo}\n"
        f"🧭 <b>Cosa fare ora:</b> {manual_advice}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>PILOTA AUTOMATICO ({bot_state.get('mode', 'PAPER')})</b>\n"
        f"<b>{auto_action['title']}</b>{alert_banner}\n"
        f"{auto_action['details']}\n"
        f"💵 PnL Attuale: <b>{auto_action['pnl_usdt']:+.2f} USDT ({auto_action['pnl_pct']:+.2f}%)</b> | "
        f"Storico Chiusi: <b>{bot_state.get('total_historical_pnl_usdt', 0.0):+.2f} USDT</b> ({bot_state.get('closed_trades_count', 0)} trade)\n\n"
        f"📊 <b>CLASSIFICA LIVE DELLE 10 CRIPTOVALUTE</b>\n"
        f"{ranking_block}\n\n"
        f"⏳ <b>TIME MACHINE (1.500 SIMS 24H) & RISK GATE ({best_coin['coin']})</b>\n"
        f"• Expected Value: <b>{tm_b['expected_r']:+.2f}R</b> | Prob. Profit/Grid: <b>{tm_b['win_or_grid_prob']:.1f}%</b>\n"
        f"• Prob. Target Prima: <b>{tm_b['target_first_prob']:.1f}%</b> | Prob. Stop Prima: <b>{tm_b['stop_first_prob']:.1f}%</b>\n"
        f"• Chart Scanner: <b>{st_b['pattern']}</b>\n"
        f"• Entry Timer 15m: <b>{st_b['timer_state']}</b>\n"
        f"• Risk & Budget Gate: <b>{rk_b['status']}</b> (Max Risk {rk_b['max_loss_usd']:.2f}$ / {rk_b['risk_pct']:.2f}%)\n\n"
        f"📐 <b>PIANO GRIGLIA & EXIT PLANNER ({best_coin['coin']} - {INVEST_EUR:.0f}€ ≈ {invest_usdt:.1f} USDT)</b>\n"
        f"• Range: <b>{fmt_p(best_coin['lower_bound'])}$ — {fmt_p(best_coin['upper_bound'])}$</b> ({best_coin['optimal_grids']} griglie)\n"
        f"• Step: <b>{fmt_p(best_coin['step_usd'])}$</b> (Netto <b>{best_coin['net_step_pct']:.2f}%</b>)\n"
        f"• Stop Loss (-1R): <b>{fmt_p(best_coin['stop_loss'])}$</b>\n"
        f"• Target +1R (Break-Even SL): <b>{fmt_p(ex_b['level_1r'])}$</b>\n"
        f"• Target +2R (Profit Trailing): <b>{fmt_p(ex_b['level_2r'])}$</b> (R/R <b>{ex_b['rr_ratio']:.2f}R</b>)\n\n"
        f"🌍 <b>WALL STREET BELL & CALENDARIO MACRO USD</b>\n"
        f"• NYSE Status: <b>{nyse_bell['status']}</b>\n"
        f"{macro_block}\n\n"
        f"📋 <b>PAGELLA DETTAGLIATA TOP PICK ({best_coin['coin']})</b>\n"
        f"{breakdown_text}{pen_text}"
    )

    send_telegram_report(report_msg)

    active_str = f"🟢 Posizione Attiva su {bot_state.get('coin', 'SOL')}" if bot_state.get("active") else f"⏸️ Standby (Top Pick: {best_coin['coin']} {best_coin['final_score']}/10)"
    caption = (
        f"🎛️ <b>Console Web Interattiva in Tempo Reale (Console_Live_AI_Bot.html)</b>\n"
        f"Stato: {active_str} | Stream Prezzi Kraken Live attivo\n"
        f"Aprila nel browser per vedere i prezzi in tempo reale, le posizioni aperte/chiuse e i pulsanti manuali."
    )
    send_telegram_webapp(caption)


if __name__ == "__main__":
    main()
