from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
import random
import re
import sys
import time
from zoneinfo import ZoneInfo
import requests


# ---------------------------------------------------------------------
# 1. CONFIGURAZIONE E VARIABILI D'AMBIENTE (QUANT + NEURAL + WEBAPP)
# ---------------------------------------------------------------------

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

STATE_FILE = "bot_state.json"
WEBAPP_FILE = "index.html"

AUTO_TRADE_ENABLED = (
    os.getenv("AUTO_TRADE_ENABLED", "false").strip().lower() == "true"
)
PIONEX_WEBHOOK_URL = os.getenv("PIONEX_WEBHOOK_URL", "").strip()
PIONEX_SIGNAL_TOKEN = os.getenv("PIONEX_SIGNAL_TOKEN", "").strip()
PIONEX_API_KEY = os.getenv("PIONEX_API_KEY", "").strip()
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET", "").strip()

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FNG_URL = "https://api.alternative.me/fng/?limit=1"
KRAKEN_FUTURES_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"

COUNTRY_ALIASES = {
    "us": "usd",
    "usa": "usd",
    "united states": "usd",
    "eu": "eur",
    "eurozone": "eur",
    "european union": "eur",
    "uk": "gbp",
    "gb": "gbp",
    "united kingdom": "gbp",
    "japan": "jpy",
    "jp": "jpy",
    "switzerland": "chf",
    "ch": "chf",
    "canada": "cad",
    "ca": "cad",
    "australia": "aud",
    "au": "aud",
    "new zealand": "nzd",
    "nz": "nzd",
    "china": "cny",
    "cn": "cny",
}


def normalize_country(value):
    value = str(value or "").strip().lower()
    return COUNTRY_ALIASES.get(value, value)


NEWS_COUNTRIES = {
    normalize_country(country)
    for country in os.getenv("NEWS_COUNTRIES", "USD").split(",")
    if country.strip()
} or {"usd"}

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Europe/Rome").strip()

try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    LOCAL_TZ = timezone.utc
    LOCAL_TZ_NAME = "UTC"

try:
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    NY_TZ = timezone.utc

NEWS_BLOCK_BEFORE_MIN = int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "60"))
NEWS_COOLDOWN_MIN = int(os.getenv("NEWS_COOLDOWN_MIN", "30"))
NEWS_SURPRISE_COOLDOWN_MIN = int(os.getenv("NEWS_SURPRISE_COOLDOWN_MIN", "90"))
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "3"))
NEWS_LOOKAHEAD_HOURS = int(os.getenv("NEWS_LOOKAHEAD_HOURS", "12"))

CRITICAL_KEYWORDS = [
    "fomc",
    "cpi",
    "powell",
    "interest rate",
    "non-farm",
    "nfp",
    "fed ",
    "federal funds",
    "ppi",
    "gdp",
    "core pce",
]

NEWS_MAX_15M_RANGE_PCT = float(os.getenv("NEWS_MAX_15M_RANGE_PCT", "1.25"))
NEWS_MAX_15M_MOVE_PCT = float(os.getenv("NEWS_MAX_15M_MOVE_PCT", "1.00"))

INVEST_EUR = float(os.getenv("INVEST_EUR", "50"))
MIN_ORDER_USDT = 5.0
TARGET_STEP_PCT = 0.32
PIONEX_ROUNDTRIP_FEE_PCT = 0.10
MIN_NET_STEP_PCT = 0.15


# ---------------------------------------------------------------------
# 2. SCALA DECISIONALE DA 1 A 10
# ---------------------------------------------------------------------

SCORE_ACTIONS = {
    1: {
        "title": "⛔ 1/10 — BLOCCO TOTALE (CHIUSURA D'EMERGENZA)",
        "desc": "Crollo severo in corso o news critica (FOMC/CPI) imminente. Resta 100% in USDT.",
    },
    2: {
        "title": "🔴 2/10 — PERICOLO ELEVATO (USCITA DI SICUREZZA)",
        "desc": "Veto di rischio attivo: dump di BTC/SOL, forti vendite sull'orderbook o news appena uscita.",
    },
    3: {
        "title": "🔴 3/10 — SCONSIGLIATO (STRUTTURA RIBASSISTA)",
        "desc": "Trend 4H/1H negativo e Rete Neurale pessimista. Rischio Stop Loss elevato.",
    },
    4: {
        "title": "🟠 4/10 — ATTENDI (MERCATO SFAVOREVOLE)",
        "desc": "Pressione in vendita superiore agli acquisti o compressione pericolosa.",
    },
    5: {
        "title": "🟡 5/10 — NEUTRALE (MEGLIO ASPETTARE)",
        "desc": "Prezzo sopra il VWAP o rapporto rischio/rendimento mediocre. Non forzare.",
    },
    6: {
        "title": "🟡 6/10 — QUASI PRONTO (IN OSSERVAZIONE)",
        "desc": "Struttura discreta ma mancano conferme neurali o micro-timing a 15m.",
    },
    7: {
        "title": "🟢 7/10 — AVVIO BOT AUTORIZZATO (BUON SETUP)",
        "desc": "Confluenza positiva Quant + AI Neurale. Il sistema avvia la griglia.",
    },
    8: {
        "title": "🟢 8/10 — OTTIMO SETUP (ALTA PROBABILITÀ AI)",
        "desc": "Regime mean-reverting confermato (Hurst < 0.5), orderbook solido e buon margine.",
    },
    9: {
        "title": "🔥 9/10 — SETUP ECCELLENTE (FORTE CONFLUENZA)",
        "desc": "Allineamento multi-timeframe, Rete Neurale rialzista e sconto sotto il VWAP.",
    },
    10: {
        "title": "🚀 10/10 — PERFEZIONE QUANTITATIVA & NEURALE",
        "desc": "Massimo punteggio su tutti i 9 pilastri. Condizioni ideali per il Grid Bot.",
    },
}


# ---------------------------------------------------------------------
# 3. RACCOLTA DATI MULTI-FONTE (KRAKEN SPOT, ORDERBOOK, FUTURES, F&G)
# ---------------------------------------------------------------------


def get_candles(pair="SOLUSD", interval=60):
    response = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": pair, "interval": interval},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken ({pair}): {data['error']}")

    result = data.get("result", {})
    key = next((name for name in result if name != "last"), None)
    if key is None:
        raise RuntimeError(f"Kraken non ha restituito candele per {pair}.")

    rows = result[key][:-1]
    if not rows:
        raise RuntimeError(f"Candele vuote per {pair}.")

    return {
        "time": [int(float(row[0])) for row in rows],
        "open": [float(row[1]) for row in rows],
        "high": [float(row[2]) for row in rows],
        "low": [float(row[3]) for row in rows],
        "close": [float(row[4]) for row in rows],
        "vwap": [float(row[5]) for row in rows],
        "volume": [float(row[6]) for row in rows],
    }


