from datetime import datetime, timezone
import math
import os
import re
import sys
from zoneinfo import ZoneInfo
import requests


# ---------------------------------------------------------------------
# 1. CONFIGURAZIONE E VARIABILI D'AMBIENTE (CALIBRAZIONE QUANT)
# ---------------------------------------------------------------------

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

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

# Parametri News bilanciati
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

INVEST_EUR = 50
MIN_ORDER_USDT = 5.0
TARGET_STEP_PCT = 0.32
PIONEX_ROUNDTRIP_FEE_PCT = 0.10
MIN_NET_STEP_PCT = 0.15


# ---------------------------------------------------------------------
# 2. SCALA DECISIONALE DA 1 A 10 (SOGLIA APERTURA A 7/10)
# ---------------------------------------------------------------------

SCORE_ACTIONS = {
    1: {
        "title": "⛔ 1/10 — BLOCCO TOTALE (EMERGENZA)",
        "desc": "Crollo violento in corso o news critica (FOMC/CPI) imminente. Resta 100% liquido in USDT.",
    },
    2: {
        "title": "🔴 2/10 — NON APRIRE (PERICOLO ELEVATO)",
        "desc": "Veto di sicurezza attivo: dump di BTC/SOL, forti vendite sull'orderbook o news appena uscita.",
    },
    3: {
        "title": "🔴 3/10 — SCONSIGLIATO (STRUTTURA RIBASSISTA)",
        "desc": "Trend 4H/1H negativo e indicatori di momentum girati al ribasso. Rischio Stop Loss alto.",
    },
    4: {
        "title": "🟠 4/10 — ATTENDI (MERCATO SFAVOREVOLE)",
        "desc": "Pressione in vendita superiore agli acquisti o compressione pericolosa. Non anticipare.",
    },
    5: {
        "title": "🟡 5/10 — NEUTRALE (MEGLIO ASPETTARE)",
        "desc": "Dati contrastanti o prezzo sopra il VWAP/parte alta del canale. Rapporto rischio/rendimento mediocre.",
    },
    6: {
        "title": "🟡 6/10 — QUASI PRONTO (OSSERVA)",
        "desc": "Struttura discreta ma mancano conferme sul 15m o sull'orderbook. Attendi un piccolo sconto.",
    },
    7: {
        "title": "🟢 7/10 — APRI IL BOT (BUON SETUP)",
        "desc": "Confluenza positiva su più indicatori e filtri di rischio verdi. Puoi avviare la griglia.",
    },
    8: {
        "title": "🟢 8/10 — APRI IL BOT (OTTIMO MOMENTO)",
        "desc": "Regime laterale sano, orderbook a supporto, prezzo sotto/vicino al VWAP e buon margine netto.",
    },
    9: {
        "title": "🔥 9/10 — APRI SENZA ESITARE (SETUP ECCELLENTE)",
        "desc": "Allineamento multi-timeframe, derivati sani e rimbalzo confermato nella zona bassa del canale.",
    },
    10: {
        "title": "🚀 10/10 — SETUP ISTITUZIONALE PERFETTO",
        "desc": "Massima confluenza su tutti gli 8 pilastri quantitativi. Condizioni ideali per il Grid Bot.",
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

        # Pesa maggiormente i livelli vicini al prezzo attuale
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
                # Funding rate orario/relativo convertito in % per 8h
                raw_fr = float(sol_perp.get("fundingRate", 0.0))
                mark_p = float(sol_perp.get("markPrice", 1.0))
                # Kraken restituisce fundingRate assoluto o relativo; normalizziamo in %
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
# 4. LIBRERIA INDICATORI QUANTITATIVI (PURO PYTHON)
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
    out = [50.0] * n

    for i in range(n, len(gains)):
        if i > n:
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
        return 0.0, 0.0, False
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(fast_s, slow_s)]
    sig_line = ema_series(macd_line, sig)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    rising = hist[-1] > hist[-2]
    return macd_line[-1], hist[-1], rising


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


def variance_ratio_mr(closes, lag=6):
    """Variance Ratio (< 1.0 indica Mean-Reversion ideale per Grid Bot,

    > 1.0 indica Mercato Trending unidirezionale).
    """
    if len(closes) < 72:
        return 1.0
    log_p = [math.log(max(c, 1e-8)) for c in closes[-72:]]
    r1 = [log_p[i] - log_p[i - 1] for i in range(1, len(log_p))]
    rk = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
    if not r1 or not rk:
        return 1.0
    m1 = sum(r1) / len(r1)
    mk = sum(rk) / len(rk)
    var1 = sum((x - m1) ** 2 for x in r1) / len(r1)
    vark = sum((x - mk) ** 2 for x in rk) / len(rk)
    if var1 <= 0:
        return 1.0
    return vark / (lag * var1)


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


def percentile(values, p):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


# ---------------------------------------------------------------------
# 5. MOTORE QUANTITATIVO A 8 PILASTRI (100 PUNTI -> SCALA 1-10)
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

        rel_24h = sol_chg24h - chg24h  # Forza relativa SOL rispetto a BTC
        is_dumping = chg4h <= -2.8 or chg24h <= -6.0
        strong_bear = price < ema200 and mdi > pdi and adx > 28

        if is_dumping or strong_bear:
            return {
                "safe": False,
                "cap": 2 if chg4h <= -3.5 else 3,
                "chg4h": chg4h,
                "chg24h": chg24h,
                "rel_24h": rel_24h,
                "status": f"🔴 BTC Ostile ({price:.0f}$, 4h: {chg4h:+.1f}%, ADX {adx:.0f}) | Alpha SOL 24h: {rel_24h:+.1f}%",
            }
        return {
            "safe": True,
            "cap": 10,
            "chg4h": chg4h,
            "chg24h": chg24h,
            "rel_24h": rel_24h,
            "status": f"🟢 BTC Stabile ({price:.0f}$, 4h: {chg4h:+.1f}%) | Alpha SOL 24h: {rel_24h:+.1f}%",
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
    # 1. Candele Multi-Timeframe (1H, 4H, 15m)
    c1h = get_candles(pair="SOLUSD", interval=60)
    c4h = get_candles(pair="SOLUSD", interval=240)
    c15m = get_candles(pair="SOLUSD", interval=15)

    # 2. Orderbook, Derivati e Sentiment
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

    # Indicatori 1H
    ema20_1h = ema(close[-250:], 20)
    ema50_1h = ema(close[-250:], 50)
    ema200_1h = ema(close[-350:], 200)
    rsi_1h = rsi(close[-250:], 14)
    srsi_1h = stoch_rsi(close[-250:], 14, 14)
    _, macd_hist, macd_rising = macd_metrics(close[-250:])
    _, bb_mid, _, bb_pct_b, bb_width = bollinger_bands(close[-250:], 20, 2.0)
    atr, adx, pdi, mdi, atr_series = atr_adx_di(
        high[-250:], low[-250:], close[-250:]
    )
    vr_ratio = variance_ratio_mr(close[-120:], lag=6)

    # VWAP 24h istituzionale
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

    # Indicatori 4H (Macro-Struttura)
    close_4h = c4h["close"]
    ema50_4h = ema(close_4h[-200:], 50)
    ema200_4h = ema(close_4h[-250:], 200)
    rsi_4h = rsi(close_4h[-150:], 14)

    # Indicatori 15m (Micro-Timing)
    close_15m = c15m["close"]
    rsi_15m = rsi(close_15m[-100:], 14)
    srsi_15m = stoch_rsi(close_15m[-100:], 14, 14)
    micro_bounce = close_15m[-1] >= c15m["open"][-1] or close_15m[-1] > ema(
        close_15m[-40:], 9
    )

    # Variazioni e Volatilità
    chg4h = (price / close[-5] - 1) * 100
    chg24 = (price / close[-25] - 1) * 100
    avg_atr_7d = (
        sum(atr_series[-168:]) / len(atr_series[-168:]) if atr_series else atr
    )
    atr_ratio = atr / avg_atr_7d if avg_atr_7d > 0 else 1.0

    # Keltner Squeeze Check (BB dentro Keltner Channels)
    kc_width = (atr * 3.0) / bb_mid * 100 if bb_mid > 0 else 0.0
    is_keltner_squeeze = bb_width < kc_width and atr_ratio < 0.72

    # Volumi di vendita anomali
    avg_vol_7d = sum(volume[-168:]) / 168 if sum(volume[-168:]) > 0 else 1.0
    red_vols_3h = [volume[-i] for i in range(1, 4) if close[-i] < opens[-i]]
    max_red_vol_ratio = (max(red_vols_3h) / avg_vol_7d) if red_vols_3h else 0.0

    # Calcolo Range Ottimale Pionex
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

    # -----------------------------------------------------------------
    # CALCOLO PUNTEGGIO SU 100 PUNTI (8 PILASTRI QUANTITATIVI)
    # -----------------------------------------------------------------
    pts = 0
    reasons = []
    risk_vetoes = []
    tech_cap = 10

    if not btc_info["safe"]:
        tech_cap = min(tech_cap, btc_info["cap"])
        risk_vetoes.append(btc_info["status"])

    # 1. MULTI-TIMEFRAME TREND (4H + 1H) — Max 15 pt
    bear_1h = price < ema200_1h and mdi > pdi
    bear_4h = price < ema200_4h and ema50_4h < ema200_4h
    if bear_1h and bear_4h and (adx > 25 or chg24 < -5.0):
        p1 = 0
        tech_cap = min(tech_cap, 3)
        risk_vetoes.append(
            "🚨 Veto MTF: Trend ribassista allineato su 4H e 1H."
        )
    elif price >= ema200_1h and price >= ema50_4h and adx < 24:
        p1 = 15
    elif price >= ema200_1h or (not bear_1h and adx < 26):
        p1 = 11
    elif not bear_4h and adx < 30:
        p1 = 7
    else:
        p1 = 3
    pts += p1
    reasons.append(
        f"{'🟢' if p1>=11 else ('🟡' if p1>=7 else '🔴')} 1. Trend MTF (4H/1H): EMA200 1H {ema200_1h:.1f}$ | 4H {ema200_4h:.1f}$ | ADX {adx:.0f} ({p1}/15)"
    )

    # 2. REGIME MEAN-REVERSION & SQUEEZE (Variance Ratio + ATR + Keltner) — Max 15 pt
    if is_keltner_squeeze:
        p2 = 3
        tech_cap = min(tech_cap, 4)
        risk_vetoes.append(
            "⚠️ Keltner Squeeze attivo: volatilità compressa, rischio breakout."
        )
    elif vr_ratio <= 0.95 and 0.72 <= atr_ratio <= 1.35:
        p2 = 15
    elif vr_ratio <= 1.12 and 0.65 <= atr_ratio <= 1.55:
        p2 = 11
    elif 0.60 <= atr_ratio <= 1.75:
        p2 = 7
    else:
        p2 = 2
    pts += p2
    reasons.append(
        f"{'🟢' if p2>=11 else ('🟡' if p2>=7 else '🔴')} 2. Regime & Volatilità: VR {vr_ratio:.2f} ({'Mean-Rev' if vr_ratio<1 else 'Trend'}) | ATR {atr_ratio:.2f}x ({p2}/15)"
    )

    # 3. POSIZIONE NEL CANALE & VWAP 24H — Max 15 pt
    vwap_diff_pct = (price / vwap_24h - 1) * 100
    if position_in_range > 72:
        p3 = 2
        tech_cap = min(tech_cap, 5)
        risk_vetoes.append(
            f"⚠️ Prezzo troppo alto nel canale ({position_in_range:.0f}% > 72%)."
        )
    elif (
        20 <= position_in_range <= 52
        and price <= vwap_24h * 1.006
        and 0.15 <= bb_pct_b <= 0.60
    ):
        p3 = 15
    elif 18 <= position_in_range <= 62 and vwap_diff_pct <= 1.2:
        p3 = 11
    elif 12 <= position_in_range <= 72:
        p3 = 6
    else:
        p3 = 2
    pts += p3
    reasons.append(
        f"{'🟢' if p3>=11 else ('🟡' if p3>=6 else '🔴')} 3. Valore & VWAP: Pos {position_in_range:.0f}% | vs VWAP {vwap_diff_pct:+.1f}% | %B {bb_pct_b:.2f} ({p3}/15)"
    )

    # 4. MOMENTUM & MICRO-TIMING (MACD + RSI 1H/15m + StochRSI) — Max 15 pt
    if rsi_1h < 28 or (not macd_rising and macd_hist < 0 and chg4h < -2.5):
        p4 = 2  # Coltello che cade
    elif (
        macd_rising
        and 36 <= rsi_1h <= 58
        and 30 <= rsi_15m <= 62
        and micro_bounce
    ):
        p4 = 15
    elif (macd_rising or micro_bounce) and 33 <= rsi_1h <= 64:
        p4 = 11
    elif 30 <= rsi_1h <= 68:
        p4 = 7
    else:
        p4 = 3
    pts += p4
    reasons.append(
        f"{'🟢' if p4>=11 else ('🟡' if p4>=7 else '🔴')} 4. Momentum (1H/15m): MACD {'📈' if macd_rising else '📉'} | RSI 1h:{rsi_1h:.0f} 15m:{rsi_15m:.0f} | Stoch:{srsi_15m:.0f} ({p4}/15)"
    )

    # 5. ORDERBOOK DEPTH & VOLUMI SMART MONEY — Max 10 pt
    imb = ob["imbalance"]
    if max_red_vol_ratio >= 2.3 or chg4h <= -3.8:
        p5 = 0
        tech_cap = min(tech_cap, 2)
        risk_vetoes.append(
            f"🚨 Veto Volumi: Picco vendite {max_red_vol_ratio:.1f}x media (4h: {chg4h:+.1f}%)."
        )
    elif imb >= 1.12 and max_red_vol_ratio < 1.3:
        p5 = 10
    elif imb >= 0.90 and max_red_vol_ratio < 1.6:
        p5 = 7
    elif imb >= 0.75:
        p5 = 4
    else:
        p5 = 2
    pts += p5
    reasons.append(
        f"{'🟢' if p5>=7 else ('🟡' if p5>=4 else '🔴')} 5. Orderbook & Volumi: Bid/Ask {imb:.2f}x | Vol Rosso Max {max_red_vol_ratio:.1f}x ({p5}/10)"
    )

    # 6. CORRELAZIONE BTC & FORZA RELATIVA (ALPHA) — Max 10 pt
    rel = btc_info["rel_24h"]
    if not btc_info["safe"]:
        p6 = 0
    elif rel >= 0.5 and btc_info["chg4h"] >= -0.8:
        p6 = 10
    elif rel >= -1.5 and btc_info["chg4h"] >= -1.5:
        p6 = 7
    elif rel >= -3.0:
        p6 = 4
    else:
        p6 = 1
    pts += p6
    reasons.append(
        f"{'🟢' if p6>=7 else ('🟡' if p6>=4 else '🔴')} 6. Forza Relativa BTC: Alpha SOL {rel:+.1f}% | BTC 4h {btc_info['chg4h']:+.1f}% ({p6}/10)"
    )

    # 7. DERIVATI (FUNDING RATE) & SENTIMENT (FEAR & GREED) — Max 10 pt
    fr = deriv["funding_pct"]
    fg_val = fng["value"]
    # Funding troppo alto (>0.04%) + Greed estremo (>78) = Rischio Long Squeeze
    if fr > 0.05 or fg_val > 82:
        p7 = 3
    elif -0.02 <= fr <= 0.025 and 25 <= fg_val <= 72:
        p7 = 10
    elif -0.04 <= fr <= 0.04 and 18 <= fg_val <= 78:
        p7 = 7
    else:
        p7 = 5
    pts += p7
    fr_str = f"{fr:+.4f}%" if deriv["ok"] else "N/D"
    reasons.append(
        f"{'🟢' if p7>=7 else '🟡'} 7. Derivati & Sentiment: Funding {fr_str} | F&G {fg_val} ({fng['label']}) ({p7}/10)"
    )

    # 8. ECONOMIA DELLA GRIGLIA (MARGINE NETTO & ROTAZIONE) — Max 10 pt
    if (
        net_step_pct >= MIN_NET_STEP_PCT
        and 0.38 <= rotation_ratio <= 0.95
        and 3.0 <= avg_daily <= 9.0
    ):
        p8 = 10
    elif net_step_pct >= 0.12 and rotation_ratio >= 0.28:
        p8 = 7
    elif net_step_pct >= 0.09:
        p8 = 4
    else:
        p8 = 1
    pts += p8
    reasons.append(
        f"{'🟢' if p8>=7 else ('🟡' if p8>=4 else '🔴')} 8. Efficienza Griglia: Netto +{net_step_pct:.2f}% | Rotazione {rotation_ratio:.2f}x/gg ({p8}/10)"
    )

    raw_tech_score = max(1, min(10, int(round(pts / 10.0))))
    capped_tech_score = min(raw_tech_score, tech_cap)

    return {
        "price": price,
        "vwap_24h": vwap_24h,
        "ema200_1h": ema200_1h,
        "ema200_4h": ema200_4h,
        "rsi_4h": rsi_4h,
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
        "ob": ob,
        "deriv": deriv,
        "fng": fng,
        "invest_usdt": invest_usdt,
        "candle_times": c1h["time"],
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
    in_window = 560 <= minutes_of_day <= 605  # 09:20 - 10:05 NY
    return in_window, ny_time.strftime("%H:%M")


def fetch_macro_events(now):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; QuantGridBot/4.0)",
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
# 7. COSTRUZIONE REPORT E INVIO TELEGRAM
# ---------------------------------------------------------------------


def make_progress_bar(score):
    fill = "🟩" if score >= 7 else ("🟨" if score >= 5 else "🟥")
    return fill * score + "⬜" * (10 - score)


def check_active_bot(price):
    if not (ACTIVE_LOWER and ACTIVE_UPPER):
        return None
    try:
        low_b, up_b = float(ACTIVE_LOWER), float(ACTIVE_UPPER)
        if low_b >= up_b:
            return None
        if price < low_b or price > up_b:
            return f"🚨 BOT ATTIVO ({low_b:.2f}$ - {up_b:.2f}$) FUORI RANGE!"
        edge_pct = min(price - low_b, up_b - price) / price * 100
        if edge_pct < 1.5:
            return f"⚠️ BOT ATTIVO ({low_b:.2f}$ - {up_b:.2f}$) vicino al bordo ({edge_pct:.1f}%)."
        return f"✅ BOT GIÀ ATTIVO NEL RANGE ({low_b:.2f}$ - {up_b:.2f}$)."
    except ValueError:
        return None


def build_report(analysis, news):
    tech_score = analysis["score"]
    penalty = news["penalty"]
    news_cap = news["max_score_cap"]

    final_score = max(1, min(10, min(tech_score - penalty, news_cap)))
    action_info = SCORE_ACTIONS[final_score]
    bar = make_progress_bar(final_score)
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m %H:%M")

    lines = [
        f"🤖 QUANT GRID ADVISOR SOL/USDT — {now_str}",
        f"💰 SOL: {analysis['price']:.2f}$ | VWAP 24h: {analysis['vwap_24h']:.2f}$ (4h: {analysis['chg4h']:+.1f}% | 24h: {analysis['chg24']:+.1f}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 PUNTEGGIO FINALE: {final_score}/10 (Quant Score: {analysis['pts_100']}/100)",
        f"{bar}",
        "",
        f"{action_info['title']}",
        f"💡 {action_info['desc']}",
    ]

    active_msg = check_active_bot(analysis["price"])
    if active_msg:
        lines.extend(["", active_msg])

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "🛡️ CHECK MULTI-ASSET & RISCHIO:",
            f"• {analysis['btc_info']['status']}",
            f"• Macro 4H: {'🟢 Sopra' if analysis['price'] >= analysis['ema200_4h'] else '🔴 Sotto'} EMA200 ({analysis['ema200_4h']:.1f}$) | RSI 4H: {analysis['rsi_4h']:.0f}",
            f"• Wall Street: {news['ws_status']}",
        ]
    )

    if analysis["risk_vetoes"]:
        for veto in analysis["risk_vetoes"]:
            lines.append(f"• {veto}")
    else:
        lines.append("• ✅ Nessun Veto di sicurezza attivo.")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if final_score >= 7:
        lines.append("🛠️ PARAMETRI PIONEX CONSIGLIATI:")
    else:
        lines.append("🛠️ PARAMETRI IPOTETICI (NON APRIRE ORA):")

    lines.extend(
        [
            f"• Range: {analysis['lower']:.2f}$ — {analysis['upper']:.2f}$ (Pos: {analysis['pos']:.0f}%)",
            f"• Griglie: {analysis['grids']} (Lordo: {analysis['step_pct']:.2f}% | Netto: +{analysis['net_step_pct']:.2f}%)",
            f"• Stop Loss Salva-Capitale: {analysis['stop']:.2f}$",
            f"• Budget: ~{analysis['invest_usdt']:.0f} USDT",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🧠 GLI 8 PILASTRI QUANTITATIVI ({analysis['pts_100']}/100 pt | Cap: {analysis['tech_cap']}/10):",
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
