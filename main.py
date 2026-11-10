import os
import json
import math
import time
import hmac
import hashlib
import random
import traceback
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ==============================================================================
# CONFIGURAZIONE BASE E SECRETS
# ==============================================================================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or "").strip()
LIVE_TRADING = (os.getenv("LIVE_TRADING") or "false").strip().lower() == "true"

INVEST_EUR = 50.0
GRIDS = 20
PIONEX_FEE = 0.0005  # 0.05% per transazione su Pionex
STATE_FILE = "bot_state.json"

# ==============================================================================
# PARAMETRI RISK & BUDGET MANAGER (SKILL #2 & #6)
# ==============================================================================
MAX_LOSS_PCT_PER_TRADE = 3.6   # Perdita max tollerata su singola operazione (%)
DAILY_MAX_LOSS_PCT = 6.0       # Circuit breaker giornaliero (%)
MAX_TRADES_PER_DAY = 3         # Limite anti-overtrading giornaliero
MIN_REWARD_RISK_R = 1.35       # Rapporto R/R minimo per autorizzare l'ingresso
CASH_BUFFER_PCT = 5.0          # Riserva liquidità per commissioni/slippage (%)

# ==============================================================================
# PANIERE MULTI-COIN (Top 10 Crypto liquide su Pionex & Kraken)
# ==============================================================================
COIN_UNIVERSE = [
    {"coin": "SOL",  "kraken": "SOLUSD",  "ws": "SOL/USD",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT"},
    {"coin": "BTC",  "kraken": "XBTUSD",  "ws": "XBT/USD",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT"},
    {"coin": "ETH",  "kraken": "ETHUSD",  "ws": "ETH/USD",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT"},
    {"coin": "XRP",  "kraken": "XRPUSD",  "ws": "XRP/USD",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT"},
    {"coin": "SUI",  "kraken": "SUIUSD",  "ws": "SUI/USD",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT"},
    {"coin": "DOGE", "kraken": "XDGUSD",  "ws": "XDG/USD",  "futures": "PF_DOGEUSD", "pionex": "DOGE_USDT"},
    {"coin": "ADA",  "kraken": "ADAUSD",  "ws": "ADA/USD",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT"},
    {"coin": "AVAX", "kraken": "AVAXUSD", "ws": "AVAX/USD", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT"},
    {"coin": "LINK", "kraken": "LINKUSD", "ws": "LINK/USD", "futures": "PF_LINKUSD", "pionex": "LINK_USDT"},
    {"coin": "DOT",  "kraken": "DOTUSD",  "ws": "DOT/USD",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT"},
]


# ==============================================================================
# GESTIONE STATO AUTONOMO (bot_state.json)
# ==============================================================================
def load_state():
    default_state = {
        "active": False,
        "mode": "PAPER",
        "coin": "SOL",
        "symbol": "SOL_USDT",
        "entry_price": 0.0,
        "lower_bound": 0.0,
        "upper_bound": 0.0,
        "stop_loss": 0.0,
        "initial_stop_loss": 0.0,
        "one_r_usd": 0.0,
        "qty": 0.0,
        "invest_usdt": 0.0,
        "opened_at": "",
        "alert_1r_sent": False,
        "alert_2r_sent": False,
        "daily_date": "",
        "daily_pnl_usdt": 0.0,
        "daily_trades_count": 0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "cumulative_pnl_usdt": 0.0,
        "cumulative_pnl_pct": 0.0,
        "last_action": "Nessuna operazione recente",
        "history": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
        except Exception as e:
            print(f"[WARN] Impossibile leggere {STATE_FILE}: {e}")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if default_state.get("daily_date") != today_str:
        default_state["daily_date"] = today_str
        default_state["daily_pnl_usdt"] = 0.0
        default_state["daily_trades_count"] = 0

    return default_state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Impossibile salvare {STATE_FILE}: {e}")


# ==============================================================================
# PIONEX REST API (ESECUZIONE REALE OPZIONALE CON HMAC-SHA256)
# ==============================================================================
def pionex_Place_market_order(symbol, side, amount_or_size):
    if not LIVE_TRADING or not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return {"ok": False, "reason": "PAPER_MODE"}

    base_url = "https://api.pionex.com"
    path = "/api/v1/trade/order"
    ts = str(int(time.time() * 1000))
    query = f"timestamp={ts}"

    body_dict = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
    }
    if side.upper() == "BUY":
        body_dict["amount"] = f"{amount_or_size:.2f}"
    else:
        body_dict["size"] = f"{amount_or_size:.4f}"

    body_str = json.dumps(body_dict, separators=(",", ":"))
    sign_payload = f"POST{path}?{query}{body_str}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
        "Content-Type": "application/json"
    }
    try:
        url = f"{base_url}{path}?{query}"
        resp = requests.post(url, headers=headers, data=body_str, timeout=12)
        data = resp.json()
        if resp.status_code == 200 and data.get("result") is True:
            return {"ok": True, "data": data.get("data", {})}
        return {"ok": False, "reason": str(data)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ==============================================================================
# FUNZIONI MATEMATICHE, STATISTICHE ED ECONOMETRICHE
# ==============================================================================
def fmt_price(p):
    p = float(p)
    if p <= 0:
        return "0.00"
    if p >= 1000:
        return f"{p:.1f}"
    if p >= 10:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.4f}"


def sigmoid(z):
    z = max(-20.0, min(20.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi_series(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    first_rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    out = [50.0] * period + [first_rsi]

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


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
        avg = sum(trs) / len(trs)
        return [avg] * len(trs)
    out = [sum(trs[:period]) / period] * period
    prev = out[-1]
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def calc_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1:
        return 20.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    tr_s = sum(tr_list[:period])
    pdm_s = sum(plus_dm[:period])
    mdm_s = sum(minus_dm[:period])
    dx_list = []

    for i in range(period, len(tr_list)):
        tr_s = tr_s - (tr_s / period) + tr_list[i]
        pdm_s = pdm_s - (pdm_s / period) + plus_dm[i]
        mdm_s = mdm_s - (mdm_s / period) + minus_dm[i]
        if tr_s == 0:
            continue
        pdi = 100.0 * (pdm_s / tr_s)
        mdi = 100.0 * (mdm_s / tr_s)
        denom = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0
        dx_list.append(dx)

    if not dx_list:
        return 20.0
    adx = sum(dx_list[:period]) / min(len(dx_list), period)
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def calc_bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        c = closes[-1]
        return c * 0.97, c, c * 1.03, 6.0, 0.5
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = sma + mult * std
    lower = sma - mult * std
    bw_pct = ((upper - lower) / sma) * 100.0 if sma > 0 else 0.0
    pct_b = (closes[-1] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return lower, sma, upper, bw_pct, pct_b


def calc_hurst_exponent(closes, max_lag=20):
    if len(closes) < max_lag * 2:
        return 0.5
    lags = range(2, max_lag)
    tau = []
    valid_lags = []
    for lag in lags:
        diffs = [closes[i] - closes[i - lag] for i in range(lag, len(closes))]
        if not diffs:
            continue
        mean_d = sum(diffs) / len(diffs)
        var_d = sum((d - mean_d) ** 2 for d in diffs) / len(diffs)
        std_d = math.sqrt(var_d)
        if std_d > 0:
            tau.append(math.log(std_d))
            valid_lags.append(math.log(lag))
    if len(valid_lags) < 3:
        return 0.5
    n = len(valid_lags)
    mean_x = sum(valid_lags) / n
    mean_y = sum(tau) / n
    num = sum((valid_lags[i] - mean_x) * (tau[i] - mean_y) for i in range(n))
    den = sum((valid_lags[i] - mean_x) ** 2 for i in range(n))
    hurst = num / den if den > 0 else 0.5
    return max(0.1, min(0.9, hurst))


def calc_shannon_entropy(closes, window=40, bins=8):
    if len(closes) < window + 1:
        return 0.75
    rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0
    ]
    if not rets:
        return 0.75
    r_min, r_max = min(rets), max(rets)
    if r_max == r_min:
        return 0.5
    counts = [0] * bins
    for r in rets:
        idx = int((r - r_min) / (r_max - r_min) * (bins - 1))
        counts[idx] += 1
    n = len(rets)
    ent = 0.0
    for c in counts:
        if c > 0:
            p = c / n
            ent -= p * math.log2(p)
    max_ent = math.log2(bins)
    return ent / max_ent if max_ent > 0 else 0.75


def calc_vwap_and_poc(highs, lows, closes, volumes, lookback=72):
    n = min(len(closes), lookback)
    h_sub = highs[-n:]
    l_sub = lows[-n:]
    c_sub = closes[-n:]
    v_sub = volumes[-n:]

    total_vol = sum(v_sub)
    if total_vol <= 0:
        return closes[-1], closes[-1], 1.0
    vwap = sum(((h_sub[i] + l_sub[i] + c_sub[i]) / 3.0) * v_sub[i] for i in range(n)) / total_vol

    p_min, p_max = min(l_sub), max(h_sub)
    bins = 24
    if p_max <= p_min:
        poc = closes[-1]
    else:
        step = (p_max - p_min) / bins
        bucket = [0.0] * bins
        for i in range(n):
            tp = (h_sub[i] + l_sub[i] + c_sub[i]) / 3.0
            idx = min(bins - 1, max(0, int((tp - p_min) / step)))
            bucket[idx] += v_sub[i]
        best_idx = max(range(bins), key=lambda k: bucket[k])
        poc = p_min + (best_idx + 0.5) * step

    recent_vol = sum(v_sub[-6:]) / 6.0 if n >= 6 else v_sub[-1]
    avg_vol = total_vol / n
    rvol = recent_vol / avg_vol if avg_vol > 0 else 1.0
    return vwap, poc, rvol


# ==============================================================================
# ARCHITETTURA NEURALE IBRIDA (Lorentzian k-NN + Online Deep MLP)
# ==============================================================================
def build_feature_matrix(highs, lows, closes, volumes):
    n = len(closes)
    rsi_arr = rsi_series(closes, 14)
    ema20_arr = ema_series(closes, 20)
    ema50_arr = ema_series(closes, 50)
    atr_arr = atr_series(highs, lows, closes, 14)

    features = []
    for i in range(n):
        c = closes[i] if closes[i] > 0 else 1.0
        f_rsi = (rsi_arr[i] - 50.0) / 25.0
        f_ema20 = ((c - ema20_arr[i]) / c) * 25.0
        f_ema50 = ((c - ema50_arr[i]) / c) * 15.0
        f_atr = ((atr_arr[i] / c) - 0.015) * 40.0

        ret_4 = ((c - closes[i - 4]) / closes[i - 4]) * 20.0 if i >= 4 else 0.0
        v_start = max(0, i - 20)
        v_win = volumes[v_start:i + 1]
        v_mean = sum(v_win) / len(v_win) if v_win else 1.0
        f_vol = (volumes[i] / v_mean - 1.0) if v_mean > 0 else 0.0

        features.append([
            max(-3.0, min(3.0, f_rsi)),
            max(-3.0, min(3.0, f_ema20)),
            max(-3.0, min(3.0, f_ema50)),
            max(-3.0, min(3.0, f_atr)),
            max(-3.0, min(3.0, ret_4)),
            max(-3.0, min(3.0, f_vol))
        ])
    return features, rsi_arr, ema20_arr, ema50_arr, atr_arr


def run_hybrid_neural_engine(features, highs, lows, closes, atr_arr, horizon=12):
    n = len(closes)
    if n < 120:
        return 0.5, 0.5, 0.5, 50.0

    X_train, y_train = [], []
    for i in range(55, n - horizon):
        c_now = closes[i]
        atr_now = max(atr_arr[i], c_now * 0.005)
        future_high = max(highs[i + 1:i + 1 + horizon])
        future_low = min(lows[i + 1:i + 1 + horizon])
        future_close = closes[i + horizon]

        up_exc = (future_high - c_now) / atr_now
        dn_exc = (c_now - future_low) / atr_now

        label = 1.0 if (dn_exc < 2.2 and (up_exc >= 0.9 or future_close >= c_now * 0.992)) else 0.0
        X_train.append(features[i])
        y_train.append(label)

    x_curr = features[-1]

    # 1. Lorentzian Distance Classifier (k-NN nello spazio spazio-temporale)
    distances = []
    for idx, x_hist in enumerate(X_train):
        d = sum(math.log(1.0 + abs(x_curr[j] - x_hist[j])) for j in range(len(x_curr)))
        distances.append((d, y_train[idx]))
    distances.sort(key=lambda t: t[0])
    k = min(25, len(distances))
    knn_votes = [ y for _, y in distances[:k] ]
    lorentzian_prob = sum(knn_votes) / k if k > 0 else 0.5

    # 2. Multi-Layer Perceptron (Rete Neurale 6 -> 8 -> 1 con Backpropagation live)
    rng = random.Random(42)
    n_in = len(x_curr)
    n_hid = 8
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(n_in)] for _ in range(n_hid)]
    b1 = [0.0] * n_hid
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(n_hid)]
    b2 = 0.0

    lr = 0.035
    epochs = 28
    for ep in range(epochs):
        step_lr = lr * (0.95 ** ep)
        for x_vec, target in zip(X_train, y_train):
            h = [math.tanh(sum(W1[m][j] * x_vec[j] for j in range(n_in)) + b1[m]) for m in range(n_hid)]
            z2 = sum(W2[m] * h[m] for m in range(n_hid)) + b2
            pred = sigmoid(z2)

            dz2 = pred - target
            for m in range(n_hid):
                grad_w2 = dz2 * h[m]
                dh = dz2 * W2[m] * (1.0 - h[m] ** 2)
                W2[m] -= step_lr * (grad_w2 + 0.001 * W2[m])
                b2 -= step_lr * dz2 * 0.1
                for j in range(n_in):
                    W1[m][j] -= step_lr * (dh * x_vec[j] + 0.001 * W1[m][j])
                b1[m] -= step_lr * dh

    h_curr = [math.tanh(sum(W1[m][j] * x_curr[j] for j in range(n_in)) + b1[m]) for m in range(n_hid)]
    mlp_prob = sigmoid(sum(W2[m] * h_curr[m] for m in range(n_hid)) + b2)

    ensemble_prob = 0.52 * lorentzian_prob + 0.48 * mlp_prob
    agreement = 1.0 - abs(lorentzian_prob - mlp_prob)
    confidence_pct = min(99.0, max(10.0, (abs(ensemble_prob - 0.5) * 200.0) * 0.65 + agreement * 35.0))
    return ensemble_prob, lorentzian_prob, mlp_prob, confidence_pct


# ==============================================================================
# SKILL #9: TIME MACHINE (Monte Carlo 1.500 scenari + Replay Storico 320h)
# ==============================================================================
def run_time_machine(closes, highs, lows, lower_bound, upper_bound, stop_loss, horizon_hours=24, n_sims=1500):
    c_now = closes[-1]
    if len(closes) < 50 or c_now <= 0:
        return {
            "target_first_prob": 50.0,
            "stop_first_prob": 25.0,
            "in_range_prob": 25.0,
            "win_or_grid_prob": 75.0,
            "expected_r": 0.5
        }

    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(max(1, len(closes) - 180), len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / max(1, len(rets) - 1)
    std_r = max(0.0015, math.sqrt(var_r))

    bar_shocks = []
    for i in range(max(1, len(closes) - 180), len(closes)):
        if closes[i - 1] > 0:
            hi_rel = (highs[i] - closes[i - 1]) / closes[i - 1]
            lo_rel = (lows[i] - closes[i - 1]) / closes[i - 1]
            cl_rel = (closes[i] - closes[i - 1]) / closes[i - 1]
            bar_shocks.append((hi_rel, lo_rel, cl_rel))

    rng = random.Random(int(c_now * 1000) + len(closes))
    mc_target, mc_stop, mc_range = 0, 0, 0

    for _ in range(n_sims):
        p = c_now
        outcome = "RANGE"
        for _ in range(horizon_hours):
            if bar_shocks and rng.random() < 0.65:
                hi_rel, lo_rel, cl_rel = rng.choice(bar_shocks)
                p_high = p * (1.0 + hi_rel)
                p_low = p * (1.0 + lo_rel)
                p_next = p * (1.0 + cl_rel)
            else:
                z = rng.gauss(mean_r * 0.4, std_r)
                p_next = p * math.exp(z)
                p_high = max(p, p_next) * (1.0 + abs(rng.gauss(0, std_r * 0.55)))
                p_low = min(p, p_next) * (1.0 - abs(rng.gauss(0, std_r * 0.55)))

            if p_low <= stop_loss:
                outcome = "STOP"
                break
            if p_high >= upper_bound:
                outcome = "TARGET"
                break
            p = p_next

        if outcome == "TARGET":
            mc_target += 1
        elif outcome == "STOP":
            mc_stop += 1
        else:
            mc_range += 1

    up_target_pct = (upper_bound - c_now) / c_now
    stop_dist_pct = (c_now - stop_loss) / c_now
    hist_target, hist_stop, hist_range, hist_total = 0, 0, 0, 0

    start_idx = max(20, len(closes) - 320)
    for i in range(start_idx, len(closes) - horizon_hours):
        entry = closes[i]
        if entry <= 0:
            continue
        t_price = entry * (1.0 + up_target_pct)
        s_price = entry * (1.0 - stop_dist_pct)
        outcome = "RANGE"
        for h in range(1, horizon_hours + 1):
            if lows[i + h] <= s_price:
                outcome = "STOP"
                break
            if highs[i + h] >= t_price:
                outcome = "TARGET"
                break
        hist_total += 1
        if outcome == "TARGET":
            hist_target += 1
        elif outcome == "STOP":
            hist_stop += 1
        else:
            hist_range += 1

    mc_t_prob = (mc_target / n_sims) * 100.0
    mc_s_prob = (mc_stop / n_sims) * 100.0
    mc_r_prob = (mc_range / n_sims) * 100.0

    if hist_total > 10:
        h_t_prob = (hist_target / hist_total) * 100.0
        h_s_prob = (hist_stop / hist_total) * 100.0
        h_r_prob = (hist_range / hist_total) * 100.0
        target_first_prob = 0.60 * mc_t_prob + 0.40 * h_t_prob
        stop_first_prob = 0.60 * mc_s_prob + 0.40 * h_s_prob
        in_range_prob = 0.60 * mc_r_prob + 0.40 * h_r_prob
    else:
        target_first_prob, stop_first_prob, in_range_prob = mc_t_prob, mc_s_prob, mc_r_prob

    win_or_grid_prob = target_first_prob + in_range_prob
    rr = up_target_pct / stop_dist_pct if stop_dist_pct > 0 else 1.5
    expected_r = (target_first_prob / 100.0) * rr + (in_range_prob / 100.0) * 0.45 - (stop_first_prob / 100.0) * 1.0

    return {
        "target_first_prob": target_first_prob,
        "stop_first_prob": stop_first_prob,
        "in_range_prob": in_range_prob,
        "win_or_grid_prob": win_or_grid_prob,
        "expected_r": expected_r
    }


# ==============================================================================
# SKILL #4: EXIT PLANNER (1R, +1R Break-Even, +2R Lock-In, Spread Check)
# ==============================================================================
def run_exit_planner(price, lower_bound, upper_bound, stop_loss, spread_bps):
    one_r_usd = max(price * 0.005, price - stop_loss)
    one_r_pct = (one_r_usd / price) * 100.0 if price > 0 else 1.0
    reward_usd = max(0.0, upper_bound - price)
    rr_ratio = reward_usd / one_r_usd if one_r_usd > 0 else 0.0

    level_1r = price + one_r_usd          # A +1R -> Stop spostato a Break-Even
    level_2r = price + 2.0 * one_r_usd    # A +2R -> Trailing Stop spostato a +1R
    level_3r = upper_bound                # Target superiore della griglia

    spread_usd = price * (spread_bps / 10000.0)
    spread_ok = one_r_usd >= (spread_usd * 5.0)

    return {
        "one_r_usd": one_r_usd,
        "one_r_pct": one_r_pct,
        "reward_usd": reward_usd,
        "rr_ratio": rr_ratio,
        "level_1r": level_1r,
        "level_2r": level_2r,
        "level_3r": level_3r,
        "spread_ok": spread_ok
    }


# ==============================================================================
# SKILL #3 & #8: CHART SCANNER & ENTRY TIMER (Pattern + Conferma 15m chiusa)
# ==============================================================================
def run_chart_scanner_and_entry_timer(h15, l15, c15, rsi_1h, rsi_15m, ema20_1h, vwap_1h, rvol):
    if len(c15) < 5:
        return {
            "pattern": "Range Consolidation",
            "triggered": True,
            "status_str": "TRIGGERED ✅",
            "bonus_pts": 2
        }

    # Usiamo la penultima candela 15m (-2) perché è 100% chiusa e confermata
    prev_c = c15[-3]
    closed_c = c15[-2]
    closed_l = l15[-2]
    closed_h = h15[-2]
    curr_c = c15[-1]

    candle_range = max(1e-9, closed_h - closed_l)
    close_pos_in_bar = (closed_c - closed_l) / candle_range

    pattern = "Nessun Setup Netto"
    triggered = False
    bonus_pts = 0

    if rsi_1h <= 46 and closed_c > prev_c and close_pos_in_bar >= 0.55:
        pattern = "Oversold Support Bounce (Rimbalzo Confermato 15m)"
        triggered = True
        bonus_pts = 4
    elif curr_c >= ema20_1h and closed_l <= ema20_1h * 1.004 and closed_c >= ema20_1h:
        pattern = "Bull Flag / Trend Pullback su EMA20"
        triggered = True
        bonus_pts = 4
    elif prev_c < ema20_1h and closed_c >= ema20_1h and rvol >= 0.85:
        pattern = "EMA20 Bullish Reclaim (Breakout + RVOL)"
        triggered = True
        bonus_pts = 3
    elif 35 <= rsi_1h <= 60 and close_pos_in_bar >= 0.45 and curr_c >= closed_l:
        pattern = "Mean-Reverting Grid Equilibrium"
        triggered = True
        bonus_pts = 2
    else:
        pattern = "Falling Knife / Candela 15m Debole"
        triggered = False
        bonus_pts = -3

    status_str = "TRIGGERED ✅ (Candela 15m confermata)" if triggered else "WAITING ⏳ (Attesa chiusura 15m)"
    return {
        "pattern": pattern,
        "triggered": triggered,
        "status_str": status_str,
        "bonus_pts": bonus_pts
    }


# ==============================================================================
# SKILL #2 & #6: BUDGET MANAGER & RISK MANAGER (Gatekeeper Pre-Trade)
# ==============================================================================
def run_budget_and_risk_manager(state, total_capital_usdt, exit_plan, macro_hours, spread_bps):
    reasons = []
    passed = True

    deployable_usdt = round(total_capital_usdt * (1.0 - CASH_BUFFER_PCT / 100.0), 2)
    cash_reserve_usdt = round(total_capital_usdt - deployable_usdt, 2)

    if exit_plan["one_r_pct"] > MAX_LOSS_PCT_PER_TRADE:
        passed = False
        reasons.append(f"Rischio Stop ({exit_plan['one_r_pct']:.2f}%) oltre max ({MAX_LOSS_PCT_PER_TRADE:.1f}%)")

    if exit_plan["rr_ratio"] < MIN_REWARD_RISK_R:
        passed = False
        reasons.append(f"Reward/Risk ({exit_plan['rr_ratio']:.2f}R) sotto min ({MIN_REWARD_RISK_R:.2f}R)")

    daily_loss_limit_usdt = -(total_capital_usdt * (DAILY_MAX_LOSS_PCT / 100.0))
    if state.get("daily_pnl_usdt", 0.0) <= daily_loss_limit_usdt:
        passed = False
        reasons.append(f"Max Daily Loss raggiunto ({state['daily_pnl_usdt']:.2f}$)")

    if state.get("daily_trades_count", 0) >= MAX_TRADES_PER_DAY:
        passed = False
        reasons.append(f"Max trade giornalieri raggiunto ({MAX_TRADES_PER_DAY}/{MAX_TRADES_PER_DAY})")

    if not exit_plan["spread_ok"] or spread_bps > 28.0:
        passed = False
        reasons.append(f"Spread eccessivo ({spread_bps:.1f} bps)")

    if macro_hours is not None and -0.5 <= macro_hours <= 3.0:
        passed = False
        reasons.append("Evento Macro USD High-Impact imminente (entro 3h)")

    status_badge = "PASS ✅" if passed else "BLOCK ⛔"
    reason_str = "Tutti i 6 controlli Risk/Budget superati" if passed else " | ".join(reasons)

    return {
        "passed": passed,
        "status_badge": status_badge,
        "reason_str": reason_str,
        "deployable_usdt": deployable_usdt,
        "cash_reserve_usdt": cash_reserve_usdt
    }


# ==============================================================================
# SKILL #1: MARKET BELL & PRICE ALERTS (Wall Street 09:30 NY Open)
# ==============================================================================
def check_nyse_market_bell():
    ny_now = datetime.now(ZoneInfo("America/New_York"))
    is_weekday = ny_now.weekday() < 5
    minutes_now = ny_now.hour * 60 + ny_now.minute
    open_min = 9 * 60 + 30    # 09:30 NY
    close_min = 16 * 60       # 16:00 NY

    if not is_weekday:
        return "Weekend (Wall Street Chiusa - Liquidità Crypto Pura)"
    if abs(minutes_now - open_min) <= 35:
        return "🔔 NYSE OPENING BELL ATTIVA (09:30 NY - Picco Volatilità USA)"
    if open_min < minutes_now < close_min:
        return "🟢 Sessione USA Aperta (Alta Liquidità Istituzionale)"
    return "🌙 Sessione Asiatica / Europea (Pre/After-Hours USA)"


# ==============================================================================
# RACCOLTA DATI MULTI-SORGENTE CON RETRY E ZERO RATE-LIMIT
# ==============================================================================
def fetch_eur_usd_rate():
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=EURUSD"
        resp = requests.get(url, timeout=10).json()
        pair_key = list(resp["result"].keys())[0]
        return float(resp["result"][pair_key]["c"][0])
    except Exception:
        return 1.08


def fetch_kraken_ohlc(pair="SOLUSD", interval=60):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=12).json()
            if resp.get("error"):
                time.sleep(0.4 * (attempt + 1))
                continue
            result = resp.get("result", {})
            pair_key = [k for k in result.keys() if k != "last"][0]
            rows = result[pair_key]
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            closes = [float(r[4]) for r in rows]
            volumes = [float(r[6]) for r in rows]
            if closes:
                return highs, lows, closes, volumes
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"OHLC non disponibile per {pair} ({interval}m)")


def build_4h_from_1h(h1, l1, c1):
    h4, l4, c4 = [], [], []
    n = len(c1)
    start = n % 4
    for i in range(start, n, 4):
        chunk_h = h1[i:i + 4]
        chunk_l = l1[i:i + 4]
        chunk_c = c1[i:i + 4]
        if chunk_c:
            h4.append(max(chunk_h))
            l4.append(min(chunk_l))
            c4.append(chunk_c[-1])
    return h4, l4, c4


def fetch_kraken_orderbook_depth(pair="SOLUSD", count=50):
    url = f"https://api.kraken.com/0/public/Depth?pair={pair}&count={count}"
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=10).json()
            if resp.get("error"):
                time.sleep(0.3)
                continue
            result = resp.get("result", {})
            pair_key = list(result.keys())[0]
            bids = result[pair_key]["bids"]
            asks = result[pair_key]["asks"]

            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            mid = (best_bid + best_ask) / 2.0
            spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 5.0

            bid_liq, ask_liq = 0.0, 0.0
            for b in bids:
                p, v = float(b[0]), float(b[1])
                dist = max(0.0005, (mid - p) / mid)
                bid_liq += (p * v) * math.exp(-dist * 120.0)
            for a in asks:
                p, v = float(a[0]), float(a[1])
                dist = max(0.0005, (p - mid) / mid)
                ask_liq += (p * v) * math.exp(-dist * 120.0)

            tot = bid_liq + ask_liq
            imbalance = (bid_liq - ask_liq) / tot if tot > 0 else 0.0
            ratio = bid_liq / ask_liq if ask_liq > 0 else 1.0
            return imbalance, ratio, spread_bps
        except Exception:
            time.sleep(0.3)
    return 0.0, 1.0, 5.0


def fetch_all_futures_map():
    f_map = {}
    try:
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        resp = requests.get(url, timeout=10).json()
        for t in resp.get("tickers", []):
            sym = t.get("symbol", "").upper()
            fr = float(t.get("fundingRate", 0.0))
            mark = float(t.get("markPrice", 1.0))
            if abs(fr) > 0.01 and mark > 1.0:
                fr = fr / mark
            f_map[sym] = {
                "funding_pct_8h": fr * 100.0,
                "open_interest": float(t.get("openInterest", 0.0))
            }
    except Exception as e:
        print(f"[WARN] Futures map fallback: {e}")
    return f_map


def fetch_fear_and_greed():
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        resp = requests.get(url, timeout=10).json()
        item = resp["data"][0]
        return int(item["value"]), item["value_classification"]
    except Exception:
        return 50, "Neutral"


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    events = []
    try:
        url_json = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url_json, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            for ev in data:
                if ev.get("country") == "USD" and ev.get("impact") in ("High", "Medium"):
                    dt_str = ev.get("date", "")
                    if dt_str:
                        ev_dt = datetime.fromisoformat(dt_str).astimezone(timezone.utc)
                        diff_h = (ev_dt - now_utc).total_seconds() / 3600.0
                        if -2.0 <= diff_h <= 36.0:
                            events.append((diff_h, ev.get("impact"), ev.get("title", "USD Event")))
    except Exception:
        pass

    if not events:
        try:
            url_xml = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
            resp = requests.get(url_xml, timeout=10)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                for ev in root.findall("event"):
                    country = (ev.findtext("country") or "").strip()
                    impact = (ev.findtext("impact") or "").strip()
                    title = (ev.findtext("title") or "").strip()
                    date_s = (ev.findtext("date") or "").strip()
                    time_s = (ev.findtext("time") or "").strip()
                    if country == "USD" and impact in ("High", "Medium") and date_s and time_s:
                        dt_parsed = datetime.strptime(f"{date_s} {time_s}", "%m-%d-%Y %I:%M%p")
                        dt_utc = dt_parsed.replace(tzinfo=timezone.utc)
                        diff_h = (dt_utc - now_utc).total_seconds() / 3600.0
                        if -2.0 <= diff_h <= 36.0:
                            events.append((diff_h, impact, title))
        except Exception:
            pass

    events.sort(key=lambda x: abs(x[0]))
    return events


# ==============================================================================
# MOTORE DI SCORING QUANTITATIVO + 5 SKILL INTEGRATE (SCALA 0-100 -> 1-10)
# ==============================================================================
def compute_quant_score(
    price, ema50_4h, ema200_4h, rsi_4h, rsi_1h, rsi_15m,
    adx_1h, bb_width_pct, bb_pct_b, atr_pct,
    hurst, entropy, vwap, poc, rvol,
    ob_imbalance, spread_bps, funding_pct, fng_val, macro_events,
    ai_prob, ai_conf, btc_trend_bull,
    time_machine, scanner_timer, risk_manager
):
    breakdown = {}

    # 1. Regime Tecnico & MTF + Chart Scanner & Entry Timer (Max 24 pt)
    tech_pts = 0
    if price >= ema50_4h and ema50_4h >= ema200_4h * 0.99:
        tech_pts += 8
    elif price >= ema200_4h * 0.985:
        tech_pts += 6
    elif price >= ema50_4h * 0.975:
        tech_pts += 4
    else:
        tech_pts += 2

    if 36 <= rsi_1h <= 58:
        tech_pts += 7
    elif 28 <= rsi_1h < 36:
        tech_pts += 6 if rsi_15m >= rsi_1h else 4
    elif 58 < rsi_1h <= 66:
        tech_pts += 4
    else:
        tech_pts += 1

    if adx_1h < 22:
        tech_pts += 5
    elif adx_1h <= 30:
        tech_pts += 4
    elif adx_1h <= 38:
        tech_pts += 2
    else:
        tech_pts += 1

    if not btc_trend_bull and price < ema50_4h:
        tech_pts = max(1, tech_pts - 2)

    tech_pts = max(0, min(24, tech_pts + scanner_timer["bonus_pts"]))
    breakdown["tech"] = tech_pts

    # 2. Econometria & Volume Profile (Max 16 pt)
    econ_pts = 0
    if hurst < 0.48:
        econ_pts += 7
    elif hurst <= 0.55:
        econ_pts += 5
    else:
        econ_pts += 2

    if entropy < 0.82:
        econ_pts += 4
    elif entropy <= 0.90:
        econ_pts += 3
    else:
        econ_pts += 1

    dist_poc_pct = abs(price - poc) / price * 100.0 if price > 0 else 5.0
    if dist_poc_pct <= 1.8 or abs(price - vwap) / price * 100.0 <= 1.5:
        econ_pts += 5
    elif dist_poc_pct <= 3.2:
        econ_pts += 3
    else:
        econ_pts += 2
    breakdown["econ"] = min(16, econ_pts)

    # 3. Microstruttura: L2 Orderbook & Futures Funding (Max 18 pt)
    micro_pts = 0
    if ob_imbalance >= 0.12:
        micro_pts += 9
    elif ob_imbalance >= -0.05:
        micro_pts += 7
    elif ob_imbalance >= -0.18:
        micro_pts += 4
    else:
        micro_pts += 1

    if spread_bps <= 8.0:
        micro_pts += 3
    elif spread_bps <= 18.0:
        micro_pts += 2
    else:
        micro_pts += 0

    if -0.015 <= funding_pct <= 0.015:
        micro_pts += 6
    elif funding_pct < -0.015:
        micro_pts += 5
    elif funding_pct <= 0.035:
        micro_pts += 3
    else:
        micro_pts += 1
    breakdown["micro"] = min(18, micro_pts)

    # 4. Hybrid Neural AI Ensemble (Max 18 pt)
    if ai_prob >= 0.64:
        ai_pts = 18
    elif ai_prob >= 0.58:
        ai_pts = 15
    elif ai_prob >= 0.53:
        ai_pts = 12
    elif ai_prob >= 0.48:
        ai_pts = 9
    elif ai_prob >= 0.43:
        ai_pts = 5
    else:
        ai_pts = 2
    breakdown["ai"] = ai_pts

    # 5. Skill #9: Time Machine Simulator (Max 12 pt)
    w_prob = time_machine["win_or_grid_prob"]
    exp_r = time_machine["expected_r"]
    if w_prob >= 76.0 and exp_r >= 0.45:
        tm_pts = 12
    elif w_prob >= 68.0 and exp_r >= 0.25:
        tm_pts = 10
    elif w_prob >= 60.0 and exp_r >= 0.10:
        tm_pts = 8
    elif w_prob >= 52.0:
        tm_pts = 5
    else:
        tm_pts = 2
    breakdown["timemachine"] = tm_pts

    # 6. Macro & Sentiment (Max 12 pt)
    macro_pts = 8
    high_imminent = False
    for diff_h, impact, _ in macro_events:
        if impact == "High" and -0.5 <= diff_h <= 3.0:
            macro_pts = 1
            high_imminent = True
            break
        elif impact == "High" and 3.0 < diff_h <= 12.0:
            macro_pts = min(macro_pts, 5)
        elif impact == "Medium" and 0.0 <= diff_h <= 3.0:
            macro_pts = min(macro_pts, 6)

    if 22 <= fng_val <= 68:
        sent_pts = 4
    elif fng_val < 22:
        sent_pts = 3
    else:
        sent_pts = 2
    breakdown["macro"] = min(12, macro_pts + sent_pts)

    raw_100 = sum(breakdown.values())
    final_score = max(1, min(10, int(round(raw_100 / 10.0))))

    if high_imminent:
        final_score = min(final_score, 4)
    if not risk_manager["passed"]:
        final_score = min(final_score, 5)

    return final_score, raw_100, breakdown


# ==============================================================================
# ANALISI SINGOLA COIN (Eseguita per ciascuna delle 10 Crypto)
# ==============================================================================
def analyze_single_coin(coin_cfg, futures_map, fng_val, macro_events, invest_usdt, btc_trend_bull, state):
    coin = coin_cfg["coin"]
    k_pair = coin_cfg["kraken"]
    f_sym = coin_cfg["futures"]
    p_sym = coin_cfg["pionex"]

    h1, l1, c1, v1 = fetch_kraken_ohlc(k_pair, 60)
    h15, l15, c15, v15 = fetch_kraken_ohlc(k_pair, 15)
    h4, l4, c4 = build_4h_from_1h(h1, l1, c1)

    ob_imbalance, ob_ratio, spread_bps = fetch_kraken_orderbook_depth(k_pair, 50)
    f_info = futures_map.get(f_sym, {"funding_pct_8h": 0.005, "open_interest": 0.0})
    funding_pct = f_info["funding_pct_8h"]

    price = c1[-1]
    ema50_4h = ema_series(c4, 50)[-1]
    ema200_4h = ema_series(c4, min(200, len(c4)))[-1]
    rsi_4h = rsi_series(c4, 14)[-1]
    rsi_15m = rsi_series(c15, 14)[-1]
    adx_1h = calc_adx(h1, l1, c1, 14)
    _, _, _, bb_width_pct, bb_pct_b = calc_bollinger(c1, 20, 2.0)

    hurst = calc_hurst_exponent(c1, 20)
    entropy = calc_shannon_entropy(c1, 40, 8)
    vwap, poc, rvol = calc_vwap_and_poc(h1, l1, c1, v1, 72)

    features, rsi_1h_arr, ema20_1h_arr, ema50_1h_arr, atr_1h_arr = build_feature_matrix(h1, l1, c1, v1)
    rsi_1h = rsi_1h_arr[-1]
    ema20_1h = ema20_1h_arr[-1]
    atr_1h = atr_1h_arr[-1]
    atr_pct = (atr_1h / price) * 100.0 if price > 0 else 1.5

    ai_prob, lor_prob, mlp_prob, ai_conf = run_hybrid_neural_engine(features, h1, l1, c1, atr_1h_arr, horizon=12)

    # Calcolo Range Dinamico Ottimale per Griglia Pionex
    half_span_pct = max(0.032, min(0.072, (atr_pct / 100.0) * 3.1))
    skew = 1.05 if ai_prob >= 0.55 else (0.95 if ai_prob < 0.45 else 1.0)
    lower_bound = price * (1.0 - half_span_pct / skew)
    upper_bound = price * (1.0 + half_span_pct * skew)

    # Stop Loss calibrato tra il pavimento della griglia e il tetto Risk Manager (3.6%)
    raw_stop = lower_bound * (1.0 - max(0.008, (atr_pct / 100.0) * 0.55))
    min_allowed_stop = price * (1.0 - (MAX_LOSS_PCT_PER_TRADE - 0.15) / 100.0)
    stop_loss = max(raw_stop, min_allowed_stop)

    grid_step_pct = ((upper_bound - lower_bound) / price) * 100.0 / GRIDS
    net_grid_profit_pct = grid_step_pct - (PIONEX_FEE * 200.0)

    # Esecuzione delle 5 Skill
    exit_plan = run_exit_planner(price, lower_bound, upper_bound, stop_loss, spread_bps)
    time_machine = run_time_machine(c1, h1, l1, lower_bound, upper_bound, stop_loss, horizon_hours=24, n_sims=1500)
    scanner_timer = run_chart_scanner_and_entry_timer(h15, l15, c15, rsi_1h, rsi_15m, ema20_1h, vwap, rvol)
    nearest_macro_h = macro_events[0][0] if macro_events else None
    risk_manager = run_budget_and_risk_manager(state, invest_usdt, exit_plan, nearest_macro_h, spread_bps)

    final_score, raw_100, breakdown = compute_quant_score(
        price, ema50_4h, ema200_4h, rsi_4h, rsi_1h, rsi_15m,
        adx_1h, bb_width_pct, bb_pct_b, atr_pct,
        hurst, entropy, vwap, poc, rvol,
        ob_imbalance, spread_bps, funding_pct, fng_val, macro_events,
        ai_prob, ai_conf, btc_trend_bull,
        time_machine, scanner_timer, risk_manager
    )

    return {
        "coin": coin,
        "kraken_pair": k_pair,
        "ws_pair": coin_cfg["ws"],
        "pionex_symbol": p_sym,
        "price": price,
        "ema50_4h": ema50_4h,
        "ema200_4h": ema200_4h,
        "rsi_4h": rsi_4h,
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "adx_1h": adx_1h,
        "bb_width_pct": bb_width_pct,
        "bb_pct_b": bb_pct_b,
        "atr_pct": atr_pct,
        "hurst": hurst,
        "entropy": entropy,
        "vwap": vwap,
        "poc": poc,
        "rvol": rvol,
        "ob_imbalance": ob_imbalance,
        "ob_ratio": ob_ratio,
        "spread_bps": spread_bps,
        "funding_pct": funding_pct,
        "ai_prob": ai_prob,
        "lor_prob": lor_prob,
        "mlp_prob": mlp_prob,
        "ai_conf": ai_conf,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "stop_loss": stop_loss,
        "grid_step_pct": grid_step_pct,
        "net_grid_profit_pct": net_grid_profit_pct,
        "exit_plan": exit_plan,
        "time_machine": time_machine,
        "scanner_timer": scanner_timer,
        "risk_manager": risk_manager,
        "final_score": final_score,
        "raw_100": raw_100,
        "breakdown": breakdown
    }


# ==============================================================================
# MACCHINA A STATI AUTONOMA (Apertura, +1R Break-Even, +2R Lock-In, Chiusura)
# ==============================================================================
def manage_autonomous_position(state, best_coin_data, all_coins_map, macro_events):
    now_rome = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")
    high_macro_imminent = any(imp == "High" and -0.5 <= dh <= 2.0 for dh, imp, _ in macro_events)
    alerts_fired = []

    # CASO 1: POSIZIONE ATTIVA -> MONITORAGGIO + SKILL #1 & #4 (Dynamic Exit Planner)
    if state["active"]:
        active_coin = state.get("coin", "SOL")
        coin_data = all_coins_map.get(active_coin, best_coin_data)

        price = coin_data["price"]
        final_score = coin_data["final_score"]
        ai_prob = coin_data["ai_prob"]

        entry = state["entry_price"]
        lower = state["lower_bound"]
        upper = state["upper_bound"]
        stop = state["stop_loss"]
        one_r = state.get("one_r_usd", max(entry * 0.015, entry - stop))
        qty = state["qty"]
        invest = state["invest_usdt"]

        # SKILL #1 & #4: Controllo traguardi +1R (Break-Even) e +2R (Trailing Lock)
        if not state.get("alert_1r_sent", False) and price >= entry + one_r:
            new_stop = round(entry * 1.001, 6)
            if new_stop > state["stop_loss"]:
                state["stop_loss"] = new_stop
                stop = new_stop
            state["alert_1r_sent"] = True
            msg_1r = f"🔔 [ALERT +1R] {active_coin} ha toccato +1R ({fmt_price(entry + one_r)}$) -> Stop Loss spostato a PAREGGIO ({fmt_price(stop)}$)!"
            alerts_fired.append(msg_1r)
            state["last_action"] = f"{msg_1r} ({now_rome})"

        if not state.get("alert_2r_sent", False) and price >= entry + 2.0 * one_r:
            new_stop = round(entry + one_r, 6)
            if new_stop > state["stop_loss"]:
                state["stop_loss"] = new_stop
                stop = new_stop
            state["alert_2r_sent"] = True
            msg_2r = f"🔔 [ALERT +2R] {active_coin} ha toccato +2R ({fmt_price(entry + 2.0 * one_r)}$) -> Trailing Stop blindato a +1R ({fmt_price(stop)}$)!"
            alerts_fired.append(msg_2r)
            state["last_action"] = f"{msg_2r} ({now_rome})"

        # Stima profitto scalping griglia accumulato + variazione direzionale
        directional_pnl_pct = ((price - entry) / entry) * 100.0 if entry > 0 else 0.0
        est_grid_harvest_pct = 0.42 if (lower <= price <= upper) else 0.15
        net_pnl_pct = (directional_pnl_pct * 0.55) + est_grid_harvest_pct - (PIONEX_FEE * 200.0)
        net_pnl_usdt = invest * (net_pnl_pct / 100.0)

        should_close = False
        close_reason = ""

        if price <= stop:
            should_close = True
            if state.get("alert_2r_sent"):
                close_reason = f"🎯 TRAILING STOP (+1R LOCK) ESEGUITO su {active_coin} ({fmt_price(price)}$ <= {fmt_price(stop)}$)"
            elif state.get("alert_1r_sent"):
                close_reason = f"🛡️ BREAK-EVEN STOP ESEGUITO su {active_coin} ({fmt_price(price)}$ <= {fmt_price(stop)}$)"
            else:
                close_reason = f"🛑 STOP LOSS COLPITO su {active_coin} ({fmt_price(price)}$ <= {fmt_price(stop)}$)"
        elif price >= upper:
            should_close = True
            close_reason = f"🎯 TAKE PROFIT GRIGLIA RAGGIUNTO su {active_coin} ({fmt_price(price)}$ >= {fmt_price(upper)}$)"
        elif final_score <= 4 and ai_prob < 0.44:
            should_close = True
            close_reason = f"⚠️ CHIUSURA DIFENSIVA AI su {active_coin} (Score sceso a {final_score}/10, AI {ai_prob*100:.0f}%)"
        elif high_macro_imminent and net_pnl_pct > -0.5:
            should_close = True
            close_reason = f"🏛️ CHIUSURA PRE-NEWS MACRO USD HIGH IMPACT su {active_coin}"

        if should_close:
            live_res = pionex_Place_market_order(state["symbol"], "SELL", qty)
            exec_tag = "LIVE PIONEX" if live_res.get("ok") else "PAPER AUTONOMO"

            state["active"] = False
            state["total_trades"] += 1
            if net_pnl_usdt >= 0:
                state["wins"] += 1
            else:
                state["losses"] += 1
            state["daily_pnl_usdt"] = round(state.get("daily_pnl_usdt", 0.0) + net_pnl_usdt, 2)
            state["cumulative_pnl_usdt"] = round(state["cumulative_pnl_usdt"] + net_pnl_usdt, 2)
            state["cumulative_pnl_pct"] = round(state["cumulative_pnl_pct"] + net_pnl_pct, 2)
            state["last_action"] = f"CHIUSO {active_coin} ({exec_tag}): {close_reason} | PnL: {net_pnl_usdt:+.2f}$ ({net_pnl_pct:+.2f}%) il {now_rome}"

            state["history"].insert(0, {
                "closed_at": now_rome,
                "coin": active_coin,
                "entry": round(entry, 4),
                "exit": round(price, 4),
                "pnl_usdt": round(net_pnl_usdt, 2),
                "pnl_pct": round(net_pnl_pct, 2),
                "reason": close_reason
            })
            state["history"] = state["history"][:15]
            save_state(state)
            return "CLOSED_NOW", state["last_action"], net_pnl_usdt, net_pnl_pct, coin_data, alerts_fired

        save_state(state)
        status_msg = (
            f"🟢 POSIZIONE ATTIVA su {active_coin}/USDT ({state['mode']}) dal {state['opened_at']} | "
            f"Entry: {fmt_price(entry)}$ | Stop: {fmt_price(stop)}$ | PnL Stimato: {net_pnl_usdt:+.2f}$ ({net_pnl_pct:+.2f}%)"
        )
        return "HOLDING", status_msg, net_pnl_usdt, net_pnl_pct, coin_data, alerts_fired

    # CASO 2: NESSUNA POSIZIONE ATTIVA -> VALUTA APERTURA SULLA MIGLIORE CRYPTO DEL PANIERE
    b_coin = best_coin_data["coin"]
    b_sym = best_coin_data["pionex_symbol"]
    b_price = best_coin_data["price"]
    b_score = best_coin_data["final_score"]
    b_ai = best_coin_data["ai_prob"]
    b_lower = best_coin_data["lower_bound"]
    b_upper = best_coin_data["upper_bound"]
    b_stop = best_coin_data["stop_loss"]
    b_rm = best_coin_data["risk_manager"]
    b_sc = best_coin_data["scanner_timer"]
    b_ep = best_coin_data["exit_plan"]

    if (
        b_score >= 7
        and b_ai >= 0.52
        and b_rm["passed"]
        and b_sc["triggered"]
        and not high_macro_imminent
    ):
        deploy_usdt = b_rm["deployable_usdt"]
        qty = deploy_usdt / b_price if b_price > 0 else 0.0
        live_res = pionex_Place_market_order(b_sym, "BUY", deploy_usdt)
        mode_str = "LIVE PIONEX" if live_res.get("ok") else "PAPER AUTONOMO"

        state["active"] = True
        state["mode"] = mode_str
        state["coin"] = b_coin
        state["symbol"] = b_sym
        state["entry_price"] = round(b_price, 6)
        state["lower_bound"] = round(b_lower, 6)
        state["upper_bound"] = round(b_upper, 6)
        state["stop_loss"] = round(b_stop, 6)
        state["initial_stop_loss"] = round(b_stop, 6)
        state["one_r_usd"] = round(b_ep["one_r_usd"], 6)
        state["qty"] = round(qty, 6)
        state["invest_usdt"] = round(deploy_usdt, 2)
        state["opened_at"] = now_rome
        state["alert_1r_sent"] = False
        state["alert_2r_sent"] = False
        state["daily_trades_count"] = state.get("daily_trades_count", 0) + 1
        state["last_action"] = (
            f"🚀 APERTA NUOVA POSIZIONE ({mode_str}) su {b_coin}/USDT a {fmt_price(b_price)}$ "
            f"[Range {fmt_price(b_lower)}$ - {fmt_price(b_upper)}$ | Stop {fmt_price(b_stop)}$ | 1R: {fmt_price(b_ep['one_r_usd'])}$] il {now_rome}"
        )
        save_state(state)
        return "OPENED_NOW", state["last_action"], 0.0, 0.0, best_coin_data, alerts_fired

    wait_reasons = []
    if b_score < 7:
        wait_reasons.append(f"Top Score {b_coin} ({b_score}/10) sotto soglia 7/10")
    if not b_rm["passed"]:
        wait_reasons.append(f"Risk Manager ({b_rm['reason_str']})")
    if not b_sc["triggered"]:
        wait_reasons.append("Entry Timer 15m in attesa di conferma")
    wait_str = " | ".join(wait_reasons) if wait_reasons else "Condizioni non ancora ottimali"

    status_msg = f"⏸️ FLAT (In attesa di setup >= 7/10 + Risk PASS — Migliore ora: {b_coin} con {b_score}/10: {wait_str})"
    save_state(state)
    return "WAITING", status_msg, 0.0, 0.0, best_coin_data, alerts_fired


# ==============================================================================
# GENERATORE CONSOLE WEB REAL-TIME INTERATTIVA (index.html)
# ==============================================================================
def generate_webapp_html(state, ranked_coins, target_data, action_state, status_msg, open_pnl_usdt, open_pnl_pct, fng_val, fng_label, macro_events, nyse_bell_str, total_capital_usdt):
    now_rome = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M:%S")

    coins_payload = []
    for c in ranked_coins:
        coins_payload.append({
            "coin": c["coin"],
            "pionex_symbol": c["pionex_symbol"],
            "ws_pair": c["ws_pair"],
            "kraken_pair": c["kraken_pair"],
            "price": c["price"],
            "final_score": c["final_score"],
            "raw_100": c["raw_100"],
            "ai_prob": round(c["ai_prob"] * 100.0, 1),
            "lor_prob": round(c["lor_prob"] * 100.0, 1),
            "mlp_prob": round(c["mlp_prob"] * 100.0, 1),
            "ai_conf": round(c["ai_conf"], 1),
            "lower_bound": c["lower_bound"],
            "upper_bound": c["upper_bound"],
            "stop_loss": c["stop_loss"],
            "grid_step_pct": round(c["grid_step_pct"], 2),
            "net_grid_profit_pct": round(c["net_grid_profit_pct"], 2),
            "rsi_1h": round(c["rsi_1h"], 1),
            "rsi_4h": round(c["rsi_4h"], 1),
            "adx_1h": round(c["adx_1h"], 1),
            "hurst": round(c["hurst"], 2),
            "entropy": round(c["entropy"], 2),
            "vwap": c["vwap"],
            "poc": c["poc"],
            "rvol": round(c["rvol"], 2),
            "ob_imbalance": round(c["ob_imbalance"] * 100.0, 1),
            "spread_bps": round(c["spread_bps"], 1),
            "funding_pct": round(c["funding_pct"], 4),
            "time_machine": {
                "win_or_grid_prob": round(c["time_machine"]["win_or_grid_prob"], 1),
                "target_first_prob": round(c["time_machine"]["target_first_prob"], 1),
                "in_range_prob": round(c["time_machine"]["in_range_prob"], 1),
                "stop_first_prob": round(c["time_machine"]["stop_first_prob"], 1),
                "expected_r": round(c["time_machine"]["expected_r"], 2)
            },
            "exit_plan": {
                "one_r_usd": c["exit_plan"]["one_r_usd"],
                "one_r_pct": round(c["exit_plan"]["one_r_pct"], 2),
                "rr_ratio": round(c["exit_plan"]["rr_ratio"], 2),
                "level_1r": c["exit_plan"]["level_1r"],
                "level_2r": c["exit_plan"]["level_2r"],
                "level_3r": c["exit_plan"]["level_3r"]
            },
            "scanner_timer": c["scanner_timer"],
            "risk_manager": c["risk_manager"],
            "breakdown": c["breakdown"]
        })

    macro_payload = [
        {"diff_h": round(dh, 1), "impact": imp, "title": title}
        for dh, imp, title in macro_events[:5]
    ]

    payload = {
        "generated_at": now_rome,
        "invest_eur": INVEST_EUR,
        "total_capital_usdt": round(total_capital_usdt, 2),
        "grids": GRIDS,
        "fng_val": fng_val,
        "fng_label": fng_label,
        "nyse_bell": nyse_bell_str,
        "action_state": action_state,
        "status_msg": status_msg,
        "bot_state": state,
        "target_coin": target_data["coin"],
        "coins": coins_payload,
        "macro_events": macro_payload
    }

    payload_json = json.dumps(payload).replace("</", "<\\/")

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pionex Quantum AI — Console Web Real-Time</title>
<style>
  :root {
    --bg: #070b14;
    --card: #101726;
    --card-alt: #141e33;
    --border: #1e2d4a;
    --text: #f1f5f9;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
    --amber: #f59e0b;
    --red: #ef4444;
    --purple: #a855f7;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(circle at top right, #111c35, var(--bg) 60%);
    color: var(--text);
    padding: 14px;
    line-height: 1.45;
  }
  .container { max-width: 1180px; margin: 0 auto; }

  .topbar {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 14px 18px; margin-bottom: 14px; gap: 12px;
  }
  .title-wrap h1 { font-size: 1.25rem; font-weight: 800; letter-spacing: -0.3px; }
  .title-wrap p { color: var(--muted); font-size: 0.8rem; margin-top: 2px; }
  .live-pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 13px; border-radius: 999px; font-weight: 700; font-size: 0.78rem;
    background: #064e3b; color: #6ee7b7; border: 1px solid #10b981;
  }
  .dot-pulse {
    width: 9px; height: 9px; border-radius: 50%; background: #10b981;
    box-shadow: 0 0 8px #10b981; animation: pulse 1.4s infinite;
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.35); opacity: 0.55; }
  }

  .advisor-banner {
    border-radius: 14px; padding: 16px 18px; margin-bottom: 14px;
    border: 2px solid var(--border); display: flex; flex-wrap: wrap;
    justify-content: space-between; align-items: center; gap: 14px;
    background: var(--card-alt);
  }
  .advisor-left { flex: 1; min-width: 260px; }
  .advisor-tag {
    display: inline-block; font-size: 0.72rem; font-weight: 800;
    text-transform: uppercase; padding: 3px 9px; border-radius: 6px; margin-bottom: 6px;
  }
  .advisor-headline { font-size: 1.15rem; font-weight: 800; margin-bottom: 4px; }
  .advisor-sub { font-size: 0.86rem; color: #cbd5e1; }
  .advisor-actions { display: flex; flex-wrap: wrap; gap: 8px; }

  .btn {
    cursor: pointer; border: none; border-radius: 10px; padding: 9px 14px;
    font-weight: 700; font-size: 0.82rem; transition: all 0.15s ease;
    display: inline-flex; align-items: center; gap: 6px; color: #fff;
  }
  .btn:hover { filter: brightness(1.12); transform: translateY(-1px); }
  .btn-green { background: linear-gradient(135deg, #059669, #10b981); }
  .btn-red { background: linear-gradient(135deg, #dc2626, #ef4444); }
  .btn-amber { background: linear-gradient(135deg, #d97706, #f59e0b); color: #0f172a; }
  .btn-blue { background: linear-gradient(135deg, #0284c7, #38bdf8); color: #0f172a; }
  .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--muted); }

  .tabs {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px;
  }
  .tab-btn {
    cursor: pointer; background: var(--card); border: 1px solid var(--border);
    color: var(--muted); padding: 10px 16px; border-radius: 10px;
    font-weight: 700; font-size: 0.84rem; transition: all 0.15s;
  }
  .tab-btn.active {
    background: var(--accent); color: #08101f; border-color: var(--accent);
  }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 14px; margin-bottom: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  }
  .card h2 {
    font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--accent); margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between;
  }
  .metric-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.87rem;
  }
  .metric-row:last-child { border-bottom: none; }
  .metric-val { font-weight: 700; }

  .progress-wrap {
    background: #1e293b; border-radius: 999px; height: 11px; overflow: hidden;
    margin: 8px 0; position: relative;
  }
  .progress-bar {
    height: 100%; border-radius: 999px; transition: width 0.35s ease;
    background: linear-gradient(90deg, #38bdf8, #10b981);
  }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  th, td { padding: 10px 8px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-size: 0.74rem; text-transform: uppercase; }
  tr.coin-row { cursor: pointer; transition: background 0.15s; }
  tr.coin-row:hover { background: rgba(56, 189, 248, 0.08); }
  tr.coin-row.selected-row { background: rgba(16, 185, 129, 0.13); }

  .pill-sm {
    padding: 2px 7px; border-radius: 6px; font-weight: 700; font-size: 0.73rem; display: inline-block;
  }
  .pill-pass { background: rgba(16,185,129,0.2); color: #34d399; border: 1px solid #10b981; }
  .pill-block { background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid #ef4444; }
  .pill-wait { background: rgba(245,158,11,0.2); color: #fbbf24; border: 1px solid #f59e0b; }

  .price-flash-up { color: #34d399 !important; transition: color 0.2s; }
  .price-flash-dn { color: #f87171 !important; transition: color 0.2s; }

  .form-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 10px;
  }
  .input-group label { display: block; font-size: 0.74rem; color: var(--muted); margin-bottom: 4px; }
  .input-group input, .input-group select {
    width: 100%; background: #0b1120; border: 1px solid var(--border);
    color: var(--text); padding: 8px 10px; border-radius: 8px; font-size: 0.85rem; font-weight: 600;
  }
  .toast {
    position: fixed; bottom: 18px; right: 18px; background: #065f46; color: #ecfdf5;
    border: 1px solid #10b981; padding: 11px 16px; border-radius: 10px;
    font-size: 0.84rem; font-weight: 700; display: none; z-index: 999;
    box-shadow: 0 10px 25px rgba(0,0,0,0.45);
  }
</style>
</head>
<body>
<div class="container">

  <!-- TOPBAR -->
  <div class="topbar">
    <div class="title-wrap">
      <h1>⚡ PIONEX QUANTUM AI — CONSOLE WEB REAL-TIME</h1>
      <p id="sub-header">Ultimo sync bot: -- | Streaming prezzi Kraken WebSocket attivo</p>
    </div>
    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
      <div class="live-pill" id="ws-status-pill">
        <span class="dot-pulse" id="ws-dot"></span>
        <span id="ws-status-text">CONNESSIONE LIVE...</span>
      </div>
      <button class="btn btn-outline" onclick="forceRefreshPrices()">🔄 Aggiorna Prezzi Ora</button>
    </div>
  </div>

  <!-- REAL-TIME ADVISOR BANNER -->
  <div class="advisor-banner" id="advisor-banner">
    <div class="advisor-left">
      <span class="advisor-tag" id="advisor-tag">ANALISI IN TEMPO REALE</span>
      <div class="advisor-headline" id="advisor-headline">Caricamento stato posizione...</div>
      <div class="advisor-sub" id="advisor-sub">Connessione al flusso prezzi in tempo reale...</div>
    </div>
    <div class="advisor-actions" id="advisor-actions"></div>
  </div>

  <!-- NAVIGATION TABS -->
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('tab-live', this)">📡 1. Posizione Attiva & Radar Live</button>
    <button class="tab-btn" onclick="switchTab('tab-scanner', this)">🏆 2. Scanner 10 Crypto & Parametri Pionex</button>
    <button class="tab-btn" onclick="switchTab('tab-history', this)">📜 3. Storico Posizioni Chiuse (<span id="closed-count-tab">0</span>)</button>
    <button class="tab-btn" onclick="switchTab('tab-manual', this)">🛠️ 4. Gestione Manuale Posizione</button>
  </div>

  <!-- TAB 1: POSIZIONE ATTIVA & RADAR LIVE -->
  <div class="tab-panel active" id="tab-live">
    <div class="grid">
      <div class="card">
        <h2>
          <span>🟢 Posizione Aperta (Live Tick-by-Tick)</span>
          <span id="active-badge" class="pill-sm pill-wait">FLAT</span>
        </h2>
        <div id="active-position-body"></div>
      </div>

      <div class="card">
        <h2>
          <span>🎯 Parametri Griglia Pionex (<span id="selected-coin-title">SOL</span>)</span>
          <button class="btn btn-blue" style="padding:4px 10px; font-size:0.74rem;" onclick="copyPionexParams()">📋 Copia Parametri</button>
        </h2>
        <div id="pionex-params-body"></div>
      </div>

      <div class="card">
        <h2>
          <span>🛡️ 5-Skill Quant Gatekeeper (<span id="skill-coin-title">SOL</span>)</span>
          <span id="skill-risk-badge" class="pill-sm pill-pass">PASS</span>
        </h2>
        <div id="skills-gatekeeper-body"></div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2><span>🧠 Hybrid Neural AI + Time Machine 24h</span></h2>
        <div id="neural-tm-body"></div>
      </div>
      <div class="card">
        <h2><span>🌍 Contesto Macro USD, Wall Street & Portafoglio</span></h2>
        <div id="macro-portfolio-body"></div>
      </div>
    </div>
  </div>

  <!-- TAB 2: SCANNER 10 CRYPTO -->
  <div class="tab-panel" id="tab-scanner">
    <div class="card">
      <h2>
        <span>🏆 Classifica Live 10 Crypto (Clicca una riga per ispezionare o aprire posizione)</span>
        <span style="font-size:0.76rem; color:var(--muted);">Prezzi aggiornati in tempo reale via WebSocket</span>
      </h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Coin</th>
              <th>Prezzo Live</th>
              <th>Score</th>
              <th>AI Win%</th>
              <th>TimeMach 24h</th>
              <th>Range Griglia Pionex</th>
              <th>Stop (1R)</th>
              <th>R/R</th>
              <th>Timer 15m</th>
              <th>Risk Mgr</th>
              <th>Azione Manuale</th>
            </tr>
          </thead>
          <tbody id="scanner-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 3: POSIZIONI CHIUSE -->
  <div class="tab-panel" id="tab-history">
    <div class="grid">
      <div class="card">
        <h2><span>📊 Riepilogo Performance Posizioni Chiuse</span></h2>
        <div id="history-summary-body"></div>
      </div>
    </div>
    <div class="card">
      <h2>
        <span>📜 Registro Completo Posizioni Chiuse</span>
        <button class="btn btn-outline" style="padding:4px 10px; font-size:0.74rem;" onclick="resetLocalConsoleState()">🔄 Riallinea allo Stato Bot GitHub</button>
      </h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Data/Ora Chiusura</th>
              <th>Coppia</th>
              <th>Prezzo Entry</th>
              <th>Prezzo Exit</th>
              <th>PnL Netto ($)</th>
              <th>PnL Netto (%)</th>
              <th>Motivo Chiusura</th>
            </tr>
          </thead>
          <tbody id="history-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 4: GESTIONE MANUALE POSIZIONE -->
  <div class="tab-panel" id="tab-manual">
    <div class="card">
      <h2><span>🛠️ Registra o Modifica Manualmente una Posizione Aperta su Pionex</span></h2>
      <p style="font-size:0.83rem; color:var(--muted); margin-bottom:10px;">
        Hai aperto o chiuso manualmente un Grid Bot su Pionex? Registralo qui sotto per monitorare in tempo reale PnL, distanza dallo Stop Loss, traguardi +1R/+2R e ricevere avvisi immediati di chiusura.
      </p>
      <div class="form-grid">
        <div class="input-group">
          <label>Crypto</label>
          <select id="man-coin" onchange="prefillManualForm()"></select>
        </div>
        <div class="input-group">
          <label>Prezzo Entry ($)</label>
          <input type="number" step="any" id="man-entry">
        </div>
        <div class="input-group">
          <label>Lower Bound Griglia ($)</label>
          <input type="number" step="any" id="man-lower">
        </div>
        <div class="input-group">
          <label>Upper Bound Griglia ($)</label>
          <input type="number" step="any" id="man-upper">
        </div>
        <div class="input-group">
          <label>Stop Loss ($)</label>
          <input type="number" step="any" id="man-stop">
        </div>
        <div class="input-group">
          <label>Capitale Investito (USDT)</label>
          <input type="number" step="any" id="man-invest">
        </div>
      </div>
      <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;">
        <button class="btn btn-green" onclick="saveManualOpenPosition()">🚀 Attiva Monitoraggio Posizione Live</button>
        <button class="btn btn-red" onclick="closeActivePosition('Chiusura Manuale Utente dalla Console Web')">🛑 Chiudi Posizione Attiva al Prezzo Live</button>
        <button class="btn btn-outline" onclick="exportBotStateJson()">💾 Scarica bot_state.json aggiornato</button>
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast-box"></div>

<script>
const INITIAL_DATA = __CONSOLE_PAYLOAD__;
const STORAGE_KEY = "pionex_ai_console_state_v7";

let appState = null;
let livePrices = {};
let prevPrices = {};
let selectedCoin = INITIAL_DATA.target_coin || "SOL";
let wsConn = null;

function fmtPrice(p) {
  p = Number(p || 0);
  if (p <= 0) return "0.00";
  if (p >= 1000) return p.toFixed(1);
  if (p >= 10) return p.toFixed(2);
  if (p >= 1) return p.toFixed(3);
  return p.toFixed(4);
}

function showToast(msg) {
  const t = document.getElementById("toast-box");
  t.textContent = msg;
  t.style.display = "block";
  setTimeout(() => { t.style.display = "none"; }, 3500);
}

function initConsoleState() {
  INITIAL_DATA.coins.forEach(c => {
    livePrices[c.coin] = c.price;
    prevPrices[c.coin] = c.price;
  });

  const savedRaw = localStorage.getItem(STORAGE_KEY);
  if (savedRaw) {
    try {
      const parsed = JSON.parse(savedRaw);
      if (parsed && parsed.bot_state) {
        appState = parsed.bot_state;
      }
    } catch (e) {}
  }
  if (!appState) {
    appState = JSON.parse(JSON.stringify(INITIAL_DATA.bot_state));
  }

  const sel = document.getElementById("man-coin");
  sel.innerHTML = INITIAL_DATA.coins.map(c => `<option value="${c.coin}">${c.coin}/USDT</option>`).join("");
  sel.value = selectedCoin;
  prefillManualForm();

  document.getElementById("sub-header").textContent =
    `Ultimo sync GitHub Bot: ${INITIAL_DATA.generated_at} | Capitale base: ${INITIAL_DATA.invest_eur.toFixed(0)}€ (~${INITIAL_DATA.total_capital_usdt.toFixed(2)} USDT)`;

  renderAll();
  startKrakenWebSocket();
  setInterval(fetchRestFallbackPrices, 8000);
}

function persistLocalState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    updated_at: new Date().toISOString(),
    bot_state: appState
  }));
}

function resetLocalConsoleState() {
  localStorage.removeItem(STORAGE_KEY);
  appState = JSON.parse(JSON.stringify(INITIAL_DATA.bot_state));
  renderAll();
  showToast("Stato Console riallineato con l'ultimo run di GitHub Actions!");
}

function getCoinObj(coinSymbol) {
  return INITIAL_DATA.coins.find(c => c.coin === coinSymbol) || INITIAL_DATA.coins[0];
}

function switchTab(tabId, btnEl) {
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.getElementById(tabId).classList.add("active");
  if (btnEl) btnEl.classList.add("active");
}

function selectCoinForInspection(coinSym) {
  selectedCoin = coinSym;
  const sel = document.getElementById("man-coin");
  if (sel) sel.value = coinSym;
  prefillManualForm();
  renderAll();
}

function startKrakenWebSocket() {
  try {
    const wsPairs = INITIAL_DATA.coins.map(c => c.ws_pair);
    wsConn = new WebSocket("wss://ws.kraken.com");
    wsConn.onopen = () => {
      document.getElementById("ws-status-text").textContent = "LIVE WEBSOCKET KRAKEN ATTIVO";
      wsConn.send(JSON.stringify({
        event: "subscribe",
        pair: wsPairs,
        subscription: { name: "ticker" }
      }));
    };
    wsConn.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (Array.isArray(data) && data.length >= 4 && data[1] && data[1].c) {
          const pairName = data[3];
          const newPrice = parseFloat(data[1].c[0]);
          const matched = INITIAL_DATA.coins.find(c => c.ws_pair === pairName);
          if (matched && newPrice > 0) {
            prevPrices[matched.coin] = livePrices[matched.coin] || newPrice;
            livePrices[matched.coin] = newPrice;
            renderAll();
          }
        }
      } catch (err) {}
    };
    wsConn.onclose = () => {
      document.getElementById("ws-status-text").textContent = "POLLING REST LIVE (8s)";
    };
  } catch (e) {
    document.getElementById("ws-status-text").textContent = "POLLING REST LIVE (8s)";
  }
}

async function fetchRestFallbackPrices() {
  try {
    const pairsCsv = INITIAL_DATA.coins.map(c => c.kraken_pair).join(",");
    const resp = await fetch(`https://api.kraken.com/0/public/Ticker?pair=${pairsCsv}`);
    const data = await resp.json();
    if (data && data.result) {
      Object.keys(data.result).forEach(k => {
        const lastP = parseFloat(data.result[k].c[0]);
        const matched = INITIAL_DATA.coins.find(c => k.includes(c.kraken_pair) || c.kraken_pair.includes(k));
        if (matched && lastP > 0) {
          prevPrices[matched.coin] = livePrices[matched.coin] || lastP;
          livePrices[matched.coin] = lastP;
        }
      });
      renderAll();
    }
  } catch (e) {}
}

function forceRefreshPrices() {
  fetchRestFallbackPrices();
  showToast("Prezzi live sincronizzati con Kraken!");
}

function computeLivePositionMetrics() {
  if (!appState.active) return null;
  const coin = appState.coin || "SOL";
  const liveP = livePrices[coin] || appState.entry_price;
  const entry = Number(appState.entry_price);
  const lower = Number(appState.lower_bound);
  const upper = Number(appState.upper_bound);
  const stop = Number(appState.stop_loss);
  const invest = Number(appState.invest_usdt || 47.5);
  const oneR = Number(appState.one_r_usd || Math.max(entry * 0.015, entry - stop));

  const dirPct = entry > 0 ? ((liveP - entry) / entry) * 100.0 : 0.0;
  const gridHarvest = (liveP >= lower && liveP <= upper) ? 0.42 : 0.15;
  const netPnlPct = (dirPct * 0.55) + gridHarvest - 0.10;
  const netPnlUsdt = invest * (netPnlPct / 100.0);

  const rMultiple = oneR > 0 ? (liveP - entry) / oneR : 0.0;
  const rangePct = (upper > lower) ? Math.max(0, Math.min(100, ((liveP - lower) / (upper - lower)) * 100.0)) : 50.0;
  const distToStopPct = liveP > 0 ? ((liveP - stop) / liveP) * 100.0 : 0.0;
  const distToTargetPct = liveP > 0 ? ((upper - liveP) / liveP) * 100.0 : 0.0;

  return {
    coin, liveP, entry, lower, upper, stop, invest, oneR,
    netPnlPct, netPnlUsdt, rMultiple, rangePct, distToStopPct, distToTargetPct
  };
}

function renderAdvisorBanner() {
  const banner = document.getElementById("advisor-banner");
  const tag = document.getElementById("advisor-tag");
  const headline = document.getElementById("advisor-headline");
  const sub = document.getElementById("advisor-sub");
  const actions = document.getElementById("advisor-actions");

  const bestCoin = INITIAL_DATA.coins[0];
  const bestLiveP = livePrices[bestCoin.coin] || bestCoin.price;

  if (appState.active) {
    const m = computeLivePositionMetrics();
    const coinObj = getCoinObj(m.coin);

    if (m.liveP <= m.stop) {
      banner.style.borderColor = "#ef4444";
      tag.style.background = "#7f1d1d"; tag.style.color = "#fecaca";
      tag.textContent = "🚨 AZIONE MANUALE RICHIESTA: CHIUDI SUBITO";
      headline.textContent = `🛑 CHIUDI ORA LA POSIZIONE SU ${m.coin}/USDT! (Stop Loss toccato a ${fmtPrice(m.liveP)}$)`;
      sub.textContent = `Il prezzo live (${fmtPrice(m.liveP)}$) è sceso sotto lo Stop Loss (${fmtPrice(m.stop)}$). Chiudi il Grid Bot su Pionex per proteggere il capitale.`;
      actions.innerHTML = `<button class="btn btn-red" onclick="closeActivePosition('Stop Loss Colpito Live')">🛑 Registra Chiusura Stop Loss Ora</button>`;
    } else if (m.liveP >= m.upper) {
      banner.style.borderColor = "#10b981";
      tag.style.background = "#064e3b"; tag.style.color = "#6ee7b7";
      tag.textContent = "🎯 TAKE PROFIT RAGGIUNTO: INCASSA IL PROFITTO";
      headline.textContent = `🎯 CHIUDI IN PROFITTO ${m.coin}/USDT! Target Superiore (${fmtPrice(m.upper)}$) Raggiunto`;
      sub.textContent = `Prezzo Live: ${fmtPrice(m.liveP)}$ | PnL Netto Stimato: ${m.netPnlUsdt >= 0 ? '+' : ''}${m.netPnlUsdt.toFixed(2)}$ (${m.netPnlPct.toFixed(2)}%). Chiudi la griglia su Pionex e incassa.`;
      actions.innerHTML = `<button class="btn btn-green" onclick="closeActivePosition('Take Profit Tetto Griglia Raggiunto')">🎯 Incassa e Chiudi Posizione Ora</button>`;
    } else if (m.rMultiple >= 1.0 && !appState.alert_1r_sent) {
      banner.style.borderColor = "#f59e0b";
      tag.style.background = "#78350f"; tag.style.color = "#fde68a";
      tag.textContent = "🛡️ TRAGUARDO +1R RAGGIUNTO: SPOSTA LO STOP A PAREGGIO";
      headline.textContent = `🔔 ${m.coin}/USDT è a +${m.rMultiple.toFixed(2)}R (${fmtPrice(m.liveP)}$): Sposta lo Stop Loss a Break-Even (${fmtPrice(m.entry)}$)!`;
      sub.textContent = `Operazione ora a rischio zero. Premi il pulsante per aggiornare lo Stop Loss al prezzo di ingresso (${fmtPrice(m.entry)}$).`;
      actions.innerHTML = `
        <button class="btn btn-amber" onclick="moveStopToBreakEven()">🛡️ Sposta Stop a Pareggio (Break-Even)</button>
        <button class="btn btn-red" onclick="closeActivePosition('Take Profit Manuale a +1R')">💰 Chiudi e Incassa Ora</button>
      `;
    } else if (coinObj && coinObj.final_score <= 4) {
      banner.style.borderColor = "#ef4444";
      tag.style.background = "#7f1d1d"; tag.style.color = "#fecaca";
      tag.textContent = "⚠️ ALLERTA AI: DETERIORAMENTO TECNICO";
      headline.textContent = `⚠️ CONSIGLIATA CHIUSURA MANUALE SU ${m.coin}/USDT (Score sceso a ${coinObj.final_score}/10)`;
      sub.textContent = `L'AI rileva perdita di struttura su ${m.coin}. PnL attuale: ${m.netPnlUsdt >= 0 ? '+' : ''}${m.netPnlUsdt.toFixed(2)}$. Valuta l'uscita difensiva.`;
      actions.innerHTML = `<button class="btn btn-red" onclick="closeActivePosition('Chiusura Difensiva AI Manuale')">⚠️ Chiudi Posizione Difensivamente</button>`;
    } else {
      banner.style.borderColor = "#10b981";
      tag.style.background = "#064e3b"; tag.style.color = "#6ee7b7";
      tag.textContent = "🟢 POSIZIONE ATTIVA E NEL RANGE: NESSUNA AZIONE RICHIESTA";
      headline.textContent = `✅ MANTIENI APERTA ${m.coin}/USDT — Griglia in Lavoro (${fmtPrice(m.liveP)}$ | ${m.rMultiple >= 0 ? '+' : ''}${m.rMultiple.toFixed(2)}R)`;
      sub.textContent = `Range: ${fmtPrice(m.lower)}$ – ${fmtPrice(m.upper)}$ | Distanza da Stop: ${m.distToStopPct.toFixed(2)}% | Distanza da Target: ${m.distToTargetPct.toFixed(2)}%`;
      actions.innerHTML = `
        <button class="btn btn-amber" onclick="moveStopToBreakEven()">🛡️ Porta Stop a Pareggio</button>
        <button class="btn btn-red" onclick="closeActivePosition('Chiusura Manuale Utente')">🛑 Chiudi Posizione Manualmente</button>
      `;
    }
  } else {
    const canOpen = bestCoin.final_score >= 7 && bestCoin.risk_manager.passed && bestCoin.scanner_timer.triggered;
    if (canOpen) {
      banner.style.borderColor = "#10b981";
      tag.style.background = "#064e3b"; tag.style.color = "#6ee7b7";
      tag.textContent = "🚀 SEGNALE VERDE: PUOI APRIRE UNA POSIZIONE ORA";
      headline.textContent = `🚀 APRI ORA SU ${bestCoin.coin}/USDT (Score ${bestCoin.final_score}/10 | AI ${bestCoin.ai_prob}% | Risk PASS)`;
      sub.textContent = `Prezzo Live: ${fmtPrice(bestLiveP)}$ | Range Griglia Pionex: ${fmtPrice(bestCoin.lower_bound)}$ – ${fmtPrice(bestCoin.upper_bound)}$ (${INITIAL_DATA.grids} griglie) | Stop Loss: ${fmtPrice(bestCoin.stop_loss)}$`;
      actions.innerHTML = `
        <button class="btn btn-green" onclick="openQuickPosition('${bestCoin.coin}')">🚀 Apri / Registra Posizione su ${bestCoin.coin}</button>
        <button class="btn btn-blue" onclick="selectCoinForInspection('${bestCoin.coin}'); copyPionexParams();">📋 Copia Parametri Pionex</button>
      `;
    } else {
      banner.style.borderColor = "#f59e0b";
      tag.style.background = "#78350f"; tag.style.color = "#fde68a";
      tag.textContent = "⏸️ ATTESA CONSIGLIATA: NON APRIRE MANUALMENTE ADESSO";
      headline.textContent = `⏸️ RESTA FLAT IN USDT — Migliore del paniere: ${bestCoin.coin}/USDT (Score ${bestCoin.final_score}/10)`;
      sub.textContent = `Motivo blocco/attesa: ${bestCoin.risk_manager.passed ? bestCoin.scanner_timer.status_str : bestCoin.risk_manager.reason_str}. Puoi comunque forzare un'apertura manuale se lo desideri.`;
      actions.innerHTML = `
        <button class="btn btn-outline" onclick="openQuickPosition('${bestCoin.coin}')">⚡ Forza Apertura Manuale su ${bestCoin.coin}</button>
      `;
    }
  }
}

function renderActivePositionCard() {
  const badge = document.getElementById("active-badge");
  const container = document.getElementById("active-position-body");

  if (!appState.active) {
    badge.className = "pill-sm pill-wait";
    badge.textContent = "FLAT (100% USDT)";
    const best = INITIAL_DATA.coins[0];
    container.innerHTML = `
      <div style="padding:12px 0; color:var(--muted); font-size:0.88rem;">
        Nessuna posizione aperta al momento. Il capitale è protetto in USDT.<br><br>
        <strong>Migliore opportunità attuale sul radar:</strong> ${best.coin}/USDT (Score <strong>${best.final_score}/10</strong> — ${best.raw_100}/100 pt).
      </div>
      <div style="margin-top:8px;">
        <button class="btn btn-green" style="width:100%; justify-content:center;" onclick="openQuickPosition('${best.coin}')">
          🚀 Apri Posizione Manuale su ${best.coin}/USDT al Prezzo Live (${fmtPrice(livePrices[best.coin] || best.price)}$)
        </button>
      </div>
    `;
    return;
  }

  const m = computeLivePositionMetrics();
  badge.className = "pill-sm pill-pass";
  badge.textContent = `ATTIVA SU ${m.coin}/USDT`;

  const pnlColor = m.netPnlUsdt >= 0 ? "#10b981" : "#ef4444";
  const sign = m.netPnlUsdt >= 0 ? "+" : "";

  container.innerHTML = `
    <div class="metric-row"><span>Coppia & Modalità</span><span class="metric-val">${m.coin}/USDT (${appState.mode})</span></div>
    <div class="metric-row"><span>Prezzo Ingresso vs Live</span><span class="metric-val">${fmtPrice(m.entry)}$ ➔ <strong style="color:var(--accent);">${fmtPrice(m.liveP)}$</strong></span></div>
    <div class="metric-row"><span>PnL Netto Live (incl. Grid)</span><span class="metric-val" style="color:${pnlColor}; font-size:1rem;">${sign}${m.netPnlUsdt.toFixed(2)}$ (${sign}${m.netPnlPct.toFixed(2)}%) [${sign}${m.rMultiple.toFixed(2)}R]</span></div>
    <div class="metric-row"><span>Range Griglia Attiva</span><span class="metric-val">${fmtPrice(m.lower)}$ – ${fmtPrice(m.upper)}$</span></div>
    <div class="progress-wrap" title="Posizione del prezzo nel range della griglia">
      <div class="progress-bar" style="width:${m.rangePct.toFixed(1)}%;"></div>
    </div>
    <div class="metric-row"><span>Stop Loss Attuale</span><span class="metric-val" style="color:#f87171;">${fmtPrice(m.stop)}$ (dist. ${m.distToStopPct.toFixed(2)}%)</span></div>
    <div class="metric-row"><span>Target +1R (Break-Even)</span><span class="metric-val">${fmtPrice(m.entry + m.oneR)}$ ${appState.alert_1r_sent ? '✅' : '⏳'}</span></div>
    <div class="metric-row"><span>Target +2R (Lock +1R)</span><span class="metric-val">${fmtPrice(m.entry + 2*m.oneR)}$ ${appState.alert_2r_sent ? '✅' : '⏳'}</span></div>
    <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
      <button class="btn btn-amber" style="flex:1; justify-content:center;" onclick="moveStopToBreakEven()">🛡️ Stop a Pareggio</button>
      <button class="btn btn-red" style="flex:1; justify-content:center;" onclick="closeActivePosition('Chiusura Manuale Utente')">🛑 Chiudi Ora</button>
    </div>
  `;
}

function renderInspectCards() {
  const c = getCoinObj(selectedCoin);
  const liveP = livePrices[c.coin] || c.price;

  document.getElementById("selected-coin-title").textContent = `${c.coin}/USDT`;
  document.getElementById("skill-coin-title").textContent = `${c.coin}/USDT`;

  document.getElementById("pionex-params-body").innerHTML = `
    <div class="metric-row"><span>Prezzo Live Streaming</span><span class="metric-val" style="color:var(--accent); font-size:1rem;">${fmtPrice(liveP)} USDT</span></div>
    <div class="metric-row"><span>Punteggio Quant AI</span><span class="metric-val">${c.final_score}/10 (${c.raw_100}/100 pt)</span></div>
    <div class="metric-row"><span>Lower Price (Pavimento)</span><span class="metric-val" style="color:#38bdf8;">${fmtPrice(c.lower_bound)} USDT</span></div>
    <div class="metric-row"><span>Upper Price (Tetto)</span><span class="metric-val" style="color:#38bdf8;">${fmtPrice(c.upper_bound)} USDT</span></div>
    <div class="metric-row"><span>Numero Griglie (Arithmetic)</span><span class="metric-val">${INITIAL_DATA.grids} Griglie (Step ${c.grid_step_pct}% | Netto ${c.net_grid_profit_pct}%)</span></div>
    <div class="metric-row"><span>Stop Loss (1R = ${fmtPrice(c.exit_plan.one_r_usd)}$)</span><span class="metric-val" style="color:#f87171;">${fmtPrice(c.stop_loss)} USDT (-${c.exit_plan.one_r_pct}%)</span></div>
    <div class="metric-row"><span>Capitale Operativo (95%)</span><span class="metric-val" style="color:#10b981;">${c.risk_manager.deployable_usdt.toFixed(2)} USDT (Riserva ${c.risk_manager.cash_reserve_usdt.toFixed(2)}$)</span></div>
  `;

  const rmBadge = document.getElementById("skill-risk-badge");
  rmBadge.className = "pill-sm " + (c.risk_manager.passed ? "pill-pass" : "pill-block");
  rmBadge.textContent = c.risk_manager.status_badge;

  document.getElementById("skills-gatekeeper-body").innerHTML = `
    <div class="metric-row"><span>Skill #2/#6 Risk Manager</span><span class="metric-val">${c.risk_manager.status_badge}</span></div>
    <div style="font-size:0.77rem; color:var(--muted); padding:3px 0 6px 0;">${c.risk_manager.reason_str}</div>
    <div class="metric-row"><span>Skill #3/#8 Pattern 15m</span><span class="metric-val">${c.scanner_timer.pattern}</span></div>
    <div class="metric-row"><span>Entry Timer 15m</span><span class="metric-val">${c.scanner_timer.status_str}</span></div>
    <div class="metric-row"><span>Skill #4 Exit Planner (R/R)</span><span class="metric-val">${c.exit_plan.rr_ratio}R (+1R: ${fmtPrice(c.exit_plan.level_1r)}$ | +2R: ${fmtPrice(c.exit_plan.level_2r)}$)</span></div>
    <div class="metric-row"><span>Orderbook L2 & Spread</span><span class="metric-val">Imb: ${c.ob_imbalance >= 0 ? '+' : ''}${c.ob_imbalance}% | Spread: ${c.spread_bps} bps</span></div>
  `;

  document.getElementById("neural-tm-body").innerHTML = `
    <div class="metric-row"><span>Ensemble AI Win Prob (12h)</span><span class="metric-val" style="color:var(--accent);">${c.ai_prob}% (Conf. ${c.ai_conf}%)</span></div>
    <div class="metric-row"><span>Lorentzian k-NN vs Deep MLP</span><span class="metric-val">${c.lor_prob}% / ${c.mlp_prob}%</span></div>
    <div class="metric-row"><span>Time Machine 24h (Win + Grid)</span><span class="metric-val" style="color:#10b981;">${c.time_machine.win_or_grid_prob}% (Target ${c.time_machine.target_first_prob}% + Grid ${c.time_machine.in_range_prob}%)</span></div>
    <div class="metric-row"><span>Rischio Stop Loss in 24h</span><span class="metric-val" style="color:#f87171;">${c.time_machine.stop_first_prob}% | Valore Atteso: ${c.time_machine.expected_r >= 0 ? '+' : ''}${c.time_machine.expected_r}R</span></div>
    <div class="metric-row"><span>Hurst / Entropia / RVOL</span><span class="metric-val">H: ${c.hurst} | Ent: ${c.entropy} | RVOL: ${c.rvol}x</span></div>
  `;

  const macroHtml = INITIAL_DATA.macro_events.length
    ? INITIAL_DATA.macro_events.map(ev => `${ev.impact === 'High' ? '🔴' : '🟠'} ${ev.title} (${ev.diff_h >= 0 ? 'tra ' + ev.diff_h + 'h' : 'passato'})`).join("<br>")
    : "🟢 Nessun evento Macro USD ad alto impatto imminente";

  document.getElementById("macro-portfolio-body").innerHTML = `
    <div class="metric-row"><span>Skill #1 Market Bell</span><span class="metric-val">${INITIAL_DATA.nyse_bell}</span></div>
    <div class="metric-row"><span>Fear & Greed Index</span><span class="metric-val">${INITIAL_DATA.fng_val}/100 (${INITIAL_DATA.fng_label})</span></div>
    <div class="metric-row"><span>PnL Oggi / Trade Oggi</span><span class="metric-val">${(appState.daily_pnl_usdt || 0) >= 0 ? '+' : ''}${(appState.daily_pnl_usdt || 0).toFixed(2)}$ (${appState.daily_trades_count || 0}/3 trade)</span></div>
    <div class="metric-row"><span>Bilancio Storico Totale</span><span class="metric-val" style="color:${(appState.cumulative_pnl_usdt || 0) >= 0 ? '#10b981' : '#ef4444'};">${(appState.cumulative_pnl_usdt || 0) >= 0 ? '+' : ''}${(appState.cumulative_pnl_usdt || 0).toFixed(2)}$ (${appState.wins || 0}W / ${appState.losses || 0}L)</span></div>
    <div style="font-size:0.78rem; color:var(--muted); margin-top:6px;">${macroHtml}</div>
  `;
}

function renderScannerTable() {
  const tbody = document.getElementById("scanner-tbody");
  tbody.innerHTML = INITIAL_DATA.coins.map((c, idx) => {
    const liveP = livePrices[c.coin] || c.price;
    const prevP = prevPrices[c.coin] || liveP;
    const flashClass = liveP > prevP ? "price-flash-up" : (liveP < prevP ? "price-flash-dn" : "");
    const isSel = c.coin === selectedCoin ? "selected-row" : "";
    const rmClass = c.risk_manager.passed ? "pill-pass" : "pill-block";
    const scClass = c.scanner_timer.triggered ? "pill-pass" : "pill-wait";

    return `
      <tr class="coin-row ${isSel}" onclick="selectCoinForInspection('${c.coin}')">
        <td><strong>#${idx + 1}</strong></td>
        <td><strong>${c.coin}/USDT</strong></td>
        <td class="${flashClass}"><strong>${fmtPrice(liveP)}$</strong></td>
        <td><strong>${c.final_score}/10</strong> <span style="color:var(--muted); font-size:0.74rem;">(${c.raw_100})</span></td>
        <td>${c.ai_prob}%</td>
        <td>${c.time_machine.win_or_grid_prob}% (${c.time_machine.expected_r >= 0 ? '+' : ''}${c.time_machine.expected_r}R)</td>
        <td>${fmtPrice(c.lower_bound)}$ – ${fmtPrice(c.upper_bound)}$</td>
        <td>${fmtPrice(c.stop_loss)}$</td>
        <td>${c.exit_plan.rr_ratio}R</td>
        <td><span class="pill-sm ${scClass}">${c.scanner_timer.triggered ? 'OK 15m' : 'WAIT'}</span></td>
        <td><span class="pill-sm ${rmClass}">${c.risk_manager.passed ? 'PASS' : 'BLOCK'}</span></td>
        <td>
          <button class="btn btn-green" style="padding:4px 9px; font-size:0.73rem;" onclick="event.stopPropagation(); openQuickPosition('${c.coin}')">
            🚀 Apri
          </button>
        </td>
      </tr>
    `;
  }).join("");
}

function renderClosedPositions() {
  const hist = appState.history || [];
  document.getElementById("closed-count-tab").textContent = hist.length;

  const totalTrades = appState.total_trades || hist.length;
  const wins = appState.wins || 0;
  const losses = appState.losses || 0;
  const wr = totalTrades > 0 ? ((wins / totalTrades) * 100.0).toFixed(1) : "0.0";
  const cumUsdt = Number(appState.cumulative_pnl_usdt || 0);
  const cumPct = Number(appState.cumulative_pnl_pct || 0);

  document.getElementById("history-summary-body").innerHTML = `
    <div class="metric-row"><span>Operazioni Chiuse Totali</span><span class="metric-val">${totalTrades} (${wins} Vinte / ${losses} Perse — Win Rate: ${wr}%)</span></div>
    <div class="metric-row"><span>Profitto/Perdita Cumulativo Netto</span><span class="metric-val" style="color:${cumUsdt >= 0 ? '#10b981' : '#ef4444'}; font-size:1rem;">${cumUsdt >= 0 ? '+' : ''}${cumUsdt.toFixed(2)} USDT (${cumPct >= 0 ? '+' : ''}${cumPct.toFixed(2)}%)</span></div>
    <div class="metric-row"><span>Ultima Azione Registrata</span><span class="metric-val" style="font-size:0.8rem;">${appState.last_action || 'Nessuna'}</span></div>
  `;

  const tbody = document.getElementById("history-tbody");
  if (!hist.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--muted); padding:18px;">Nessuna posizione chiusa in archivio. Quando una posizione viene chiusa (dal bot o manualmente), apparirà qui con il relativo PnL.</td></tr>`;
    return;
  }

  tbody.innerHTML = hist.map(h => {
    const pos = Number(h.pnl_usdt) >= 0;
    const col = pos ? "#10b981" : "#ef4444";
    const s = pos ? "+" : "";
    return `
      <tr>
        <td>${h.closed_at || '-'}</td>
        <td><strong>${h.coin || 'SOL'}/USDT</strong></td>
        <td>${fmtPrice(h.entry)}$</td>
        <td>${fmtPrice(h.exit)}$</td>
        <td style="color:${col}; font-weight:700;">${s}${Number(h.pnl_usdt).toFixed(2)}$</td>
        <td style="color:${col}; font-weight:700;">${s}${Number(h.pnl_pct).toFixed(2)}%</td>
        <td>${h.reason || '-'}</td>
      </tr>
    `;
  }).join("");
}

function prefillManualForm() {
  const coinSym = document.getElementById("man-coin").value || selectedCoin;
  const c = getCoinObj(coinSym);
  const liveP = livePrices[coinSym] || c.price;
  document.getElementById("man-entry").value = liveP;
  document.getElementById("man-lower").value = Number(c.lower_bound.toFixed(4));
  document.getElementById("man-upper").value = Number(c.upper_bound.toFixed(4));
  document.getElementById("man-stop").value = Number(c.stop_loss.toFixed(4));
  document.getElementById("man-invest").value = c.risk_manager.deployable_usdt;
}

function openQuickPosition(coinSym) {
  selectedCoin = coinSym;
  const c = getCoinObj(coinSym);
  const liveP = livePrices[coinSym] || c.price;
  const nowStr = new Date().toLocaleString("it-IT");

  appState.active = true;
  appState.mode = "MANUALE LIVE CONSOLE";
  appState.coin = coinSym;
  appState.symbol = c.pionex_symbol;
  appState.entry_price = liveP;
  appState.lower_bound = c.lower_bound;
  appState.upper_bound = c.upper_bound;
  appState.stop_loss = c.stop_loss;
  appState.initial_stop_loss = c.stop_loss;
  appState.one_r_usd = Math.max(liveP * 0.008, liveP - c.stop_loss);
  appState.invest_usdt = c.risk_manager.deployable_usdt;
  appState.qty = appState.invest_usdt / liveP;
  appState.opened_at = nowStr;
  appState.alert_1r_sent = false;
  appState.alert_2r_sent = false;
  appState.last_action = `Aperta posizione manuale su ${coinSym}/USDT a ${fmtPrice(liveP)}$ il ${nowStr}`;

  persistLocalState();
  renderAll();
  showToast(`Posizione ${coinSym}/USDT attivata in Console a ${fmtPrice(liveP)}$!`);
}

function saveManualOpenPosition() {
  const coinSym = document.getElementById("man-coin").value;
  const entry = parseFloat(document.getElementById("man-entry").value);
  const lower = parseFloat(document.getElementById("man-lower").value);
  const upper = parseFloat(document.getElementById("man-upper").value);
  const stop = parseFloat(document.getElementById("man-stop").value);
  const invest = parseFloat(document.getElementById("man-invest").value);

  if (!entry || !lower || !upper || !stop || !invest) {
    showToast("Compila tutti i campi numerici!");
    return;
  }
  const nowStr = new Date().toLocaleString("it-IT");
  appState.active = true;
  appState.mode = "MANUALE PIONEX";
  appState.coin = coinSym;
  appState.symbol = `${coinSym}_USDT`;
  appState.entry_price = entry;
  appState.lower_bound = lower;
  appState.upper_bound = upper;
  appState.stop_loss = stop;
  appState.initial_stop_loss = stop;
  appState.one_r_usd = Math.max(entry * 0.005, entry - stop);
  appState.invest_usdt = invest;
  appState.qty = invest / entry;
  appState.opened_at = nowStr;
  appState.alert_1r_sent = false;
  appState.alert_2r_sent = false;
  appState.last_action = `Registrata posizione manuale su ${coinSym}/USDT a ${fmtPrice(entry)}$ (${nowStr})`;

  selectedCoin = coinSym;
  persistLocalState();
  renderAll();
  switchTab("tab-live", document.querySelectorAll(".tab-btn")[0]);
  showToast(`Monitoraggio live attivato per ${coinSym}/USDT!`);
}

function moveStopToBreakEven() {
  if (!appState.active) return;
  const bePrice = Number((appState.entry_price * 1.001).toFixed(6));
  appState.stop_loss = bePrice;
  appState.alert_1r_sent = true;
  appState.last_action = `Stop Loss spostato a Pareggio (${fmtPrice(bePrice)}$) su ${appState.coin}/USDT`;
  persistLocalState();
  renderAll();
  showToast(`Stop Loss spostato a Break-Even (${fmtPrice(bePrice)}$)!`);
}

function closeActivePosition(reasonText) {
  if (!appState.active) {
    showToast("Nessuna posizione attiva da chiudere.");
    return;
  }
  const m = computeLivePositionMetrics();
  const nowStr = new Date().toLocaleString("it-IT");

  appState.active = false;
  appState.total_trades = (appState.total_trades || 0) + 1;
  if (m.netPnlUsdt >= 0) {
    appState.wins = (appState.wins || 0) + 1;
  } else {
    appState.losses = (appState.losses || 0) + 1;
  }
  appState.daily_pnl_usdt = Number(((appState.daily_pnl_usdt || 0) + m.netPnlUsdt).toFixed(2));
  appState.cumulative_pnl_usdt = Number(((appState.cumulative_pnl_usdt || 0) + m.netPnlUsdt).toFixed(2));
  appState.cumulative_pnl_pct = Number(((appState.cumulative_pnl_pct || 0) + m.netPnlPct).toFixed(2));
  appState.last_action = `CHIUSO ${m.coin}/USDT a ${fmtPrice(m.liveP)}$ (${reasonText}) | PnL: ${m.netPnlUsdt >= 0 ? '+' : ''}${m.netPnlUsdt.toFixed(2)}$ il ${nowStr}`;

  if (!Array.isArray(appState.history)) appState.history = [];
  appState.history.unshift({
    closed_at: nowStr,
    coin: m.coin,
    entry: Number(m.entry.toFixed(4)),
    exit: Number(m.liveP.toFixed(4)),
    pnl_usdt: Number(m.netPnlUsdt.toFixed(2)),
    pnl_pct: Number(m.netPnlPct.toFixed(2)),
    reason: reasonText
  });

  persistLocalState();
  renderAll();
  showToast(`Posizione ${m.coin}/USDT chiusa! PnL: ${m.netPnlUsdt >= 0 ? '+' : ''}${m.netPnlUsdt.toFixed(2)}$`);
}

function copyPionexParams() {
  const c = getCoinObj(selectedCoin);
  const txt =
`Coppia Pionex: ${c.coin}/USDT
Lower Price: ${fmtPrice(c.lower_bound)}
Upper Price: ${fmtPrice(c.upper_bound)}
Griglie: ${INITIAL_DATA.grids} (Arithmetic)
Stop Loss: ${fmtPrice(c.stop_loss)}
Capitale Operativo: ${c.risk_manager.deployable_usdt.toFixed(2)} USDT`;
  navigator.clipboard.writeText(txt).then(() => {
    showToast(`Parametri Pionex per ${c.coin}/USDT copiati negli appunti!`);
  });
}

function exportBotStateJson() {
  const blob = new Blob([JSON.stringify(appState, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "bot_state.json";
  a.click();
}

function renderAll() {
  renderAdvisorBanner();
  renderActivePositionCard();
  renderInspectCards();
  renderScannerTable();
  renderClosedPositions();
}

window.addEventListener("DOMContentLoaded", initConsoleState);
</script>
</body>
</html>
""".replace("__CONSOLE_PAYLOAD__", payload_json)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    return "index.html"


# ==============================================================================
# INVIO TELEGRAM BLINDATO (Sanitizzazione HTML + Fallback Testo Puro)
# ==============================================================================
def sanitize_telegram_html(text):
    placeholders = {
        "<b>": "[[B_OPEN]]", "</b>": "[[B_CLOSE]]",
        "<i>": "[[I_OPEN]]", "</i>": "[[I_CLOSE]]",
        "<code>": "[[C_OPEN]]", "</code>": "[[C_CLOSE]]"
    }
    for k, v in placeholders.items():
        text = text.replace(k, v)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for k, v in placeholders.items():
        text = text.replace(v, k)
    return text


def send_telegram_report(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Credenziali Telegram mancanti nei Secrets.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    safe_html = sanitize_telegram_html(text)
    if len(safe_html) > 4000:
        safe_html = safe_html[:3980] + "\n..."

    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": safe_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if resp.status_code == 200:
            print("[OK] Messaggio Telegram inviato con successo.")
            return
        print(f"[WARN] Telegram HTML resp ({resp.status_code}): {resp.text}. Provo fallback plain-text...")
    except Exception as e:
        print(f"[ERROR] Eccezione invio Telegram HTML: {e}")

    try:
        plain_text = (
            text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", "")
                .replace("<code>", "").replace("</code>", "")
        )
        if len(plain_text) > 4000:
            plain_text = plain_text[:3980] + "\n..."
        resp2 = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": plain_text,
            "disable_web_page_preview": True
        }, timeout=15)
        print(f"[INFO] Fallback plain-text status: {resp2.status_code}")
    except Exception as e2:
        print(f"[ERROR] Errore critico fallback Telegram: {e2}")


def send_telegram_webapp(file_path, best_coin, final_score, action_state):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not os.path.exists(file_path):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    caption = (
        f"🖥️ <b>CONSOLE WEB REAL-TIME (Top: {best_coin}/USDT — {final_score}/10 | {action_state})</b>\n"
        f"Tocca il file qui sopra per aprire la Console Live con prezzi in streaming WebSocket, "
        f"posizioni aperte, storico posizioni chiuse e avvisi manuali in tempo reale."
    )
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": sanitize_telegram_html(caption),
                    "parse_mode": "HTML"
                },
                files={"document": ("Console_Live_AI_Bot.html", f, "text/html")},
                timeout=25
            )
            if resp.status_code != 200:
                print(f"[WARN] Invio documento HTML status {resp.status_code}: {resp.text}")
                with open(file_path, "rb") as f2:
                    requests.post(
                        url,
                        data={
                            "chat_id": TELEGRAM_CHAT_ID,
                            "caption": f"CONSOLE WEB REAL-TIME (Top: {best_coin}/USDT - {final_score}/10)"
                        },
                        files={"document": ("Console_Live_AI_Bot.html", f2, "text/html")},
                        timeout=25
                    )
    except Exception as e:
        print(f"[ERROR] Invio WebApp Telegram fallito: {e}")


# ==============================================================================
# MAIN ORCHESTRATOR
# ==============================================================================
def main():
    state = load_state()

    eur_usd = fetch_eur_usd_rate()
    base_invest_usdt = round(INVEST_EUR * eur_usd, 2)
    total_capital_usdt = round(base_invest_usdt + max(-25.0, state.get("cumulative_pnl_usdt", 0.0)), 2)

    futures_map = fetch_all_futures_map()
    fng_val, fng_label = fetch_fear_and_greed()
    macro_events = fetch_macro_calendar()
    nyse_bell_str = check_nyse_market_bell()

    try:
        _, _, btc_c1, _ = fetch_kraken_ohlc("XBTUSD", 60)
        btc_c4 = btc_c1[::4] if len(btc_c1) >= 4 else btc_c1
        btc_ema50_4h = ema_series(btc_c4, min(50, len(btc_c4)))[-1]
        btc_trend_bull = btc_c4[-1] >= btc_ema50_4h * 0.99
    except Exception:
        btc_trend_bull = True

    all_results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_coin = {
            executor.submit(
                analyze_single_coin,
                cfg, futures_map, fng_val, macro_events, total_capital_usdt, btc_trend_bull, state
            ): cfg["coin"]
            for cfg in COIN_UNIVERSE
        }
        for fut in as_completed(future_to_coin):
            c_name = future_to_coin[fut]
            try:
                res = fut.result()
                all_results.append(res)
            except Exception as e:
                print(f"[WARN] Salto {c_name} per errore fetch: {e}")

    if not all_results:
        raise RuntimeError("Nessuna coppia analizzata correttamente da Kraken.")

    all_results.sort(
        key=lambda x: (
            1 if x["risk_manager"]["passed"] else 0,
            x["final_score"],
            x["raw_100"],
            x["time_machine"]["expected_r"]
        ),
        reverse=True
    )
    best_coin_data = all_results[0]
    all_coins_map = {r["coin"]: r for r in all_results}

    action_state, status_msg, open_pnl_usdt, open_pnl_pct, target_data, alerts_fired = manage_autonomous_position(
        state, best_coin_data, all_coins_map, macro_events
    )

    t_coin = target_data["coin"]
    t_price = target_data["price"]
    t_score = target_data["final_score"]
    t_raw = target_data["raw_100"]
    t_ai = target_data["ai_prob"]
    t_lor = target_data["lor_prob"]
    t_mlp = target_data["mlp_prob"]
    t_conf = target_data["ai_conf"]
    t_lower = target_data["lower_bound"]
    t_upper = target_data["upper_bound"]
    t_stop = target_data["stop_loss"]
    t_step = target_data["grid_step_pct"]
    t_net = target_data["net_grid_profit_pct"]
    t_tm = target_data["time_machine"]
    t_ep = target_data["exit_plan"]
    t_sc = target_data["scanner_timer"]
    t_rm = target_data["risk_manager"]
    bd = target_data["breakdown"]

    if t_score >= 8 and t_rm["passed"]:
        verdict = f"🟢 FORTE OPPORTUNITÀ GRIGLIA SU {t_coin}/USDT"
    elif t_score == 7 and t_rm["passed"]:
        verdict = f"🟢 INGRESSO GRIGLIA AUTORIZZATO SU {t_coin}/USDT"
    elif t_score >= 6:
        verdict = f"🟡 NEUTRALE / SEGNALE IN FORMAZIONE ({t_coin}/USDT)"
    else:
        verdict = "🔴 MERCATO SFAVOREVOLE / BLOCCO DIFENSIVO RISK MANAGER"

    top5_lines = []
    for idx, item in enumerate(all_results[:5], start=1):
        rm_icon = "✅" if item["risk_manager"]["passed"] else "⛔"
        sc_icon = "⚡" if item["scanner_timer"]["triggered"] else "⏳"
        top5_lines.append(
            f"{idx}. <b>{item['coin']}</b>: {item['final_score']}/10 ({item['raw_100']}pt) | "
            f"AI {item['ai_prob']*100:.0f}% | TM24h {item['time_machine']['win_or_grid_prob']:.0f}% | "
            f"Risk {rm_icon} | Timer {sc_icon}"
        )
    top5_str = "\n".join(top5_lines)

    if state["active"]:
        manual_advice = (
            f"🟢 <b>POSIZIONE APERTA SU {state['coin']}/USDT</b>: Mantieni attiva nel range "
            f"{fmt_price(state['lower_bound'])}$ - {fmt_price(state['upper_bound'])}$. "
            f"Chiudi manualmente solo se scende sotto {fmt_price(state['stop_loss'])}$ o supera {fmt_price(state['upper_bound'])}$."
        )
    elif t_score >= 7 and t_rm["passed"] and t_sc["triggered"]:
        manual_advice = (
            f"🚀 <b>AZIONE MANUALE CONSIGLIATA: APRI ORA SU {t_coin}/USDT</b> "
            f"(Range {fmt_price(t_lower)}$ - {fmt_price(t_upper)}$ | Stop {fmt_price(t_stop)}$)"
        )
    else:
        manual_advice = "⏸️ <b>AZIONE MANUALE CONSIGLIATA: NON APRIRE ORA</b> (Resta liquido in USDT)"

    alerts_block = ""
    if alerts_fired:
        alerts_block = "\n" + "\n".join(f"<b>{a}</b>" for a in alerts_fired) + "\n"

    total_t = state["total_trades"]
    win_rate = (state["wins"] / total_t * 100.0) if total_t > 0 else 0.0
    now_rome_str = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")

    report = (
        f"🤖 <b>PIONEX MULTI-COIN QUANT AI v7.1</b> ({now_rome_str})\n"
        f"📊 <b>PUNTEGGIO TOP ({t_coin}/USDT): {t_score}/10</b> <i>({t_raw}/100 pt)</i>\n"
        f"🎯 <b>{verdict}</b>\n"
        f"{alerts_block}\n"
        f"🧭 <b>INDICAZIONE OPERATIVA</b>\n"
        f"• {manual_advice}\n\n"
        f"🏆 <b>CLASSIFICA SCANNER 10 CRYPTO (Top 5 Ora)</b>\n"
        f"{top5_str}\n\n"
        f"⚙️ <b>STATO BOT AUTONOMO ({state['mode']})</b>\n"
        f"• Stato: <b>{action_state}</b>\n"
        f"• Dettaglio: {status_msg}\n"
        f"• Bilancio Chiuso: <b>{state['cumulative_pnl_usdt']:+.2f}$ ({state['cumulative_pnl_pct']:+.2f}%)</b> | "
        f"Trade: {total_t} (Win Rate: {win_rate:.0f}%)\n\n"
        f"🛡️ <b>GATEKEEPER 5-SKILL ({t_coin}/USDT)</b>\n"
        f"• <b>Risk & Budget Mgr</b>: <b>{t_rm['status_badge']}</b> ({t_rm['reason_str']})\n"
        f"• <b>Chart Scanner 15m</b>: {t_sc['pattern']} — <b>{t_sc['status_str']}</b>\n"
        f"• <b>Time Machine (24h)</b>: Win/Grid <b>{t_tm['win_or_grid_prob']:.1f}%</b> "
        f"(Target {t_tm['target_first_prob']:.1f}% | Grid {t_tm['in_range_prob']:.1f}% | Stop {t_tm['stop_first_prob']:.1f}%) | "
        f"EV: <b>{t_tm['expected_r']:+.2f}R</b>\n"
        f"• <b>Exit Planner</b>: 1R = {fmt_price(t_ep['one_r_usd'])}$ ({t_ep['one_r_pct']:.2f}%) | "
        f"R/R: <b>{t_ep['rr_ratio']:.2f}R</b> | +1R BE: {fmt_price(t_ep['level_1r'])}$ | +2R Lock: {fmt_price(t_ep['level_2r'])}$\n"
        f"• <b>Market Bell</b>: {nyse_bell_str}\n\n"
        f"🧠 <b>HYBRID NEURAL AI & MICROSTRUTTURA ({t_coin})</b>\n"
        f"• Ensemble Win Prob (12h): <b>{t_ai*100:.1f}%</b> (Lorentzian {t_lor*100:.1f}% | MLP {t_mlp*100:.1f}% | Conf. {t_conf:.0f}%)\n"
        f"• Prezzo: <b>{fmt_price(t_price)}$</b> | VWAP: {fmt_price(target_data['vwap'])}$ | POC: {fmt_price(target_data['poc'])}$\n"
        f"• RSI (15m/1h/4h): {target_data['rsi_15m']:.1f} / {target_data['rsi_1h']:.1f} / {target_data['rsi_4h']:.1f} | "
        f"ADX: {target_data['adx_1h']:.1f} | Hurst: {target_data['hurst']:.2f}\n"
        f"• Orderbook L2: <b>{target_data['ob_imbalance']*100:+.1f}%</b> | Spread: {target_data['spread_bps']:.1f} bps | "
        f"Funding 8h: {target_data['funding_pct']:+.4f}%\n"
        f"• Breakdown: Tech {bd['tech']}/24 | Econ {bd['econ']}/16 | Micro {bd['micro']}/18 | "
        f"AI {bd['ai']}/18 | TM {bd['timemachine']}/12 | Macro {bd['macro']}/12\n\n"
        f"🎛️ <b>PARAMETRI PIONEX ({t_coin}/USDT — {INVEST_EUR:.0f}€ ≈ {total_capital_usdt:.2f} USDT)</b>\n"
        f"• Capitale Operativo (95%): <b>{t_rm['deployable_usdt']:.2f} USDT</b> (Riserva 5%: {t_rm['cash_reserve_usdt']:.2f}$)\n"
        f"• Lower Bound: <code>{fmt_price(t_lower)}</code> USDT\n"
        f"• Upper Bound: <code>{fmt_price(t_upper)}</code> USDT\n"
        f"• Griglie: <code>{GRIDS}</code> (Arithmetic — Step {t_step:.2f}% | Netto {t_net:.2f}%)\n"
        f"• Stop Loss (1R): <code>{fmt_price(t_stop)}</code> USDT"
    )

    # 1. Invio immediato del report testuale su Telegram
    send_telegram_report(report)

    # 2. Generazione e invio della Console Web Real-Time allegata
    html_path = generate_webapp_html(
        state, all_results, target_data, action_state, status_msg,
        open_pnl_usdt, open_pnl_pct, fng_val, fng_label, macro_events, nyse_bell_str, total_capital_usdt
    )
    send_telegram_webapp(html_path, t_coin, t_score, action_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        err_msg = f"⚠️ ERRORE ESECUZIONE BOT:\n{exc}\n\n{traceback.format_exc()[-1200:]}"
        print(err_msg)
        send_telegram_report(err_msg)
        raise