def get_orderbook_metrics(pair="SOLUSD", depth=50):
    try:
        response = requests.get(
            "https://api.kraken.com/0/public/Depth",
            params={"pair": pair, "count": depth},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            return {"ok": False, "imbalance": 1.0, "spread_pct": 0.05}

        result = next(iter(data["result"].values()))
        bids = result.get("bids", [])
        asks = result.get("asks", [])
        if not bids or not asks:
            return {"ok": False, "imbalance": 1.0, "spread_pct": 0.05}

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid_price * 100

        bid_vol = sum(
            float(b[1]) / (1 + i * 0.05) for i, b in enumerate(bids[:depth])
        )
        ask_vol = sum(
            float(a[1]) / (1 + i * 0.05) for i, a in enumerate(asks[:depth])
        )
        imbalance = bid_vol / ask_vol if ask_vol > 0 else 1.0
        return {"ok": True, "imbalance": imbalance, "spread_pct": spread_pct}
    except Exception:
        return {"ok": False, "imbalance": 1.0, "spread_pct": 0.05}


def get_derivatives_and_sentiment():
    deriv = {"ok": False, "funding_pct": 0.0, "oi_usd": 0.0}
    fng = {"ok": False, "value": 50, "label": "Neutral"}

    try:
        resp = requests.get(KRAKEN_FUTURES_URL, timeout=10)
        if resp.status_code == 200:
            tickers = resp.json().get("tickers", [])
            sol_perp = next(
                (t for t in tickers if t.get("symbol") == "PF_SOLUSD"), None
            )
            if sol_perp:
                raw_fr = float(sol_perp.get("fundingRate", 0.0))
                mark_p = float(sol_perp.get("markPrice", 1.0))
                fr_pct = (
                    (raw_fr / mark_p * 100)
                    if abs(raw_fr) > 0.005
                    else (raw_fr * 100)
                )
                oi = float(sol_perp.get("openInterest", 0.0)) * mark_p
                deriv = {"ok": True, "funding_pct": fr_pct, "oi_usd": oi}
    except Exception:
        pass

    try:
        resp = requests.get(FNG_URL, timeout=8)
        if resp.status_code == 200:
            item = resp.json().get("data", [{}])[0]
            fng = {
                "ok": True,
                "value": int(item.get("value", 50)),
                "label": item.get("value_classification", "Neutral"),
            }
    except Exception:
        pass

    return deriv, fng


def get_eurusd():
    try:
        response = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=EURUSD",
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            return 1.08
        ticker = next(iter(data["result"].values()))
        return float(ticker["c"][0])
    except Exception:
        return 1.08


# ---------------------------------------------------------------------
# 4. INDICATORI QUANTITATIVI, ESPONENTE DI HURST E RETE NEURALE (MLP)
# ---------------------------------------------------------------------


def ema_series(values, n):
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for val in values[1:]:
        out.append(val * k + out[-1] * (1 - k))
    return out


def ema(values, n):
    s = ema_series(values, n)
    return s[-1] if s else 0.0


def rsi_series(closes, n=14):
    if len(closes) <= n:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
    first_rsi = 100.0 - (100.0 / (1.0 + rs))
    out = [50.0] * n + [first_rsi]

    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def rsi(closes, n=14):
    return rsi_series(closes, n)[-1]


def stoch_rsi(closes, rsi_len=14, stoch_len=14):
    r_ser = rsi_series(closes, rsi_len)
    if len(r_ser) < stoch_len:
        return 50.0
    window = r_ser[-stoch_len:]
    low_r, high_r = min(window), max(window)
    if high_r - low_r == 0:
        return 50.0
    return (r_ser[-1] - low_r) / (high_r - low_r) * 100.0


def macd_metrics(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return 0.0, 0.0, False, [0.0] * len(closes)
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_s, slow_s)]
    sig_line = ema_series(macd_line, sig)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    return macd_line[-1], hist[-1], (hist[-1] > hist[-2]), hist


def bollinger_bands(closes, n=20, mult=2.0):
    if len(closes) < n:
        p = closes[-1] if closes else 1.0
        return p * 0.98, p, p * 1.02, 0.5, 4.0
    window = closes[-n:]
    mid = sum(window) / n
    variance = sum((x - mid) ** 2 for x in window) / n
    std = math.sqrt(variance)
    up = mid + mult * std
    low = mid - mult * std
    pct_b = (closes[-1] - low) / (up - low) if (up - low) > 0 else 0.5
    bw_pct = (up - low) / mid * 100 if mid > 0 else 0.0
    return low, mid, up, pct_b, bw_pct


def hurst_exponent(closes):
    if len(closes) < 100:
        return 0.50
    lags = [2, 4, 8, 16, 32]
    tau = []
    log_p = [math.log(max(c, 1e-8)) for c in closes[-120:]]
    for lag in lags:
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        mean_d = sum(diffs) / len(diffs)
        std_d = math.sqrt(sum((d - mean_d) ** 2 for d in diffs) / len(diffs))
        tau.append(max(std_d, 1e-8))

    x = [math.log(l) for l in lags]
    y = [math.log(t) for t in tau]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    num = sum((x[i] - mx) * (y[i] - my) for i in range(len(x)))
    den = sum((x[i] - mx) ** 2 for i in range(len(x)))
    h = num / den if den > 0 else 0.50
    return max(0.10, min(0.90, h))


def variance_ratio_mr(closes, lag=6):
    if len(closes) < 72:
        return 1.0
    log_p = [math.log(max(c, 1e-8)) for c in closes[-72:]]
    r1 = [log_p[i] - log_p[i - 1] for i in range(1, len(log_p))]
    rk = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
    if not r1 or not rk:
        return 1.0
    m1, mk = sum(r1) / len(r1), sum(rk) / len(rk)
    var1 = sum((x - m1) ** 2 for x in r1) / len(r1)
    vark = sum((x - mk) ** 2 for x in rk) / len(rk)
    return (vark / (lag * var1)) if var1 > 0 else 1.0


def atr_adx_di(high, low, close, n=14):
    if len(close) <= n * 2:
        return 0.0, 0.0, 0.0, 0.0, []

    true_ranges, positive_dm, negative_dm = [], [], []
    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        positive_dm.append(up if up > down and up > 0 else 0)
        negative_dm.append(down if down > up and down > 0 else 0)
        true_ranges.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )

    smoothed_tr = sum(true_ranges[:n])
    smoothed_p = sum(positive_dm[:n])
    smoothed_m = sum(negative_dm[:n])

    dx_values, atr_series = [], []
    pdi, mdi = 0.0, 0.0

    for i in range(n, len(true_ranges) + 1):
        if i > n:
            smoothed_tr = smoothed_tr - smoothed_tr / n + true_ranges[i - 1]
            smoothed_p = smoothed_p - smoothed_p / n + positive_dm[i - 1]
            smoothed_m = smoothed_m - smoothed_m / n + negative_dm[i - 1]

        atr_series.append(smoothed_tr / n)
        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue

        pdi = 100 * smoothed_p / smoothed_tr
        mdi = 100 * smoothed_m / smoothed_tr
        dx_values.append(
            0.0 if (pdi + mdi == 0) else 100 * abs(pdi - mdi) / (pdi + mdi)
        )

    seed = dx_values[:n]
    adx_value = sum(seed) / len(seed)
    for dx in dx_values[n:]:
        adx_value = (adx_value * (n - 1) + dx) / n

    return smoothed_tr / n, adx_value, pdi, mdi, atr_series


def _sigmoid(x):
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def run_neural_and_lorentzian_ai(high, low, close, volume):
    n_len = len(close)
    if n_len < 150:
        return 0.50, 0.50, 0.50

    rsi_s = rsi_series(close, 14)
    ema20_s = ema_series(close, 20)
    ema50_s = ema_series(close, 50)
    _, _, _, hist_s = macd_metrics(close)

    features, targets = [], []
    horizon = 6

    start_idx = max(30, n_len - 450)
    for i in range(start_idx, n_len):
        p = close[i]
        rsi_val = rsi_s[i] if i < len(rsi_s) else rsi_s[-1]
        e20_val = ema20_s[i] if i < len(ema20_s) else ema20_s[-1]
        e50_val = ema50_s[i] if i < len(ema50_s) else ema50_s[-1]
        h_val = hist_s[i] if i < len(hist_s) else hist_s[-1]

        f1 = (rsi_val - 50.0) / 25.0
        f2 = (p / e20_val - 1.0) * 25.0 if e20_val > 0 else 0.0
        f3 = (e20_val / e50_val - 1.0) * 25.0 if e50_val > 0 else 0.0
        f4 = (h_val / p) * 200.0 if p > 0 else 0.0
        f5 = (p / close[i - 4] - 1.0) * 20.0
        avg_v = sum(volume[i - 20 : i]) / 20.0 if i >= 20 else volume[i]
        f6 = min(2.5, (volume[i] / avg_v) - 1.0) if avg_v > 0 else 0.0

        feat_vec = [
            max(-3.0, min(3.0, val)) for val in (f1, f2, f3, f4, f5, f6)
        ]
        features.append(feat_vec)

        if i < n_len - horizon:
            future_high = max(high[i + 1 : i + 1 + horizon])
            future_low = min(low[i + 1 : i + 1 + horizon])
            win = (
                1.0
                if (future_high >= p * 1.0065 and future_low >= p * 0.982)
                else 0.0
            )
            targets.append(win)

    train_x = features[:-horizon]
    current_x = features[-1]

    if len(train_x) < 60 or len(targets) < 60:
        return 0.50, 0.50, 0.50

    distances = []
    for idx, tx in enumerate(train_x):
        d = sum(math.log(1.0 + abs(current_x[j] - tx[j])) for j in range(6))
        distances.append((d, targets[idx]))
    distances.sort(key=lambda item: item[0])
    k_nearest = distances[:15]
    lorentzian_prob = sum(t for _, t in k_nearest) / len(k_nearest)

    rng = random.Random(42)
    n_in, n_hid = 6, 5
    w1 = [[rng.uniform(-0.35, 0.35) for _ in range(n_in)] for _ in range(n_hid)]
    b1 = [0.0] * n_hid
    w2 = [rng.uniform(-0.35, 0.35) for _ in range(n_hid)]
    b2 = 0.0
    lr = 0.045

    for _ in range(30):
        for x_vec, y_true in zip(train_x, targets):
            h_act = [
                math.tanh(
                    sum(w1[h][j] * x_vec[j] for j in range(n_in)) + b1[h]
                )
                for h in range(n_hid)
            ]
            out_pred = _sigmoid(
                sum(w2[h] * h_act[h] for h in range(n_hid)) + b2
            )

            err = out_pred - y_true
            d_hid = [
                err * w2[h] * (1.0 - h_act[h] ** 2) for h in range(n_hid)
            ]

            for h in range(n_hid):
                w2[h] -= lr * err * h_act[h]
                b1[h] -= lr * d_hid[h]
                for j in range(n_in):
                    w1[h][j] -= lr * d_hid[h] * x_vec[j]
            b2 -= lr * err

    h_curr = [
        math.tanh(sum(w1[h][j] * current_x[j] for j in range(n_in)) + b1[h])
        for h in range(n_hid)
    ]
    mlp_prob = _sigmoid(sum(w2[h] * h_curr[h] for h in range(n_hid)) + b2)

    ensemble_prob = 0.60 * mlp_prob + 0.40 * lorentzian_prob
    return ensemble_prob, mlp_prob, lorentzian_prob


def percentile(values, p):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


# ---------------------------------------------------------------------
# 5. MOTORE QUANT + NEURALE A 9 PILASTRI (100 PUNTI -> SCALA 1-10)
# ---------------------------------------------------------------------


def analyze_btc(sol_chg4h, sol_chg24h):
    try:
        btc = get_candles(pair="XBTUSD", interval=60)
        close, high, low = btc["close"], btc["high"], btc["low"]

        price = close[-1]
        chg4h = (price / close[-5] - 1) * 100
        chg24h = (price / close[-25] - 1) * 100
        ema200 = ema(close[-300:], 200)
        _, adx, pdi, mdi, _ = atr_adx_di(high[-250:], low[-250:], close[-250:])

        rel_24h = sol_chg24h - chg24h
        is_dumping = chg4h <= -2.8 or chg24h <= -6.0
        strong_bear = price < ema200 and mdi > pdi and adx > 28

        if is_dumping or strong_bear:
            return {
                "safe": False,
                "cap": 2 if chg4h <= -3.5 else 3,
                "chg4h": chg4h,
                "chg24h": chg24h,
                "rel_24h": rel_24h,
                "status": f"🔴 BTC Ostile ({price:.0f}$, 4h: {chg4h:+.1f}%, ADX {adx:.0f}) | Alpha SOL: {rel_24h:+.1f}%",
            }
        return {
            "safe": True,
            "cap": 10,
            "chg4h": chg4h,
            "chg24h": chg24h,
            "rel_24h": rel_24h,
            "status": f"🟢 BTC Stabile ({price:.0f}$, 4h: {chg4h:+.1f}%) | Alpha SOL: {rel_24h:+.1f}%",
        }
    except Exception as exc:
        return {
            "safe": False,
            "cap": 5,
            "chg4h": 0.0,
            "chg24h": 0.0,
            "rel_24h": 0.0,
            "status": f"🟠 BTC non verificabile ({type(exc).__name__})",
        }


def analyze():
    c1h = get_candles(pair="SOLUSD", interval=60)
    c4h = get_candles(pair="SOLUSD", interval=240)
    c15m = get_candles(pair="SOLUSD", interval=15)

    ob = get_orderbook_metrics(pair="SOLUSD", depth=50)
    deriv, fng = get_derivatives_and_sentiment()

    opens, high, low, close, volume = (
        c1h["open"],
        c1h["high"],
        c1h["low"],
        c1h["close"],
        c1h["volume"],
    )
    if len(close) < 250:
        raise RuntimeError(f"Candele 1H insufficienti: {len(close)}/250.")

    price = close[-1]

    ema200_1h = ema(close[-350:], 200)
    rsi_1h = rsi(close[-250:], 14)
    _, macd_hist, macd_rising, _ = macd_metrics(close[-250:])
    _, bb_mid, _, bb_pct_b, bb_width = bollinger_bands(close[-250:], 20, 2.0)
    atr, adx, pdi, mdi, atr_series = atr_adx_di(
        high[-250:], low[-250:], close[-250:]
    )

    hurst = hurst_exponent(close[-150:])
    vr_ratio = variance_ratio_mr(close[-120:], lag=6)
    ai_prob, mlp_prob, lor_prob = run_neural_and_lorentzian_ai(
        high, low, close, volume
    )

    vol_24h = sum(volume[-24:])
    vwap_24h = (
        sum(
            ((high[-i] + low[-i] + close[-i]) / 3) * volume[-i]
            for i in range(1, 25)
        )
        / vol_24h
        if vol_24h > 0
        else price
    )

    close_4h = c4h["close"]
    ema50_4h = ema(close_4h[-200:], 50)
    ema200_4h = ema(close_4h[-250:], 200)
    rsi_4h = rsi(close_4h[-150:], 14)

    close_15m = c15m["close"]
    rsi_15m = rsi(close_15m[-100:], 14)
    srsi_15m = stoch_rsi(close_15m[-100:], 14, 14)
    micro_bounce = close_15m[-1] >= c15m["open"][-1] or close_15m[-1] > ema(
        close_15m[-40:], 9
    )

    chg4h = (price / close[-5] - 1) * 100
    chg24 = (price / close[-25] - 1) * 100
    avg_atr_7d = (
        sum(atr_series[-168:]) / len(atr_series[-168:]) if atr_series else atr
    )
    atr_ratio = atr / avg_atr_7d if avg_atr_7d > 0 else 1.0

    kc_width = (atr * 3.0) / bb_mid * 100 if bb_mid > 0 else 0.0
    is_keltner_squeeze = bb_width < kc_width and atr_ratio < 0.70

    avg_vol_7d = sum(volume[-168:]) / 168 if sum(volume[-168:]) > 0 else 1.0
    red_vols_3h = [volume[-i] for i in range(1, 4) if close[-i] < opens[-i]]
    max_red_vol_ratio = (max(red_vols_3h) / avg_vol_7d) if red_vols_3h else 0.0

    daily_ranges = [
        (
            max(high[-(d + 1) * 24 : len(high) - d * 24])
            - min(low[-(d + 1) * 24 : len(low) - d * 24])
        )
        / price
        * 100
        for d in range(7)
    ]
    avg_daily = sum(daily_ranges) / len(daily_ranges)

    lower = min(max(percentile(low[-168:], 0.10), price * 0.92), price * 0.960)
    upper = max(min(percentile(high[-168:], 0.90), price * 1.08), price * 1.040)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()
    max_grids_by_money = max(1, int(invest_usdt // MIN_ORDER_USDT))
    target_grids = max(5, min(50, round(width_pct / TARGET_STEP_PCT)))
    grids = min(target_grids, max_grids_by_money)

    step_pct = width_pct / grids
    net_step_pct = step_pct - PIONEX_ROUNDTRIP_FEE_PCT
    rotation_ratio = avg_daily / width_pct if width_pct > 0 else 0.0
    stop_loss = lower * 0.978
    position_in_range = (price - lower) / (upper - lower) * 100

    btc_info = analyze_btc(chg4h, chg24)

    pts = 0
    reasons = []
    risk_vetoes = []
    tech_cap = 10

    if not btc_info["safe"]:
        tech_cap = min(tech_cap, btc_info["cap"])
        risk_vetoes.append(btc_info["status"])

    # PILASTRO 1: RETE NEURALE MLP & LORENTZIAN k-NN AI — Max 15 pt
    ai_pct = ai_prob * 100
    if ai_prob < 0.38:
        p1 = 0
        tech_cap = min(tech_cap, 4)
        risk_vetoes.append(
            f"🚨 Veto AI Neurale: Probabilità di successo troppo bassa ({ai_pct:.0f}%)."
        )
    elif ai_prob >= 0.64:
        p1 = 15
    elif ai_prob >= 0.55:
        p1 = 12
    elif ai_prob >= 0.48:
        p1 = 8
    else:
        p1 = 4
    pts += p1
    reasons.append(
        f"{'🟢' if p1>=12 else ('🟡' if p1>=8 else '🔴')} 1. AI Neurale & Lorentzian: Win Prob {ai_pct:.0f}% (MLP:{mlp_prob*100:.0f}% | Lor:{lor_prob*100:.0f}%) ({p1}/15)"
    )

    # PILASTRO 2: STRUTTURA MULTI-TIMEFRAME (4H + 1H) — Max 15 pt
    bear_1h = price < ema200_1h and mdi > pdi
    bear_4h = price < ema200_4h and ema50_4h < ema200_4h
    if bear_1h and bear_4h and (adx > 25 or chg24 < -5.0):
        p2 = 0
        tech_cap = min(tech_cap, 3)
        risk_vetoes.append(
            "🚨 Veto MTF: Trend ribassista allineato su 4H e 1H."
        )
    elif price >= ema200_1h and price >= ema50_4h and adx < 24:
        p2 = 15
    elif price >= ema200_1h or (not bear_1h and adx < 26):
        p2 = 11
    elif not bear_4h and adx < 30:
        p2 = 7
    else:
        p2 = 3
    pts += p2
    reasons.append(
        f"{'🟢' if p2>=11 else ('🟡' if p2>=7 else '🔴')} 2. Trend MTF (4H/1H): EMA200 1H {ema200_1h:.1f}$ | 4H {ema200_4h:.1f}$ | ADX {adx:.0f} ({p2}/15)"
    )

    # PILASTRO 3: FRATTALI DI HURST & REGIME MEAN-REVERSION — Max 12 pt
    if is_keltner_squeeze:
        p3 = 2
        tech_cap = min(tech_cap, 4)
        risk_vetoes.append(
            "⚠️ Keltner Squeeze attivo: volatilità compressa, possibile esplosione."
        )
    elif hurst <= 0.48 and vr_ratio <= 0.98 and 0.70 <= atr_ratio <= 1.40:
        p3 = 12
    elif hurst <= 0.54 and 0.65 <= atr_ratio <= 1.60:
        p3 = 9
    elif 0.58 <= atr_ratio <= 1.75:
        p3 = 5
    else:
        p3 = 2
    pts += p3
    reasons.append(
        f"{'🟢' if p3>=9 else ('🟡' if p3>=5 else '🔴')} 3. Frattale Hurst & ATR: H={hurst:.2f} ({'Mean-Rev' if hurst<0.5 else 'Trend'}) | VR {vr_ratio:.2f} | ATR {atr_ratio:.2f}x ({p3}/12)"
    )

    # PILASTRO 4: POSIZIONE NEL CANALE & VWAP 24H ISTITUZIONALE — Max 13 pt
    vwap_diff_pct = (price / vwap_24h - 1) * 100
    if position_in_range > 74:
        p4 = 2
        tech_cap = min(tech_cap, 5)
        risk_vetoes.append(
            f"⚠️ Prezzo troppo alto nel canale ({position_in_range:.0f}% > 74%)."
        )
    elif (
        18 <= position_in_range <= 54
        and price <= vwap_24h * 1.006
        and 0.12 <= bb_pct_b <= 0.62
    ):
        p4 = 13
    elif 15 <= position_in_range <= 64 and vwap_diff_pct <= 1.2:
        p4 = 10
    elif 10 <= position_in_range <= 74:
        p4 = 6
    else:
        p4 = 2
    pts += p4
    reasons.append(
        f"{'🟢' if p4>=10 else ('🟡' if p4>=6 else '🔴')} 4. Valore & VWAP: Pos {position_in_range:.0f}% | vs VWAP {vwap_diff_pct:+.1f}% | %B {bb_pct_b:.2f} ({p4}/13)"
    )

    # PILASTRO 5: MOMENTUM & MICRO-TIMING (MACD + RSI 1H/15m) — Max 12 pt
    if rsi_1h < 28 or (not macd_rising and macd_hist < 0 and chg4h < -2.5):
        p5 = 2
    elif (
        macd_rising
        and 36 <= rsi_1h <= 58
        and 28 <= rsi_15m <= 64
        and micro_bounce
    ):
        p5 = 12
    elif (macd_rising or micro_bounce) and 32 <= rsi_1h <= 65:
        p5 = 9
    elif 30 <= rsi_1h <= 68:
        p5 = 6
    else:
        p5 = 2
    pts += p5
    reasons.append(
        f"{'🟢' if p5>=9 else ('🟡' if p5>=6 else '🔴')} 5. Momentum (1H/15m): MACD {'📈' if macd_rising else '📉'} | RSI 1h:{rsi_1h:.0f} 15m:{rsi_15m:.0f} | Stoch:{srsi_15m:.0f} ({p5}/12)"
    )

    # PILASTRO 6: ORDERBOOK DEPTH & VOLUMI SMART MONEY — Max 10 pt
    imb = ob["imbalance"]
    if max_red_vol_ratio >= 2.3 or chg4h <= -3.8:
        p6 = 0
        tech_cap = min(tech_cap, 2)
        risk_vetoes.append(
            f"🚨 Veto Volumi: Picco vendite {max_red_vol_ratio:.1f}x media (4h: {chg4h:+.1f}%)."
        )
    elif imb >= 1.12 and max_red_vol_ratio < 1.3:
        p6 = 10
    elif imb >= 0.90 and max_red_vol_ratio < 1.6:
        p6 = 7
    elif imb >= 0.75:
        p6 = 4
    else:
        p6 = 2
    pts += p6
    reasons.append(
        f"{'🟢' if p6>=7 else ('🟡' if p6>=4 else '🔴')} 6. Orderbook & Volumi: Bid/Ask {imb:.2f}x | Vol Rosso Max {max_red_vol_ratio:.1f}x ({p6}/10)"
    )

    # PILASTRO 7: CORRELAZIONE BTC & FORZA RELATIVA (ALPHA) — Max 8 pt
    rel = btc_info["rel_24h"]
    if not btc_info["safe"]:
        p7 = 0
    elif rel >= 0.5 and btc_info["chg4h"] >= -0.8:
        p7 = 8
    elif rel >= -1.5 and btc_info["chg4h"] >= -1.5:
        p7 = 6
    elif rel >= -3.0:
        p7 = 3
    else:
        p7 = 1
    pts += p7
    reasons.append(
        f"{'🟢' if p7>=6 else ('🟡' if p7>=3 else '🔴')} 7. Forza Relativa BTC: Alpha SOL {rel:+.1f}% | BTC 4h {btc_info['chg4h']:+.1f}% ({p7}/8)"
    )

    # PILASTRO 8: DERIVATI (FUNDING RATE) & SENTIMENT — Max 7 pt
    fr = deriv["funding_pct"]
    fg_val = fng["value"]
    if fr > 0.05 or fg_val > 82:
        p8 = 2
    elif -0.02 <= fr <= 0.025 and 25 <= fg_val <= 72:
        p8 = 7
    elif -0.04 <= fr <= 0.04 and 18 <= fg_val <= 78:
        p8 = 5
    else:
        p8 = 3
    pts += p8
    fr_str = f"{fr:+.4f}%" if deriv["ok"] else "N/D"
    reasons.append(
        f"{'🟢' if p8>=5 else '🟡'} 8. Derivati & Sentiment: Funding {fr_str} | F&G {fg_val} ({fng['label']}) ({p8}/7)"
    )

    # PILASTRO 9: ECONOMIA DELLA GRIGLIA (MARGINE NETTO & ROTAZIONE) — Max 8 pt
    if (
        net_step_pct >= MIN_NET_STEP_PCT
        and 0.38 <= rotation_ratio <= 0.95
        and 3.0 <= avg_daily <= 9.0
    ):
        p9 = 8
    elif net_step_pct >= 0.12 and rotation_ratio >= 0.28:
        p9 = 6
    elif net_step_pct >= 0.09:
        p9 = 3
    else:
        p9 = 1
    pts += p9
    reasons.append(
        f"{'🟢' if p9>=6 else ('🟡' if p9>=3 else '🔴')} 9. Efficienza Griglia: Netto +{net_step_pct:.2f}% | Rotazione {rotation_ratio:.2f}x/gg ({p9}/8)"
    )

    raw_tech_score = max(1, min(10, int(round(pts / 10.0))))
    capped_tech_score = min(raw_tech_score, tech_cap)

    return {
        "price": price,
        "vwap_24h": vwap_24h,
        "ema200_1h": ema200_1h,
        "ema200_4h": ema200_4h,
        "rsi_4h": rsi_4h,
        "hurst": hurst,
        "ai_prob": ai_prob,
        "mlp_prob": mlp_prob,
        "lor_prob": lor_prob,
        "pdi": pdi,
        "mdi": mdi,
        "chg4h": chg4h,
        "chg24": chg24,
        "lower": lower,
        "upper": upper,
        "grids": grids,
        "step_pct": step_pct,
        "net_step_pct": net_step_pct,
        "stop": stop_loss,
        "pos": position_in_range,
        "pts_100": pts,
        "raw_tech_score": raw_tech_score,
        "score": capped_tech_score,
        "tech_cap": tech_cap,
        "reasons": reasons,
        "risk_vetoes": risk_vetoes,
        "btc_info": btc_info,
        "invest_usdt": invest_usdt,
        "candle_times": c1h["time"],
        "recent_closes": close[-48:],
    }


# ---------------------------------------------------------------------
# 6. CALENDARIO ECONOMICO MACRO
# ---------------------------------------------------------------------


def parse_calendar_time(value):
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    event_time = datetime.fromisoformat(raw)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time.astimezone(timezone.utc)


def extract_number(val_str):
    if not val_str:
        return None
    match = re.search(r"[-+]?\d*\.?\d+", str(val_str).replace(",", ""))
    return float(match.group(0)) if match else None


def check_surprise_factor(actual_str, estimate_str):
    act = extract_number(actual_str)
    est = extract_number(estimate_str)
    if act is None or est is None:
        return False, 0.0
    diff = abs(act - est)
    if abs(est) > 0.5:
        rel_pct = (diff / abs(est)) * 100
        return rel_pct >= 15.0, rel_pct
    return diff >= 0.2, diff * 100


def is_wall_street_open_window(now_utc):
    ny_time = now_utc.astimezone(NY_TZ)
    if ny_time.weekday() >= 5:
        return False, ny_time.strftime("%H:%M")
    minutes_of_day = ny_time.hour * 60 + ny_time.minute
    in_window = 560 <= minutes_of_day <= 605
    return in_window, ny_time.strftime("%H:%M")


def fetch_macro_events(now):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; NeuralGridBot/5.0)",
    }
    response = requests.get(CALENDAR_URL, headers=headers, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")

    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Formato JSON non valido.")

    events = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("date"):
            continue

        country = str(item.get("country") or "").strip()
        if normalize_country(country) not in NEWS_COUNTRIES:
            continue

        impact = str(item.get("impact") or "unknown").strip().lower()
        if impact not in ("high", "medium"):
            continue

        try:
            event_time = parse_calendar_time(item["date"])
        except (TypeError, ValueError):
            continue

        minutes_to_event = (event_time - now).total_seconds() / 60
        if not (
            -NEWS_LOOKBACK_HOURS * 60
            <= minutes_to_event
            <= NEWS_LOOKAHEAD_HOURS * 60
        ):
            continue

        title = str(item.get("title") or item.get("event") or "Evento")
        is_critical = any(kw in title.lower() for kw in CRITICAL_KEYWORDS)
        actual = str(item.get("actual") or "").strip()
        estimate = str(
            item.get("forecast") or item.get("estimate") or ""
        ).strip()
        is_surprise, surprise_val = check_surprise_factor(actual, estimate)

        events.append(
            {
                "dt": event_time,
                "local_dt": event_time.astimezone(LOCAL_TZ),
                "country": country.upper(),
                "impact": impact,
                "is_critical": is_critical,
                "name": title,
                "actual": actual,
                "estimate": estimate,
                "is_surprise": is_surprise,
                "surprise_val": surprise_val,
                "mins_diff": minutes_to_event,
            }
        )

    events.sort(key=lambda e: e["dt"])
    return events


def get_post_news_reaction(event):
    candles = get_candles(pair="SOLUSD", interval=5)
    times, closes = candles["time"], candles["close"]
    if len(closes) < 4:
        return None

    interval_seconds = 300
    release_ts = event["dt"].timestamp()
    first_post_ts = math.ceil(release_ts / interval_seconds) * interval_seconds

    post_indices = [i for i, ts in enumerate(times) if ts >= first_post_ts]
    pre_indices = [
        i for i, ts in enumerate(times) if ts + interval_seconds <= release_ts
    ]

    if not pre_indices or len(post_indices) < 3:
        return None

    last_three = post_indices[-3:]
    pre_price = closes[pre_indices[-1]]
    current_price = closes[last_three[-1]]
    reaction_pct = (current_price / pre_price - 1) * 100
    move_15m_pct = (closes[last_three[-1]] / closes[last_three[0]] - 1) * 100

    high_15m = max(candles["high"][i] for i in last_three)
    low_15m = min(candles["low"][i] for i in last_three)
    range_15m_pct = (high_15m - low_15m) / current_price * 100

    stable = (
        abs(move_15m_pct) <= NEWS_MAX_15M_MOVE_PCT
        and range_15m_pct <= NEWS_MAX_15M_RANGE_PCT
    )
    direction = (
        "rialzista 📈"
        if reaction_pct > 0.30
        else ("ribassista 📉" if reaction_pct < -0.30 else "neutra ➖")
    )

    return {
        "reaction_pct": reaction_pct,
        "move_15m_pct": move_15m_pct,
        "range_15m_pct": range_15m_pct,
        "stable": stable,
        "direction": direction,
    }


def format_event_line(ev):
    icon = (
        "🔥"
        if ev["is_critical"]
        else ("🔴" if ev["impact"] == "high" else "🟠")
    )
    time_str = ev["local_dt"].strftime("%H:%M")
    mins = round(ev["mins_diff"])
    timing = f"tra {mins} min" if mins > 0 else f"{abs(mins)} min fa"

    data_str = ""
    if ev["actual"] or ev["estimate"]:
        act = ev["actual"] if ev["actual"] else "In attesa"
        est = f" (Prev: {ev['estimate']})" if ev["estimate"] else ""
        surp = " ⚡[SORPRESA!]" if ev["is_surprise"] else ""
        data_str = f" — Dati: {act}{est}{surp}"

    return (
        f"{icon} [{time_str}] {ev['country']} {ev['name']} ({timing}){data_str}"
    )


def get_news_context():
    now = datetime.now(timezone.utc)
    ws_open_active, ny_time_str = is_wall_street_open_window(now)

    try:
        events = fetch_macro_events(now)
    except Exception as exc:
        return {
            "ok": False,
            "evaluated_lines": [f"⚠️ Errore lettura calendario: {exc}"],
            "explanation": "Calendario macro non verificabile: cautela (-1 pt, Max 6/10).",
            "penalty": 1,
            "max_score_cap": 6,
            "ws_status": f"NY Time: {ny_time_str}",
        }

    evaluated_lines = [format_event_line(e) for e in events[:10]]
    if not evaluated_lines:
        evaluated_lines = [
            "✅ Nessuna notizia ad alto/medio impatto nelle vicinanze."
        ]

    high_impact = [e for e in events if e["impact"] == "high"]
    med_impact = [e for e in events if e["impact"] == "medium"]

    if ws_open_active and high_impact:
        return {
            "ok": True,
            "evaluated_lines": evaluated_lines,
            "explanation": f"🔔 Apertura Wall Street ({ny_time_str} NY) in giornata di news: attendi le 10:05 NY (Max 4/10).",
            "penalty": 0,
            "max_score_cap": 4,
            "ws_status": f"🔴 Finestra Apertura NY ({ny_time_str})",
        }

    upcoming_high = [
        e for e in high_impact if 0 <= e["mins_diff"] <= NEWS_BLOCK_BEFORE_MIN
    ]
    if upcoming_high:
        ev = min(upcoming_high, key=lambda e: e["dt"])
        mins = math.ceil(ev["mins_diff"])
        cap = 1 if (mins <= 20 and ev["is_critical"]) else 3
        return {
            "ok": True,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Blocco Pre-News: '{ev['name']}' uscirà tra {mins} min (Max {cap}/10).",
            "penalty": 0,
            "max_score_cap": cap,
            "ws_status": f"🟢 NY Time: {ny_time_str}",
        }

    for ev in reversed(high_impact):
        if ev["mins_diff"] <= 0:
            elapsed = abs(ev["mins_diff"])
            req_cooldown = (
                NEWS_SURPRISE_COOLDOWN_MIN
                if ev["is_surprise"]
                else NEWS_COOLDOWN_MIN
            )
            if elapsed < req_cooldown:
                rem = math.ceil(req_cooldown - elapsed)
                surp_txt = (
                    " (DATO A SORPRESA!)" if ev["is_surprise"] else ""
                )
                return {
                    "ok": True,
                    "evaluated_lines": evaluated_lines,
                    "explanation": f"Cooldown attivo{surp_txt}: '{ev['name']}' uscita da {round(elapsed)}m. Attendi {rem}m (Max 2/10).",
                    "penalty": 0,
                    "max_score_cap": 2,
                    "ws_status": f"🟢 NY Time: {ny_time_str}",
                }

    post_high = [
        e for e in high_impact if -NEWS_LOOKBACK_HOURS * 60 <= e["mins_diff"] < 0
    ]
    if post_high:
        ev = max(post_high, key=lambda e: e["dt"])
        try:
            reaction = get_post_news_reaction(ev)
        except Exception:
            reaction = None

        if reaction is not None:
            reac_text = (
                f"Reazione SOL a '{ev['name']}': {reaction['direction']} ({reaction['reaction_pct']:+.2f}%), "
                f"range 15m del {reaction['range_15m_pct']:.2f}%."
            )
            if not reaction["stable"]:
                return {
                    "ok": True,
                    "evaluated_lines": evaluated_lines,
                    "explanation": f"{reac_text} ⚠️ Mercato ancora instabile post-news (Max 4/10).",
                    "penalty": 0,
                    "max_score_cap": 4,
                    "ws_status": f"🟢 NY Time: {ny_time_str}",
                }
            return {
                "ok": True,
                "evaluated_lines": evaluated_lines,
                "explanation": f"{reac_text} ✅ Volatilità post-news rientrata.",
                "penalty": 0,
                "max_score_cap": 10,
                "ws_status": f"🟢 NY Time: {ny_time_str}",
            }

    upcoming_med = [e for e in med_impact if -10 <= e["mins_diff"] <= 30]
    if upcoming_med:
        ev = upcoming_med[0]
        return {
            "ok": True,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Notizia a medio impatto imminente ('{ev['name']}'). Cautela (-1 pt).",
            "penalty": 1,
            "max_score_cap": 8,
            "ws_status": f"🟢 NY Time: {ny_time_str}",
        }

    return {
        "ok": True,
        "evaluated_lines": evaluated_lines,
        "explanation": "✅ Via libera macroeconomico: nessuna notizia ostile imminente.",
        "penalty": 0,
        "max_score_cap": 10,
        "ws_status": f"🟢 NY Time: {ny_time_str}",
    }


# ---------------------------------------------------------------------
# 7. ESECUZIONE AUTONOMA E STORICO OPERAZIONI
# ---------------------------------------------------------------------


def load_bot_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if "history" not in data:
                    data["history"] = []
                return data
        except Exception:
            pass
    if ACTIVE_LOWER and ACTIVE_UPPER:
        try:
            low_b, up_b = float(ACTIVE_LOWER), float(ACTIVE_UPPER)
            return {
                "active": True,
                "entry_price": round((low_b + up_b) / 2, 2),
                "highest_price": round((low_b + up_b) / 2, 2),
                "lower": low_b,
                "upper": up_b,
                "stop": round(low_b * 0.978, 2),
                "grids": 20,
                "qty_sol": 0.3,
                "trailing_locked": False,
                "opened_at": "Manuale",
                "history": [],
            }
        except ValueError:
            pass
    return {"active": False, "history": []}


def save_bot_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        print(f"Avviso: impossibile salvare {STATE_FILE}: {exc}")


def execute_pionex_action(action, price, invest_usdt):
    if not AUTO_TRADE_ENABLED:
        return True, "Simulazione Paper Trading (AUTO_TRADE_ENABLED=false)"

    if PIONEX_WEBHOOK_URL:
        try:
            side = "buy" if action == "OPEN" else "close"
            payload = {
                "token": PIONEX_SIGNAL_TOKEN,
                "time": datetime.now(timezone.utc).isoformat(),
                "symbol": "SOL_USDT",
                "action": side,
                "price": str(round(price, 2)),
                "contracts": str(round(invest_usdt / price, 3)),
            }
            resp = requests.post(PIONEX_WEBHOOK_URL, json=payload, timeout=15)
            if resp.status_code == 200:
                return True, f"Segnale Webhook ({side.upper()}) eseguito!"
            return (
                False,
                f"Errore Webhook (HTTP {resp.status_code}): {resp.text}",
            )
        except Exception as exc:
            return False, f"Eccezione Webhook: {exc}"

    if PIONEX_API_KEY and PIONEX_API_SECRET:
        try:
            ts = str(int(time.time() * 1000))
            path = "/api/v1/trade/order"
            side = "BUY" if action == "OPEN" else "SELL"
            body_dict = {"symbol": "SOL_USDT", "side": side, "type": "MARKET"}
            if side == "BUY":
                body_dict["amount"] = str(round(invest_usdt, 2))
            else:
                state = load_bot_state()
                qty = state.get("qty_sol", round(invest_usdt / price, 3))
                body_dict["size"] = str(round(qty * 0.998, 3))

            body_str = json.dumps(body_dict, separators=(",", ":"))
            msg = f"POST{path}?timestamp={ts}{body_str}"
            signature = hmac.new(
                PIONEX_API_SECRET.encode("utf-8"),
                msg.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "PIONEX-KEY": PIONEX_API_KEY,
                "PIONEX-SIGNATURE": signature,
                "Content-Type": "application/json",
            }
            url = f"https://api.pionex.com{path}?timestamp={ts}"
            resp = requests.post(
                url, data=body_str, headers=headers, timeout=15
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("result"):
                return True, f"Ordine API Spot ({side}) eseguito su Pionex!"
            return False, f"Errore API Pionex: {data}"
        except Exception as exc:
            return False, f"Eccezione API Pionex: {exc}"

    return False, "AUTO_TRADE_ENABLED=true ma nessuna chiave/webhook impostato."


def manage_autonomous_trading(analysis, final_score):
    state = load_bot_state()
    history = state.get("history", [])
    price = analysis["price"]
    ai_prob = analysis["ai_prob"]
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m %H:%M")

    if state.get("active"):
        low_b = float(state["lower"])
        up_b = float(state["upper"])
        stop_b = float(state["stop"])
        entry_p = float(state["entry_price"])
        highest_p = max(float(state.get("highest_price", entry_p)), price)
        state["highest_price"] = round(highest_p, 2)

        pnl_pct = (price / entry_p - 1) * 100
        trailing_note = ""

        if pnl_pct >= 2.5 and stop_b < entry_p:
            stop_b = round(entry_p * 1.003, 2)
            state["stop"] = stop_b
            state["trailing_locked"] = True
            trailing_note = (
                f"\n🔒 Break-Even attivato! Stop Loss alzato a {stop_b:.2f}$"
            )
        elif pnl_pct >= 4.5 and stop_b < highest_p * 0.975:
            stop_b = round(highest_p * 0.975, 2)
            state["stop"] = stop_b
            trailing_note = (
                f"\n🔒 Trailing Stop aggiornato a {stop_b:.2f}$"
            )

        close_reason = None
        if price <= stop_b:
            close_reason = f"🛑 STOP LOSS ({price:.2f}$ <= {stop_b:.2f}$)"
        elif price >= up_b * 1.008:
            close_reason = f"🎯 TAKE PROFIT ({price:.2f}$ > {up_b:.2f}$)"
        elif final_score <= 2 and ai_prob < 0.44:
            close_reason = f"🚨 EMERGENZA AI/MACRO (Voto {final_score}/10)"

        if close_reason:
            _, msg = execute_pionex_action(
                "CLOSE", price, analysis["invest_usdt"]
            )
            closed_trade = {
                "opened_at": state.get("opened_at", "N/D"),
                "closed_at": now_str,
                "entry_price": round(entry_p, 2),
                "exit_price": round(price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usdt": round(analysis["invest_usdt"] * (pnl_pct / 100), 2),
                "reason": close_reason,
            }
            history.insert(0, closed_trade)
            new_state = {
                "active": False,
                "last_closed_at": now_str,
                "last_close_reason": close_reason,
                "last_pnl_pct": round(pnl_pct, 2),
                "history": history[:30],
            }
            save_bot_state(new_state)
            return (
                f"🔴 CHIUSURA AUTOMATICA ESEGUITA!\n"
                f"• Motivo: {close_reason}\n"
                f"• Ingresso: {entry_p:.2f}$ -> Uscita: {price:.2f}$ (PnL Spot: {pnl_pct:+.2f}%)\n"
                f"• Stato Pionex: {msg}"
            )

        save_bot_state(state)
        lock_icon = "🔒" if state.get("trailing_locked") else "🛡️"
        return (
            f"✅ BOT AUTONOMO ATTIVO (Dal: {state.get('opened_at', 'N/D')})\n"
            f"• Entrata: {entry_p:.2f}$ | Attuale: {price:.2f}$ (PnL Spot: {pnl_pct:+.2f}%)\n"
            f"• Range: {low_b:.2f}$ — {up_b:.2f}$ | {lock_icon} Stop: {stop_b:.2f}${trailing_note}"
        )

    if final_score >= 7 and ai_prob >= 0.52:
        _, msg = execute_pionex_action("OPEN", price, analysis["invest_usdt"])
        new_state = {
            "active": True,
            "entry_price": round(price, 2),
            "highest_price": round(price, 2),
            "lower": round(analysis["lower"], 2),
            "upper": round(analysis["upper"], 2),
            "stop": round(analysis["stop"], 2),
            "grids": analysis["grids"],
            "qty_sol": round(analysis["invest_usdt"] / price, 3),
            "trailing_locked": False,
            "opened_at": now_str,
            "history": history,
        }
        save_bot_state(new_state)
        return (
            f"🚀 APERTURA AUTOMATICA ESEGUITA! (Voto {final_score}/10 | AI {ai_prob*100:.0f}%)\n"
            f"• Prezzo d'ingresso: {price:.2f}$\n"
            f"• Range impostato: {analysis['lower']:.2f}$ — {analysis['upper']:.2f}$ ({analysis['grids']} griglie)\n"
            f"• Stop Loss iniziale: {analysis['stop']:.2f}$\n"
            f"• Stato Pionex: {msg}"
        )

    save_bot_state(state)
    last_info = ""
    if state.get("last_close_reason"):
        last_info = f"\n• Ultima chiusura ({state.get('last_closed_at')}): {state.get('last_pnl_pct', 0):+.2f}%"
    return (
        f"⏸️ BOT IN ATTESA DI SEGNALE (Stato: Spento — 100% USDT)\n"
        f"• Requisito Apertura: Voto >= 7/10 e AI >= 52% (Attuale: {final_score}/10 | AI {ai_prob*100:.0f}%){last_info}"
    )


# ---------------------------------------------------------------------
# 8. GENERATORE WEBAPP DASHBOARD (index.html per GitHub Pages)
# ---------------------------------------------------------------------


def generate_webapp_dashboard(analysis, news, final_score):
    state = load_bot_state()
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
    is_active = state.get("active", False)
    mode_badge = (
        "🟢 LIVE TRADING PIONEX"
        if AUTO_TRADE_ENABLED
        else "🧪 PAPER TRADING (SIMULAZIONE)"
    )

    price = analysis["price"]
    if is_active:
        entry_p = float(state["entry_price"])
        low_b = float(state["lower"])
        up_b = float(state["upper"])
        stop_b = float(state["stop"])
        pnl_pct = (price / entry_p - 1) * 100
        pnl_usdt = analysis["invest_usdt"] * (pnl_pct / 100)
        pos_pct = max(0, min(100, (price - low_b) / (up_b - low_b) * 100))
        status_html = f"""
        <div class="bg-emerald-950/60 border border-emerald-500/40 rounded-2xl p-6">
            <div class="flex justify-between items-center mb-4">
                <span class="px-3 py-1 bg-emerald-500/20 text-emerald-300 rounded-full text-sm font-bold">🟢 POSIZIONE ATTIVA (Dal {state.get('opened_at')})</span>
                <span class="text-2xl font-extrabold {'text-emerald-400' if pnl_pct>=0 else 'text-rose-400'}">{pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)</span>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm mb-4">
                <div class="bg-slate-900/80 p-3 rounded-xl"><div class="text-slate-400">Prezzo Entrata</div><div class="text-lg font-bold text-white">{entry_p:.2f}$</div></div>
                <div class="bg-slate-900/80 p-3 rounded-xl"><div class="text-slate-400">Prezzo Attuale</div><div class="text-lg font-bold text-sky-400">{price:.2f}$</div></div>
                <div class="bg-slate-900/80 p-3 rounded-xl"><div class="text-slate-400">Range Griglia</div><div class="text-lg font-bold text-white">{low_b:.2f}$ - {up_b:.2f}$</div></div>
                <div class="bg-slate-900/80 p-3 rounded-xl"><div class="text-slate-400">Stop Loss Dinamico</div><div class="text-lg font-bold text-amber-400">{stop_b:.2f}$</div></div>
            </div>
            <div class="text-xs text-slate-300 mb-1 flex justify-between"><span>Lower ({low_b:.2f}$)</span><span>Posizione nel Range: {pos_pct:.0f}%</span><span>Upper ({up_b:.2f}$)</span></div>
            <div class="w-full bg-slate-800 h-3 rounded-full overflow-hidden"><div class="bg-gradient-to-r from-emerald-500 to-sky-400 h-3" style="width: {pos_pct:.0f}%"></div></div>
        </div>
        """
    else:
        status_html = f"""
        <div class="bg-slate-900 border border-slate-700 rounded-2xl p-6">
            <div class="flex justify-between items-center mb-2">
                <span class="px-3 py-1 bg-amber-500/20 text-amber-300 rounded-full text-sm font-bold">⏸️ IN ATTESA DI SEGNALE (100% USDT)</span>
                <span class="text-sm text-slate-400">Target Apertura: Voto &ge; 7/10 &amp; AI &ge; 52%</span>
            </div>
            <p class="text-slate-300 text-sm mt-2">Il bot sta monitorando il mercato ogni 30 minuti. Aprirà automaticamente la griglia (Range ipotetico: <b>{analysis['lower']:.2f}$ - {analysis['upper']:.2f}$</b> | Stop: <b>{analysis['stop']:.2f}$</b>) non appena i filtri Quant e la Rete Neurale daranno il via libera.</p>
        </div>
        """

    history = state.get("history", [])
    total_pnl_pct = sum(float(h.get("pnl_pct", 0)) for h in history)
    history_rows = ""
    if history:
        for h in history:
            p_pct = float(h.get("pnl_pct", 0))
            col = "text-emerald-400" if p_pct >= 0 else "text-rose-400"
            history_rows += f"""
            <tr class="border-b border-slate-800 text-sm">
                <td class="py-3 px-2">{h.get('opened_at')} &rarr; {h.get('closed_at')}</td>
                <td class="py-3 px-2">{h.get('entry_price')}$ &rarr; {h.get('exit_price')}$</td>
                <td class="py-3 px-2 font-bold {col}">{p_pct:+.2f}% ({h.get('pnl_usdt', 0):+.2f}$)</td>
                <td class="py-3 px-2 text-slate-300">{h.get('reason')}</td>
            </tr>
            """
    else:
        history_rows = '<tr><td colspan="4" class="py-4 text-center text-slate-500">Nessun trade chiuso finora nello storico.</td></tr>'

    pillars_html = "".join(
        f'<li class="p-3 bg-slate-900/90 border border-slate-800 rounded-xl text-sm">{r}</li>'
        for r in analysis["reasons"]
    )
    news_html = "".join(
        f'<li class="text-sm text-slate-300 py-1">{n}</li>'
        for n in news["evaluated_lines"]
    )

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="300">
    <title>Neural Quant Grid Bot — SOL/USDT</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-5xl mx-auto space-y-6">
        <div class="flex flex-col md:flex-row justify-between items-start md:items-center bg-slate-900 border border-slate-800 p-5 rounded-2xl gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight text-white">🧠 Neural Quant Grid Bot <span class="text-sky-400">SOL/USDT</span></h1>
                <p class="text-xs text-slate-400 mt-1">Ultimo aggiornamento: {now_str} ({LOCAL_TZ_NAME}) • Auto-refresh 5m</p>
            </div>
            <span class="px-4 py-1.5 rounded-full text-xs font-bold bg-slate-800 border border-slate-700 text-sky-300">{mode_badge}</span>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Prezzo SOL / VWAP 24h</div>
                <div class="text-2xl font-extrabold text-white mt-1">{price:.2f}$</div>
                <div class="text-xs text-slate-400 mt-1">VWAP: {analysis['vwap_24h']:.2f}$ ({analysis['chg24']:+.1f}% 24h)</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Punteggio Finale</div>
                <div class="text-2xl font-extrabold {'text-emerald-400' if final_score>=7 else ('text-amber-400' if final_score>=5 else 'text-rose-400')} mt-1">{final_score}/10</div>
                <div class="text-xs text-slate-400 mt-1">Quant Score: {analysis['pts_100']}/100</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Rete Neurale + Lorentz AI</div>
                <div class="text-2xl font-extrabold text-sky-400 mt-1">{analysis['ai_prob']*100:.0f}%</div>
                <div class="text-xs text-slate-400 mt-1">MLP: {analysis['mlp_prob']*100:.0f}% | Lor: {analysis['lor_prob']*100:.0f}%</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Esponente Frattale Hurst</div>
                <div class="text-2xl font-extrabold text-purple-400 mt-1">H = {analysis['hurst']:.2f}</div>
                <div class="text-xs text-slate-400 mt-1">{'Mean-Reverting (Ideale)' if analysis['hurst']<0.5 else 'Trending'}</div>
            </div>
        </div>

        {status_html}

        <div class="grid md:grid-cols-2 gap-6">
            <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl">
                <h2 class="text-lg font-bold mb-3 text-white">🧬 I 9 Pilastri Quantitativi &amp; Neurali</h2>
                <ul class="space-y-2">{pillars_html}</ul>
            </div>
            <div class="space-y-6">
                <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl">
                    <h2 class="text-lg font-bold mb-3 text-white">📰 Calendario Macro &amp; News</h2>
                    <ul class="space-y-1 mb-3">{news_html}</ul>
                    <div class="p-3 bg-slate-950 rounded-xl text-xs text-sky-300 border border-slate-800">{news['explanation']}</div>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-5 rounded-2xl">
                    <div class="flex justify-between items-center mb-3">
                        <h2 class="text-lg font-bold text-white">📜 Storico Operazioni Chiuse</h2>
                        <span class="text-sm font-bold {'text-emerald-400' if total_pnl_pct>=0 else 'text-rose-400'}">PnL Storico: {total_pnl_pct:+.2f}%</span>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left">
                            <thead><tr class="border-b border-slate-700 text-xs text-slate-400"><th class="pb-2">Data</th><th class="pb-2">Prezzo</th><th class="pb-2">PnL</th><th class="pb-2">Motivo</th></tr></thead>
                            <tbody>{history_rows}</tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>"""
    try:
        with open(WEBAPP_FILE, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print(f"Avviso: impossibile generare {WEBAPP_FILE}: {exc}")


# ---------------------------------------------------------------------
# 9. COSTRUZIONE REPORT TELEGRAM E AVVIO
# ---------------------------------------------------------------------


def make_progress_bar(score):
    fill = "🟩" if score >= 7 else ("🟨" if score >= 5 else "🟥")
    return fill * score + "⬜" * (10 - score)


def build_report(analysis, news):
    tech_score = analysis["score"]
    penalty = news["penalty"]
    news_cap = news["max_score_cap"]

    final_score = max(1, min(10, min(tech_score - penalty, news_cap)))
    action_info = SCORE_ACTIONS[final_score]
    bar = make_progress_bar(final_score)
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m %H:%M")

    auto_status_block = manage_autonomous_trading(analysis, final_score)
    generate_webapp_dashboard(analysis, news, final_score)

    lines = [
        f"🧠 NEURAL QUANT GRID BOT SOL/USDT — {now_str}",
        f"💰 SOL: {analysis['price']:.2f}$ | VWAP 24h: {analysis['vwap_24h']:.2f}$ (4h: {analysis['chg4h']:+.1f}% | 24h: {analysis['chg24']:+.1f}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 PUNTEGGIO FINALE: {final_score}/10 (Quant+AI: {analysis['pts_100']}/100)",
        f"{bar}",
        "",
        f"{action_info['title']}",
        f"💡 {action_info['desc']}",
        "━━━━━━━━━━━━━━━━━━━━",
        "🤖 GESTIONE OPERATIVA AUTONOMA:",
        auto_status_block,
        "━━━━━━━━━━━━━━━━━━━━",
        "🛡️ CHECK MULTI-ASSET & SALVA-CAPITALE:",
        f"• {analysis['btc_info']['status']}",
        f"• Macro 4H: {'🟢 Sopra' if analysis['price'] >= analysis['ema200_4h'] else '🔴 Sotto'} EMA200 ({analysis['ema200_4h']:.1f}$) | RSI 4H: {analysis['rsi_4h']:.0f}",
        f"• Wall Street: {news['ws_status']}",
    ]

    if analysis["risk_vetoes"]:
        for veto in analysis["risk_vetoes"]:
            lines.append(f"• {veto}")
    else:
        lines.append("• ✅ Nessun Veto di sicurezza attivo.")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if final_score >= 7:
        lines.append("🛠️ PARAMETRI OTTIMALI CALCOLATI:")
    else:
        lines.append("🛠️ PARAMETRI IPOTETICI (ATTENDERE SEGNALE):")

    lines.extend(
        [
            f"• Range: {analysis['lower']:.2f}$ — {analysis['upper']:.2f}$ (Pos: {analysis['pos']:.0f}%)",
            f"• Griglie: {analysis['grids']} (Lordo: {analysis['step_pct']:.2f}% | Netto: +{analysis['net_step_pct']:.2f}%)",
            f"• Stop Loss Dinamico: {analysis['stop']:.2f}$",
            f"• Budget: ~{analysis['invest_usdt']:.0f} USDT",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🧬 I 9 PILASTRI QUANT & NEURALI ({analysis['pts_100']}/100 pt | Cap: {analysis['tech_cap']}/10):",
        ]
    )
    lines.extend(analysis["reasons"])

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            f"📰 CALENDARIO MACRO ({', '.join(sorted(NEWS_COUNTRIES)).upper()}):",
        ]
    )
    lines.extend(news["evaluated_lines"])
    lines.extend(["", f"⚖️ Impatto News: {news['explanation']}"])

    return "\n".join(lines)


def main():
    try:
        analysis = analyze()
    except Exception as exc:
        print(f"Errore analisi ({type(exc).__name__}): {exc}")
        sys.exit(1)

    news = get_news_context()
    report = build_report(analysis, news)

    print(report)

    if not TG_TOKEN or not TG_ID:
        print(
            "\nTelegram non configurato: imposta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        )
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_ID, "text": report},
            timeout=15,
        )
        print(f"\nTelegram HTTP {response.status_code}")
        if response.status_code != 200:
            print(f"Invio Telegram non riuscito: {response.text}")
            sys.exit(1)
    except requests.RequestException as exc:
        print(f"Errore Telegram ({type(exc).__name__}).")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

#### PASSO 2: Aggiorna l'ultimo blocco nel file `.yml`
Nel tuo file dentro **`.github/workflows/`**, assicurati che l'ultimo step in fondo salvi sia `bot_state.json` sia **`index.html`**, così:

```yaml
      - name: Salva Memoria e WebApp Dashboard
        run: |
          git config --global user.name "NeuralGridBot"
          git config --global user.email "bot@users.noreply.github.com"
          git add bot_state.json index.html || true
          git diff --staged --quiet || (git commit -m "Aggiornamento Dashboard e Stato Bot [skip ci]" && git pull --rebase && git push)
```

---

#### PASSO 3: Accendi la WebApp su GitHub Pages (3 clic!)
1. Fai partire una volta il bot da **Actions -> Run workflow** (così crea il file `index.html`).
2. Vai in alto su **Settings** (Impostazioni del tuo repository su GitHub).
3. Nel menu a sinistra, clicca su **Pages**.
4. Sotto la voce **Branch**, cambia da `None` a **`main`** (o `master`) e clicca su **Save**.

🎉 **Fatto!** Dopo 1 minuto, in alto nella stessa pagina **Pages** ti apparirà il tuo link personale (sarà del tipo `https://tuonome.github.io/pionex/`). Aprilo dal telefono o dal PC e vedrai la tua **WebApp Dashboard in tempo reale**!
