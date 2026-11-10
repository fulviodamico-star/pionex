import os
import re
import json
import time
import math
import hmac
import hashlib
import random
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ==============================================================================
# CONFIGURAZIONE AMBIENTE E PARAMETRI FAST-SCALP (3-4 TRADE AL GIORNO)
# ==============================================================================
TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TOKEN")
    or ""
).strip()

TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("CHAT_ID")
    or os.getenv("TELEGRAM_CHAT")
    or ""
).strip()

PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or os.getenv("API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or os.getenv("API_SECRET") or "").strip()
GITHUB_EVENT_NAME = (os.getenv("GITHUB_EVENT_NAME") or "manual").strip()

INVEST_EUR = float(os.getenv("INVEST_EUR", "50.0"))
AUTO_TRADE_ENABLED = os.getenv("AUTO_TRADE_ENABLED", "true").lower() == "true"

# Parametri calibrati per 3-4 operazioni Fast-Scalp al giorno
Entry_Score_Min = 6              # Minimo 6/10 (con raw_100 >= 58)
Entry_Raw_100_Min = 58           # Punteggio reale minimo su 100
MAX_LOSS_PCT_PER_TRADE = 2.2     # Stop-Loss stretto da scalping (max 2.2%)
DAILY_MAX_LOSS_PCT = 6.0         # Circuit Breaker giornaliero (-6.0%)
MAX_TRADES_PER_DAY = 5           # Fino a 5 operazioni al giorno (target medio: 3-4/giorno)
MIN_REWARD_RISK_R = 1.25         # R:R minimo 1.25x per target intraday rapidi
CASH_BUFFER_PCT = 5.0            # Riserva liquidita' 5%
PIONEX_FEE_ROUNDTRIP_PCT = 0.10  # 0.05% buy + 0.05% sell
SCALP_MIN_NET_PROFIT_PCT = 0.65  # Incasso rapido netto >= +0.65% su inversione 15m
TIME_STOP_PROFIT_HOURS = 4.5     # Chiude dopo 4.5h se net_pnl >= +0.35% per liberare capitale
TIME_STOP_STALE_HOURS = 6.0      # Chiude dopo 6.0h se il momentum diventa negativo

STATE_FILE = "bot_state.json"
WEBAPP_FILE = "index.html"
HTTP_TIMEOUT = 12

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PionexAutonomousQuantBot/7.3; +https://pionex.com)"
}

COINS = [
    {"code": "SOL",  "name": "Solana",    "kraken": "SOLUSD",  "ws": "SOL/USD",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT",  "prec": 3},
    {"code": "BTC",  "name": "Bitcoin",   "kraken": "XBTUSD",  "ws": "XBT/USD",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT",  "prec": 5},
    {"code": "ETH",  "name": "Ethereum",  "kraken": "ETHUSD",  "ws": "ETH/USD",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT",  "prec": 4},
    {"code": "XRP",  "name": "Ripple",    "kraken": "XRPUSD",  "ws": "XRP/USD",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT",  "prec": 1},
    {"code": "SUI",  "name": "Sui",       "kraken": "SUIUSD",  "ws": "SUI/USD",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT",  "prec": 2},
    {"code": "DOGE", "name": "Dogecoin",  "kraken": "XDGUSD",  "ws": "XDG/USD",  "futures": "PF_DOGEUSD", "pionex": "DOGE_USDT", "prec": 0},
    {"code": "ADA",  "name": "Cardano",   "kraken": "ADAUSD",  "ws": "ADA/USD",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT",  "prec": 1},
    {"code": "AVAX", "name": "Avalanche", "kraken": "AVAXUSD", "ws": "AVAX/USD", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT", "prec": 2},
    {"code": "LINK", "name": "Chainlink", "kraken": "LINKUSD", "ws": "LINK/USD", "futures": "PF_LINKUSD", "pionex": "LINK_USDT", "prec": 2},
    {"code": "DOT",  "name": "Polkadot",  "kraken": "DOTUSD",  "ws": "DOT/USD",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT",  "prec": 2},
]
COIN_MAP = {c["code"]: c for c in COINS}


# ==============================================================================
# 1. UTILITA' MATEMATICHE E FORMATTAZIONE
# ==============================================================================
def safe_get_json(url, params=None, retries=2):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries:
                time.sleep(0.7 * (attempt + 1))
            else:
                print(f"[WARN] JSON fetch error ({url}): {e}")
    return None


def clamp(val, low, high):
    return max(low, min(high, val))


def sigmoid(x):
    x = clamp(x, -20.0, 20.0)
    return 1.0 / (1.0 + math.exp(-x))


def fmt_price(p):
    if p <= 0:
        return "$0.00"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 10:
        return f"${p:.2f}"
    if p >= 1:
        return f"${p:.3f}"
    return f"${p:.4f}"


# ==============================================================================
# 2. GESTIONE MEMORIA PERSISTENTE (bot_state.json)
# ==============================================================================
def load_state():
    default_state = {
        "position": {
            "is_open": False,
            "coin_code": "SOL",
            "symbol": "SOL_USDT",
            "entry_price": 0.0,
            "amount_coin": 0.0,
            "invested_usdt": 0.0,
            "sl_price": 0.0,
            "tp_price": 0.0,
            "initial_sl": 0.0,
            "one_r": 0.0,
            "highest_price": 0.0,
            "breakeven_locked": False,
            "tp1_locked": False,
            "tp2_locked": False,
            "alerts_triggered": [],
            "opened_at": "",
            "opened_ts": 0,
            "mode": "SIMULATION",
            "order_id": ""
        },
        "stats": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_pct": 0.0,
            "total_pnl_usdt": 0.0,
            "daily_date": "",
            "daily_pnl_pct": 0.0,
            "daily_trades": 0,
            "last_action": "Nessuna operazione recente"
        },
        "history": []
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k in default_state["position"]:
                    data.setdefault("position", {}).setdefault(k, default_state["position"][k])
                for k in default_state["stats"]:
                    data.setdefault("stats", {}).setdefault(k, default_state["stats"][k])
                data.setdefault("history", [])
                return data
        except Exception as e:
            print(f"[WARN] Impossibile leggere {STATE_FILE}: {e}")
    return default_state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Salvataggio {STATE_FILE} fallito: {e}")


# ==============================================================================
# 3. ESECUZIONE ORDINI REALI SU PIONEX API v1 (HMAC-SHA256)
# ==============================================================================
def pionex_place_market_order(symbol, side, size=None, amount=None):
    if not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return {"ok": False, "mode": "SIMULATION", "msg": "Chiavi API Pionex non configurate (Simulazione attiva)"}

    if not AUTO_TRADE_ENABLED:
        return {"ok": False, "mode": "SIMULATION", "msg": "AUTO_TRADE_ENABLED=false"}

    base_url = "https://api.pionex.com"
    path = "/api/v1/trade/order"
    ts = str(int(time.time() * 1000))
    query = f"timestamp={ts}"

    body_dict = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET"
    }
    if side.upper() == "BUY" and amount is not None:
        body_dict["amount"] = f"{amount:.2f}"
    elif side.upper() == "SELL" and size is not None:
        body_dict["size"] = str(size)

    body_str = json.dumps(body_dict, separators=(',', ':'))
    sign_payload = f"POST{path}?{query}{body_str}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
        "Content-Type": "application/json",
        "User-Agent": HEADERS["User-Agent"]
    }

    try:
        url = f"{base_url}{path}?{query}"
        resp = requests.post(url, data=body_str, headers=headers, timeout=HTTP_TIMEOUT)
        rj = resp.json()
        if resp.status_code == 200 and rj.get("result") is True:
            order_id = str(rj.get("data", {}).get("orderId", "OK"))
            return {"ok": True, "mode": "LIVE_PIONEX", "order_id": order_id, "msg": f"Ordine Pionex ID {order_id}"}
        else:
            err_msg = rj.get("message", str(rj))
            return {"ok": False, "mode": "LIVE_ERROR", "msg": f"Rifiuto API Pionex: {err_msg}"}
    except Exception as e:
        return {"ok": False, "mode": "LIVE_ERROR", "msg": f"Errore connessione Pionex: {e}"}


# ==============================================================================
# 4. ACQUISIZIONE DATI GLOBALI E MULTI-COIN
# ==============================================================================
def fetch_eur_usd_rate():
    data = safe_get_json("https://api.kraken.com/0/public/Ticker", params={"pair": "EURUSD"})
    try:
        if data and not data.get("error"):
            res = data["result"]
            key = list(res.keys())[0]
            return float(res[key]["c"][0])
    except Exception:
        pass
    return 1.08


def fetch_kraken_ohlc(pair="SOLUSD", interval=60):
    url = "https://api.kraken.com/0/public/OHLC"
    data = safe_get_json(url, params={"pair": pair, "interval": interval})
    if not data or data.get("error"):
        return []
    result = data.get("result", {})
    candles_raw = None
    for k, v in result.items():
        if k != "last" and isinstance(v, list):
            candles_raw = v
            break
    if not candles_raw:
        return []

    candles = []
    for row in candles_raw:
        try:
            candles.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "vwap": float(row[5]),
                "volume": float(row[6]),
                "count": int(row[7]),
            })
        except Exception:
            continue
    return candles


def build_4h_from_1h(candles_1h):
    if not candles_1h:
        return []
    buckets = {}
    for c in candles_1h:
        b_time = (c["time"] // 14400) * 14400
        if b_time not in buckets:
            buckets[b_time] = {
                "time": b_time,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "vwap": c["vwap"],
                "volume": c["volume"],
                "count": c["count"]
            }
        else:
            b = buckets[b_time]
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c["volume"]
            b["count"] += c["count"]
    return [buckets[k] for k in sorted(buckets.keys())]


def fetch_kraken_orderbook(pair="SOLUSD", count=50):
    url = "https://api.kraken.com/0/public/Depth"
    data = safe_get_json(url, params={"pair": pair, "count": count})
    default = {"imbalance": 0.0, "spread_bps": 5.0, "bid_ask_ratio": 1.0}
    if not data or data.get("error"):
        return default
    try:
        res = data.get("result", {})
        key = list(res.keys())[0]
        bids = res[key].get("bids", [])
        asks = res[key].get("asks", [])
        if not bids or not asks:
            return default

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 5.0

        bid_vol_w = 0.0
        for b in bids:
            p, v = float(b[0]), float(b[1])
            dist = max(0.0001, (mid - p) / mid)
            w = math.exp(-dist * 100.0)
            bid_vol_w += p * v * w

        ask_vol_w = 0.0
        for a in asks:
            p, v = float(a[0]), float(a[1])
            dist = max(0.0001, (p - mid) / mid)
            w = math.exp(-dist * 100.0)
            ask_vol_w += p * v * w

        total = bid_vol_w + ask_vol_w
        imbalance = (bid_vol_w - ask_vol_w) / total if total > 0 else 0.0
        ratio = bid_vol_w / ask_vol_w if ask_vol_w > 0 else 1.0
        return {"imbalance": imbalance, "spread_bps": spread_bps, "bid_ask_ratio": ratio}
    except Exception:
        return default


def fetch_all_kraken_futures():
    url = "https://futures.kraken.com/derivatives/api/v3/tickers"
    data = safe_get_json(url)
    futures_map = {}
    if not data or "tickers" not in data:
        return futures_map
    try:
        for t in data["tickers"]:
            sym = str(t.get("symbol", "")).upper()
            fr_raw = float(t.get("fundingRate", 0.0) or 0.0)
            oi = float(t.get("openInterest", 0.0) or 0.0)
            mark = float(t.get("markPrice", 0.0) or 0.0)
            if abs(fr_raw) < 0.0005 and mark > 0:
                fr_8h_pct = fr_raw * 8.0 * 100.0
            else:
                fr_8h_pct = fr_raw * 100.0
            futures_map[sym] = {
                "funding_8h_pct": clamp(fr_8h_pct, -0.5, 0.5),
                "open_interest": oi,
                "available": True
            }
    except Exception:
        pass
    return futures_map


def fetch_fear_and_greed():
    data = safe_get_json("https://api.alternative.me/fng/?limit=1")
    try:
        if data and "data" in data and len(data["data"]) > 0:
            item = data["data"][0]
            return int(item.get("value", 50)), item.get("value_classification", "Neutral")
    except Exception:
        pass
    return 50, "Neutral"


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    events = []
    json_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    data = safe_get_json(json_url)
    if isinstance(data, list):
        for item in data:
            try:
                country = str(item.get("country", "")).upper()
                impact = str(item.get("impact", "")).capitalize()
                title = str(item.get("title", "Evento Macro"))
                date_str = str(item.get("date", ""))
                if country != "USD" or impact not in ("High", "Medium") or not date_str:
                    continue
                ev_dt = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                diff_hours = (ev_dt - now_utc).total_seconds() / 3600.0
                if -2.0 <= diff_hours <= 36.0:
                    events.append({"title": title, "impact": impact, "diff_hours": diff_hours})
            except Exception:
                continue

    if not events:
        xml_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        try:
            r = requests.get(xml_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.text.strip().startswith("<"):
                root = ET.fromstring(r.text)
                for ev in root.findall("event"):
                    country = (ev.findtext("country") or "").strip().upper()
                    impact = (ev.findtext("impact") or "").strip().capitalize()
                    title = (ev.findtext("title") or "Evento USD").strip()
                    date_s = (ev.findtext("date") or "").strip()
                    time_s = (ev.findtext("time") or "").strip().lower()
                    if country != "USD" or impact not in ("High", "Medium") or not date_s or not time_s:
                        continue
                    if "all day" in time_s or "tentative" in time_s:
                        continue
                    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", time_s)
                    if not m:
                        continue
                    hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
                    if ampm == "pm" and hh < 12:
                        hh += 12
                    if ampm == "am" and hh == 12:
                        hh = 0
                    dt_parsed = datetime.strptime(f"{date_s} {hh:02d}:{mm:02d}", "%m-%d-%Y %H:%M").replace(tzinfo=timezone.utc)
                    diff_hours = (dt_parsed - now_utc).total_seconds() / 3600.0
                    if -2.0 <= diff_hours <= 36.0:
                        events.append({"title": title, "impact": impact, "diff_hours": diff_hours})
        except Exception:
            pass

    events.sort(key=lambda x: abs(x["diff_hours"]))
    return events


# ==============================================================================
# 5. INDICATORI TECNICI ED ECONOMETRICI
# ==============================================================================
def calc_ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema = [values[0]]
    for price in values[1:]:
        ema.append(price * k + ema[-1] * (1.0 - k))
    return ema


def calc_ema(values, period):
    series = calc_ema_series(values, period)
    return series[-1] if series else 0.0


def calc_rsi_series(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    rsis = [50.0] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis.append(100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))

    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = max(0.0, d)
        l = max(0.0, -d)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rsis.append(100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))
    return rsis


def calc_rsi(closes, period=14):
    return calc_rsi_series(closes, period)[-1]


def calc_atr_series(candles, period=14):
    if len(candles) < 2:
        return [0.0]
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return calc_ema_series(trs, period)


def calc_atr(candles, period=14):
    s = calc_atr_series(candles, period)
    return s[-1] if s else 0.0


def calc_macd_hist_series(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return [0.0] * len(closes)
    ema_f = calc_ema_series(closes, fast)
    ema_s = calc_ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    sig_line = calc_ema_series(macd_line, signal)
    return [m - s for m, s in zip(macd_line, sig_line)]


def calc_bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        c = closes[-1] if closes else 0.0
        return c, c, c, 0.5, 0.0
    w = closes[-period:]
    mid = sum(w) / period
    std = statistics.pstdev(w)
    upper = mid + mult * std
    lower = mid - mult * std
    c = closes[-1]
    pct_b = (c - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    bw = (upper - lower) / mid if mid > 0 else 0.0
    return lower, mid, upper, pct_b, bw


def calc_adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return 20.0, 20.0, 20.0
    tr_list, pdm_list, mdm_list = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        ph, pl = candles[i - 1]["high"], candles[i - 1]["low"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - ph
        dn = pl - l
        pdm_list.append(up if (up > dn and up > 0) else 0.0)
        mdm_list.append(dn if (dn > up and dn > 0) else 0.0)

    atr_s = calc_ema_series(tr_list, period)
    pdm_s = calc_ema_series(pdm_list, period)
    mdm_s = calc_ema_series(mdm_list, period)

    dx_list = []
    plus_di, minus_di = 20.0, 20.0
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a <= 0:
            dx_list.append(0.0)
            continue
        pdi = 100.0 * (p / a)
        mdi = 100.0 * (m / a)
        plus_di, minus_di = pdi, mdi
        denom = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    adx = calc_ema(dx_list[-period * 2:], period)
    return adx, plus_di, minus_di


def calc_supertrend(candles, period=10, multiplier=3.0):
    if len(candles) < period + 2:
        return 1, candles[-1]["close"] if candles else 0.0
    atr_s = calc_atr_series(candles, period)
    final_upper, final_lower = 0.0, 0.0
    direction = 1

    for i in range(1, len(candles)):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2.0
        atr = atr_s[i]
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        prev_close = candles[i - 1]["close"]

        if i == 1:
            final_upper = basic_upper
            final_lower = basic_lower
            continue

        if basic_upper < final_upper or prev_close > final_upper:
            final_upper = basic_upper
        if basic_lower > final_lower or prev_close < final_lower:
            final_lower = basic_lower

        c = candles[i]["close"]
        if c > final_upper:
            direction = 1
        elif c < final_lower:
            direction = -1

    st_line = final_lower if direction == 1 else final_upper
    return direction, st_line


def calc_hurst_exponent(closes, max_lag=20):
    if len(closes) < 60:
        return 0.50
    log_p = [math.log(max(p, 1e-8)) for p in closes[-120:]]
    lags = range(2, min(max_lag, len(log_p) // 3))
    x_vals, y_vals = [], []
    for lag in lags:
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        std = statistics.pstdev(diffs)
        if std > 0:
            x_vals.append(math.log(lag))
            y_vals.append(math.log(std))
    if len(x_vals) < 3:
        return 0.50
    mx = sum(x_vals) / len(x_vals)
    my = sum(y_vals) / len(y_vals)
    num = sum((x - mx) * (y - my) for x, y in zip(x_vals, y_vals))
    den = sum((x - mx) ** 2 for x in x_vals)
    hurst = num / den if den > 0 else 0.50
    return clamp(hurst, 0.20, 0.85)


def calc_linreg_zscore(closes, window=40):
    if len(closes) < window:
        return 0.0, 0.0
    y = closes[-window:]
    x = list(range(window))
    mx = (window - 1) / 2.0
    my = sum(y) / window
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    slope = num / den if den > 0 else 0.0
    intercept = my - slope * mx
    residuals = [yi - (slope * xi + intercept) for xi, yi in zip(x, y)]
    std_res = statistics.pstdev(residuals)
    z_score = residuals[-1] / std_res if std_res > 0 else 0.0
    slope_pct = (slope / my) * 100.0 if my > 0 else 0.0
    return z_score, slope_pct


def calc_volume_profile_poc(candles, bins=24, lookback=120):
    subset = candles[-lookback:] if len(candles) >= lookback else candles
    if not subset:
        return 0.0
    low_min = min(c["low"] for c in subset)
    high_max = max(c["high"] for c in subset)
    if high_max <= low_min:
        return subset[-1]["close"]
    step = (high_max - low_min) / bins
    vol_bins = [0.0] * bins
    for c in subset:
        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        idx = int((typical - low_min) / step)
        idx = clamp(idx, 0, bins - 1)
        vol_bins[idx] += c["volume"]
    best_idx = max(range(bins), key=lambda i: vol_bins[i])
    return low_min + (best_idx + 0.5) * step


def calc_rolling_vwap(candles_15m, bars=96):
    subset = candles_15m[-bars:] if len(candles_15m) >= bars else candles_15m
    if not subset:
        return 0.0
    pv = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * c["volume"] for c in subset)
    vv = sum(c["volume"] for c in subset)
    return pv / vv if vv > 0 else subset[-1]["close"]


def calc_volume_zscore(candles, window=30):
    if len(candles) < window + 2:
        return 0.0
    vols = [c["volume"] for c in candles[-window - 1:-1]]
    mean_v = sum(vols) / len(vols)
    std_v = statistics.pstdev(vols)
    curr_v = candles[-1]["volume"]
    return (curr_v - mean_v) / std_v if std_v > 0 else 0.0


# ==============================================================================
# 6. RETI NEURALI E ML REAL-TIME (MLP + Lorentzian k-NN + Monte Carlo)
# ==============================================================================
def build_feature_matrix(candles_1h):
    closes = [c["close"] for c in candles_1h]
    rsis = calc_rsi_series(closes, 14)
    ema20s = calc_ema_series(closes, 20)
    macds = calc_macd_hist_series(closes, 12, 26, 9)
    atrs = calc_atr_series(candles_1h, 14)

    features = []
    for i in range(len(candles_1h)):
        if i < 30:
            features.append([0.0] * 6)
            continue
        c = closes[i]
        atr = max(atrs[i], c * 0.001)
        f1 = clamp((rsis[i] - 50.0) / 25.0, -2.5, 2.5)
        f2 = clamp((c - ema20s[i]) / (2.0 * atr), -2.5, 2.5)
        f3 = clamp(macds[i] / (0.5 * atr), -2.5, 2.5)
        ret3 = (c - closes[i - 3]) / closes[i - 3] if closes[i - 3] > 0 else 0.0
        f4 = clamp(ret3 / 0.03, -2.5, 2.5)
        v_win = [candles_1h[j]["volume"] for j in range(i - 20, i)]
        mv = sum(v_win) / len(v_win)
        sv = statistics.pstdev(v_win)
        f5 = clamp((candles_1h[i]["volume"] - mv) / sv if sv > 0 else 0.0, -2.5, 2.5)
        rng = max(candles_1h[i]["high"] - candles_1h[i]["low"], 1e-8)
        f6 = ((c - candles_1h[i]["low"]) / rng) * 2.0 - 1.0
        features.append([f1, f2, f3, f4, f5, f6])
    return features


def lorentzian_knn_predict(candles_1h, features, horizon=4, k=18):
    n = len(candles_1h)
    if n < 120:
        return 0.50
    closes = [c["close"] for c in candles_1h]
    atrs = calc_atr_series(candles_1h, 14)
    curr_x = features[-1]

    candidates = []
    for i in range(35, n - horizon - 1):
        hist_x = features[i]
        dist = sum(math.log(1.0 + abs(a - b)) for a, b in zip(curr_x, hist_x))
        future_ret = closes[i + horizon] - closes[i]
        threshold = 0.15 * atrs[i]
        if future_ret > threshold:
            label = 1.0
        elif future_ret < -threshold:
            label = 0.0
        else:
            label = 0.5
        candidates.append((dist, label))

    candidates.sort(key=lambda x: x[0])
    nearest = candidates[:k]
    if not nearest:
        return 0.50

    weighted_sum = 0.0
    weight_total = 0.0
    for dist, label in nearest:
        w = 1.0 / (dist + 0.05)
        weighted_sum += w * label
        weight_total += w
    return clamp(weighted_sum / weight_total if weight_total > 0 else 0.50, 0.05, 0.95)


def online_mlp_neural_net(candles_1h, features, horizon=4, epochs=35, lr=0.045):
    n = len(candles_1h)
    if n < 120:
        return 0.50
    closes = [c["close"] for c in candles_1h]
    dataset = []
    start_idx = max(35, n - 320)
    for i in range(start_idx, n - horizon - 1):
        x = features[i]
        y = 1.0 if closes[i + horizon] > closes[i] else 0.0
        dataset.append((x, y))

    if len(dataset) < 30:
        return 0.50

    rng = random.Random(42)
    in_dim = 6
    hid_dim = 8
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(in_dim)] for _ in range(hid_dim)]
    b1 = [0.0 for _ in range(hid_dim)]
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(hid_dim)]
    b2 = 0.0

    for ep in range(epochs):
        step_lr = lr * (0.97 ** ep)
        for x, y in dataset:
            h_raw = [sum(W1[j][i] * x[i] for i in range(in_dim)) + b1[j] for j in range(hid_dim)]
            h_act = [math.tanh(clamp(v, -10.0, 10.0)) for v in h_raw]
            out_raw = sum(W2[j] * h_act[j] for j in range(hid_dim)) + b2
            y_hat = sigmoid(out_raw)

            dz2 = y_hat - y
            for j in range(hid_dim):
                grad_w2 = dz2 * h_act[j] + 0.001 * W2[j]
                dh = dz2 * W2[j] * (1.0 - h_act[j] ** 2)
                W2[j] -= step_lr * grad_w2
                b1[j] -= step_lr * dh
                for i in range(in_dim):
                    W1[j][i] -= step_lr * (dh * x[i] + 0.001 * W1[j][i])
            b2 -= step_lr * dz2

    curr_x = features[-1]
    h_raw = [sum(W1[j][i] * curr_x[i] for i in range(in_dim)) + b1[j] for j in range(hid_dim)]
    h_act = [math.tanh(clamp(v, -10.0, 10.0)) for v in h_raw]
    out_raw = sum(W2[j] * h_act[j] for j in range(hid_dim)) + b2
    return clamp(sigmoid(out_raw), 0.05, 0.95)


def monte_carlo_Barrier_sim(candles_1h, curr_price, tp_price, sl_price, paths=350, horizon_bars=10):
    if len(candles_1h) < 50 or curr_price <= 0:
        return 0.50
    closes = [c["close"] for c in candles_1h[-120:]]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 20:
        return 0.50

    rng = random.Random(1337)
    wins = 0.0
    for _ in range(paths):
        p = curr_price
        outcome = None
        for _ in range(horizon_bars):
            r = rng.choice(rets)
            p *= math.exp(r)
            if p >= tp_price:
                outcome = 1.0
                break
            if p <= sl_price:
                outcome = 0.0
                break
        if outcome is None:
            outcome = 0.65 if p > curr_price else 0.35
        wins += outcome
    return clamp(wins / paths, 0.05, 0.95)


# ==============================================================================
# 7. LE 5 SKILL OPERATIVE AVANZATE (CALIBRATE PER 3-4 SCALP/GIORNO)
# ==============================================================================
def skill_time_machine_backtest(candles_1h, features, horizon_bars=6):
    if len(candles_1h) < 120:
        return {"win_rate": 55.0, "expectancy_r": 0.25, "matches": 12}

    closes = [c["close"] for c in candles_1h]
    highs = [c["high"] for c in candles_1h]
    lows = [c["low"] for c in candles_1h]
    atrs = calc_atr_series(candles_1h, 14)
    curr_x = features[-1]

    candidates = []
    for i in range(40, len(candles_1h) - horizon_bars - 1):
        dist = sum(math.log(1.0 + abs(a - b)) for a, b in zip(curr_x, features[i]))
        candidates.append((dist, i))

    candidates.sort(key=lambda x: x[0])
    top_matches = candidates[:20]

    wins = 0
    total_r = 0.0
    for _, idx in top_matches:
        entry = closes[idx]
        risk = max(atrs[idx] * 1.15, entry * 0.01)
        tp = entry + 1.45 * risk
        sl = entry - 1.0 * risk

        outcome_r = None
        for h in range(1, horizon_bars + 1):
            if lows[idx + h] <= sl:
                outcome_r = -1.0
                break
            if highs[idx + h] >= tp:
                outcome_r = 1.45
                break
        if outcome_r is None:
            final_c = closes[idx + horizon_bars]
            outcome_r = clamp((final_c - entry) / risk, -1.0, 1.45)

        if outcome_r > 0:
            wins += 1
        total_r += outcome_r

    n = len(top_matches)
    win_rate = (wins / n) * 100.0 if n > 0 else 50.0
    expectancy_r = total_r / n if n > 0 else 0.0
    return {"win_rate": win_rate, "expectancy_r": expectancy_r, "matches": n}


def skill_chart_scanner_entry_timer(candles_15m, vol_z_1h, ob_imbalance):
    """
    Calibrato per Fast-Scalp (Dip-Bounce oppure Momentum-Breakout 15m).
    """
    if len(candles_15m) < 25:
        return {"triggered": False, "triggers": 0, "vwap_15m": 0.0, "summary": "Dati 15m insufficienti"}

    closes_15m = [c["close"] for c in candles_15m]
    c15 = closes_15m[-1]
    o15 = candles_15m[-1]["open"]
    p15 = closes_15m[-2]
    ema20_15m = calc_ema(closes_15m, 20)
    rsi_15m_series = calc_rsi_series(closes_15m, 14)
    rsi_15m = rsi_15m_series[-1]
    rsi_15m_prev = rsi_15m_series[-2] if len(rsi_15m_series) >= 2 else rsi_15m
    vwap_15m = calc_rolling_vwap(candles_15m, 96)

    triggers = 0
    notes = []

    # 1. Zona di Valore / Trend rispetto al VWAP 24h + Candela Attiva
    vwap_diff_pct = (c15 - vwap_15m) / vwap_15m if vwap_15m > 0 else 0.0
    if -0.015 <= vwap_diff_pct <= 0.028 and (c15 >= o15 or c15 >= p15):
        triggers += 1
        notes.append("VWAP Active")

    # 2. Rimbalzo o Tenuta sopra EMA20 15m con RSI 15m costruttivo
    if c15 >= ema20_15m * 0.997 and rsi_15m >= rsi_15m_prev - 1.5:
        triggers += 1
        notes.append("EMA20 15m Hold/Bounce")

    # 3. Partecipazione Volumi (non in contrazione estrema)
    if vol_z_1h >= -0.20:
        triggers += 1
        notes.append(f"Vol Active (Z={vol_z_1h:+.2f})")

    # 4. Pressione Bid L2 favorevole o neutra-positiva
    if ob_imbalance >= 0.01:
        triggers += 1
        notes.append(f"L2 Bid ({ob_imbalance * 100:+.1f}%)")

    rsi_ok = 28.0 <= rsi_15m <= 72.0
    triggered = (triggers >= 2) and rsi_ok
    if not rsi_ok:
        notes.append(f"RSI 15m estremo ({rsi_15m:.0f})")

    summary = " + ".join(notes) if notes else "In attesa trigger 15m"
    return {
        "triggered": triggered,
        "triggers": triggers,
        "vwap_15m": vwap_15m,
        "ema20_15m": ema20_15m,
        "rsi_15m": rsi_15m,
        "rsi_15m_prev": rsi_15m_prev,
        "summary": summary
    }


def skill_budget_and_risk_manager(invest_eur, eur_usd, entry_price, sl_price, tp_price, state, macro_high_soon):
    total_usdt = invest_eur * eur_usd
    cash_buffer_usdt = total_usdt * (CASH_BUFFER_PCT / 100.0)
    usable_usdt = total_usdt - cash_buffer_usdt

    sl_dist_pct = ((entry_price - sl_price) / entry_price) * 100.0 if entry_price > 0 else 2.0
    tp_dist_pct = ((tp_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 2.5
    rr_ratio = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0.0

    if sl_dist_pct > MAX_LOSS_PCT_PER_TRADE and sl_dist_pct > 0:
        scale_factor = MAX_LOSS_PCT_PER_TRADE / sl_dist_pct
        position_usdt = usable_usdt * scale_factor
    else:
        position_usdt = usable_usdt

    position_usdt = clamp(position_usdt, 10.5, usable_usdt)
    max_loss_usdt = position_usdt * (sl_dist_pct / 100.0)
    expected_gain_usdt = position_usdt * (tp_dist_pct / 100.0)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = state["stats"]
    if stats.get("daily_date") != today_str:
        stats["daily_date"] = today_str
        stats["daily_pnl_pct"] = 0.0
        stats["daily_trades"] = 0

    daily_pnl_pct = stats.get("daily_pnl_pct", 0.0)
    daily_trades = stats.get("daily_trades", 0)

    reasons = []
    passed = True
    if rr_ratio < MIN_REWARD_RISK_R:
        passed = False
        reasons.append(f"R:R basso ({rr_ratio:.2f}x < {MIN_REWARD_RISK_R}x)")
    if daily_pnl_pct <= -DAILY_MAX_LOSS_PCT:
        passed = False
        reasons.append(f"Circuit Breaker Giornaliero ({daily_pnl_pct:+.2f}%)")
    if daily_trades >= MAX_TRADES_PER_DAY:
        passed = False
        reasons.append(f"Max {MAX_TRADES_PER_DAY} trade giornalieri raggiunti")
    if macro_high_soon:
        passed = False
        reasons.append("News Macro USD High Impact imminente (<2h)")

    status_str = "PASS (Rischio OK)" if passed else "BLOCCATO: " + ", ".join(reasons)
    return {
        "passed": passed,
        "status_str": status_str,
        "total_usdt": total_usdt,
        "position_usdt": position_usdt,
        "cash_buffer_usdt": cash_buffer_usdt,
        "rr_ratio": rr_ratio,
        "sl_dist_pct": sl_dist_pct,
        "tp_dist_pct": tp_dist_pct,
        "max_loss_usdt": max_loss_usdt,
        "expected_gain_usdt": expected_gain_usdt
    }


def skill_exit_planner(entry_price, sl_price, tp_price):
    one_r = max(entry_price - sl_price, entry_price * 0.008)
    be_trigger = entry_price + 0.65 * one_r
    be_fee_lock = entry_price * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0)
    tp1_trigger = entry_price + 1.0 * one_r
    tp1_lock = be_fee_lock + 0.25 * one_r
    tp2_trigger = entry_price + 1.45 * one_r
    tp2_lock = entry_price + 0.85 * one_r
    return {
        "one_r": one_r,
        "be_trigger": be_trigger,
        "be_fee_lock": be_fee_lock,
        "tp1_trigger": tp1_trigger,
        "tp1_lock": tp1_lock,
        "tp2_trigger": tp2_trigger,
        "tp2_lock": tp2_lock,
        "tp3_target": tp_price
    }


def skill_nyse_bell_and_session():
    now_utc = datetime.now(timezone.utc)
    minutes_utc = now_utc.hour * 60 + now_utc.minute
    nyse_open_min = 13 * 60 + 30
    nyse_close_min = 20 * 60
    diff_open = abs(minutes_utc - nyse_open_min)

    if diff_open <= 30:
        return "🔔 NYSE OPENING BELL (Alta Volatilita' USA)"
    elif nyse_open_min < minutes_utc <= nyse_close_min:
        return "🇺🇸 Sessione Wall Street Attiva (Alta Liquidita')"
    elif 7 * 60 <= minutes_utc < nyse_open_min:
        return "🇪🇺 Sessione Europea Attiva"
    else:
        return "🌏 Sessione Asiatica / Notturna"


# ==============================================================================
# 8. VALUTAZIONE SINGOLA COIN (CALIBRATA PER 3-4 FAST-SCALP AL GIORNO)
# ==============================================================================
def evaluate_coin(coin_cfg, futures_map, fng_val, macro_events):
    code = coin_cfg["code"]
    pair = coin_cfg["kraken"]

    candles_1h = fetch_kraken_ohlc(pair, interval=60)
    candles_15m = fetch_kraken_ohlc(pair, interval=15)
    candles_4h = build_4h_from_1h(candles_1h)
    ob = fetch_kraken_orderbook(pair, count=50)

    if len(candles_1h) < 60 or len(candles_4h) < 30 or len(candles_15m) < 30:
        return None

    fut = futures_map.get(coin_cfg["futures"], {"funding_8h_pct": 0.005, "open_interest": 0.0, "available": False})

    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h]
    closes_15m = [c["close"] for c in candles_15m]
    price = closes_15m[-1]

    ema20_4h = calc_ema(closes_4h, 20)
    ema50_4h = calc_ema(closes_4h, 50)
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)

    rsi_4h = calc_rsi(closes_4h, 14)
    rsi_1h_series = calc_rsi_series(closes_1h, 14)
    rsi_1h, rsi_1h_prev = rsi_1h_series[-1], rsi_1h_series[-2]
    rsi_15m_series = calc_rsi_series(closes_15m, 14)
    rsi_15m = rsi_15m_series[-1]
    rsi_15m_prev = rsi_15m_series[-2] if len(rsi_15m_series) >= 2 else rsi_15m

    macd_hist_s = calc_macd_hist_series(closes_1h, 12, 26, 9)
    macd_hist, macd_hist_prev = macd_hist_s[-1], macd_hist_s[-2]

    atr_1h = calc_atr(candles_1h, 14)
    atr_pct = (atr_1h / price) * 100.0 if price > 0 else 1.5

    bb_low, bb_mid, bb_up, bb_pct_b, bb_bw = calc_bollinger(closes_1h, 20, 2.0)
    adx_1h, plus_di, minus_di = calc_adx(candles_1h, 14)
    st_dir_1h, st_line_1h = calc_supertrend(candles_1h, 10, 3.0)
    hurst = calc_hurst_exponent(closes_1h, 20)
    z_linreg, slope_pct = calc_linreg_zscore(closes_1h, 40)
    poc_price = calc_volume_profile_poc(candles_1h, 24, 120)
    vol_z = calc_volume_zscore(candles_1h, 30)

    # Target & Stop da vero Intraday Fast-Scalp (SL 0.90%-1.80%, TP 1.35%-2.50%)
    recent_lows = [c["low"] for c in candles_15m[-16:]]
    swing_low = min(recent_lows) if recent_lows else price * 0.985
    recent_highs = [c["high"] for c in candles_1h[-24:]]
    swing_high = max(recent_highs) if recent_highs else price * 1.025

    sl_dist = max(price * 0.009, min(price * 0.018, 1.15 * atr_1h))
    sl_price = max(price - sl_dist, swing_low - 0.15 * atr_1h)
    sl_price = clamp(sl_price, price * 0.980, price * 0.991)

    actual_risk = price - sl_price
    min_tp_by_r = price + actual_risk * 1.35
    tp_candidate = max(min_tp_by_r, min(swing_high * 0.998, price + 2.2 * atr_1h))
    tp_price = clamp(tp_candidate, price * 1.0135, price * 1.025)

    # Reti Neurali e ML
    features = build_feature_matrix(candles_1h)
    prob_knn = lorentzian_knn_predict(candles_1h, features, horizon=4, k=18)
    prob_mlp = online_mlp_neural_net(candles_1h, features, horizon=4, epochs=35, lr=0.045)
    prob_mc = monte_carlo_Barrier_sim(candles_1h, price, tp_price, sl_price, paths=300, horizon_bars=10)
    ai_ensemble_prob = 0.40 * prob_mlp + 0.35 * prob_knn + 0.25 * prob_mc

    # Skill Evaluation
    tm_stats = skill_time_machine_backtest(candles_1h, features, horizon_bars=6)
    scanner_timer = skill_chart_scanner_entry_timer(candles_15m, vol_z, ob["imbalance"])

    # RUBRICA A 100 PUNTI (Trend + Scalp Rebound Intraday)
    # A. Trend & Struttura Intraday (Max 25 pt)
    score_trend = 0
    if price > ema50_4h:
        score_trend += 8
    elif price > ema20_4h * 0.992:
        score_trend += 5

    if price > ema20_1h and ema20_1h >= ema50_1h * 0.998:
        score_trend += 9
    elif price > ema20_1h or scanner_timer["triggered"]:
        score_trend += 6

    if st_dir_1h == 1 or rsi_15m > rsi_15m_prev:
        score_trend += 8
    score_trend = clamp(score_trend, 0, 25)

    # B. Reti Neurali + Time Machine (Max 25 pt)
    ai_pts = round(clamp((ai_ensemble_prob - 0.35) / 0.35, 0.0, 1.0) * 18.0)
    tm_pts = 7 if (tm_stats["win_rate"] >= 52.0 and tm_stats["expectancy_r"] > 0.05) else (4 if tm_stats["win_rate"] >= 46.0 else 2)
    score_ai = clamp(ai_pts + tm_pts, 0, 25)

    # C. Momentum & Entry Timer 15m (Max 20 pt)
    score_mom = 0
    if 38.0 <= rsi_1h <= 66.0:
        score_mom += 7
    elif 30.0 <= rsi_1h < 38.0 and rsi_1h > rsi_1h_prev:
        score_mom += 8
    elif 66.0 < rsi_1h <= 72.0:
        score_mom += 4

    if macd_hist > macd_hist_prev or macd_hist > 0:
        score_mom += 6

    if scanner_timer["triggered"]:
        score_mom += 7
    elif scanner_timer["triggers"] >= 1:
        score_mom += 4
    score_mom = clamp(score_mom, 0, 20)

    # D. Orderbook L2, Volumi & POC (Max 15 pt)
    score_micro = 0
    imb = ob["imbalance"]
    if imb > 0.12:
        score_micro += 8
    elif imb > 0.01:
        score_micro += 6
    elif imb > -0.08:
        score_micro += 4

    if price >= poc_price * 0.992:
        score_micro += 4
    if vol_z > -0.15:
        score_micro += 3
    score_micro = clamp(score_micro, 0, 15)

    # E. Derivati, Sentiment & Macro (Max 15 pt)
    score_macro = 0
    fr = fut["funding_8h_pct"]
    if -0.02 <= fr <= 0.025:
        score_macro += 5
    elif fr < -0.02:
        score_macro += 6
    else:
        score_macro += 2

    if 20 <= fng_val <= 78:
        score_macro += 5
    else:
        score_macro += 3

    high_soon = [e for e in macro_events if e["impact"] == "High" and -0.5 <= e["diff_hours"] <= 2.0]
    if not high_soon:
        score_macro += 5
    score_macro = clamp(score_macro, 0, 15)

    raw_100 = score_trend + score_ai + score_mom + score_micro + score_macro
    final_score = clamp(int(round(raw_100 / 10.0)), 1, 10)

    return {
        "coin": coin_cfg,
        "code": code,
        "price": price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "atr_1h": atr_1h,
        "atr_pct": atr_pct,
        "raw_100": raw_100,
        "final_score": final_score,
        "score_trend": score_trend,
        "score_ai": score_ai,
        "score_mom": score_mom,
        "score_micro": score_micro,
        "score_macro": score_macro,
        "ai_prob": ai_ensemble_prob,
        "prob_mlp": prob_mlp,
        "prob_knn": prob_knn,
        "prob_mc": prob_mc,
        "rsi_1h": rsi_1h,
        "rsi_4h": rsi_4h,
        "rsi_15m": rsi_15m,
        "rsi_15m_prev": rsi_15m_prev,
        "adx_1h": adx_1h,
        "hurst": hurst,
        "z_linreg": z_linreg,
        "poc_price": poc_price,
        "vol_z": vol_z,
        "ob": ob,
        "fut": fut,
        "st_dir_1h": st_dir_1h,
        "tm_stats": tm_stats,
        "scanner_timer": scanner_timer,
        "high_soon": bool(high_soon)
    }


def scan_all_coins(futures_map, fng_val, macro_events):
    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_code = {
            executor.submit(evaluate_coin, cfg, futures_map, fng_val, macro_events): cfg["code"]
            for cfg in COINS
        }
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                res = future.result()
                if res:
                    results[code] = res
            except Exception as e:
                print(f"[WARN] Errore analisi {code}: {e}")
    return results


# ==============================================================================
# 9. MOTORE AUTONOMO FAST-SCALP (3-4 OPERAZIONI/GIORNO)
# ==============================================================================
def manage_autonomous_trading(state, best_eval, all_evals, eur_usd):
    pos = state["position"]
    stats = state["stats"]
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%d/%m %H:%M UTC")
    now_ts = int(now_dt.timestamp())

    best_risk = skill_budget_and_risk_manager(
        INVEST_EUR, eur_usd,
        best_eval["price"], best_eval["sl_price"], best_eval["tp_price"],
        state, best_eval["high_soon"]
    )
    best_eval["risk_manager"] = best_risk
    best_eval["exit_plan"] = skill_exit_planner(best_eval["price"], best_eval["sl_price"], best_eval["tp_price"])

    # CASO A: POSIZIONE APERTA -> GESTIONE SCALPING E PROFIT LOCK
    if pos["is_open"]:
        open_code = pos.get("coin_code", "SOL")
        open_eval = all_evals.get(open_code, best_eval)
        curr_price = open_eval["price"]
        entry = pos["entry_price"]
        sl = pos["sl_price"]
        tp = pos["tp_price"]
        one_r = pos.get("one_r") or max(entry - pos.get("initial_sl", sl), entry * 0.009)
        highest = max(pos.get("highest_price", entry), curr_price)
        pos["highest_price"] = highest

        gross_pnl_pct = ((curr_price - entry) / entry) * 100.0 if entry > 0 else 0.0
        net_pnl_pct = gross_pnl_pct - PIONEX_FEE_ROUNDTRIP_PCT
        pnl_usdt = pos["invested_usdt"] * (net_pnl_pct / 100.0)
        current_r = (curr_price - entry) / one_r if one_r > 0 else 0.0

        opened_ts = pos.get("opened_ts") or now_ts
        hours_open = max(0.0, (now_ts - opened_ts) / 3600.0)

        alerts_fired = []
        trail_msg = ""

        # Stadio 1: +0.65R -> Break-Even Copertura Commissioni
        be_fee_price = round(entry * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0), 6)
        if not pos.get("breakeven_locked", False) and (current_r >= 0.65 or (curr_price >= entry * 1.0055 and open_eval["rsi_15m"] >= 66.0)):
            if sl < be_fee_price:
                pos["sl_price"] = be_fee_price
                sl = be_fee_price
            pos["breakeven_locked"] = True
            alerts_fired.append(f"🔔 Fast-Scalp Lock: Stop-Loss portato a Break-Even Netto ({fmt_price(sl)})")
            trail_msg = "🛡️ Break-Even Netto Attivato"

        # Stadio 2: +1.0R -> Blindatura +0.30R
        if not pos.get("tp1_locked", False) and current_r >= 1.0:
            new_sl = round(be_fee_price + 0.30 * one_r, 6)
            if new_sl > sl:
                pos["sl_price"] = new_sl
                sl = new_sl
            pos["tp1_locked"] = True
            alerts_fired.append(f"🔔 Alert +1.0R: Profitto blindato a {fmt_price(sl)}")
            trail_msg = f"🔒 Profitto +1R Blindato ({fmt_price(sl)})"

        # Stadio 3: +1.45R -> Blindatura +0.85R
        if not pos.get("tp2_locked", False) and current_r >= 1.45:
            new_sl = round(entry + 0.85 * one_r, 6)
            if new_sl > sl:
                pos["sl_price"] = new_sl
                sl = new_sl
            pos["tp2_locked"] = True
            alerts_fired.append(f"🔔 Alert +1.45R: Trailing Stop alzato a {fmt_price(sl)}")
            trail_msg = f"🚀 Trailing +1.45R ({fmt_price(sl)})"

        # Stadio 4: +1.90R -> Blindatura +1.35R
        if current_r >= 1.90 and "R190" not in pos.get("alerts_triggered", []):
            new_sl = round(entry + 1.35 * one_r, 6)
            if new_sl > sl:
                pos["sl_price"] = new_sl
                sl = new_sl
            pos.setdefault("alerts_triggered", []).append("R190")
            alerts_fired.append(f"🔔 Alert +1.9R: Super-Trailing a {fmt_price(sl)}")

        # Rilevamento Inversione 15m per Incasso Rapido (Fast-Scalp Exit)
        rsi_15m = open_eval.get("rsi_15m", 50.0)
        rsi_15m_prev = open_eval.get("rsi_15m_prev", rsi_15m)
        ob_imb = open_eval["ob"]["imbalance"]
        momentum_fading_15m = (
            (rsi_15m_prev >= 61.0 and rsi_15m < rsi_15m_prev - 4.0)
            or (ob_imb <= -0.10 and open_eval["ai_prob"] < 0.50)
            or (highest >= entry + 1.0 * one_r and curr_price <= highest - 0.40 * one_r)
        )

        close_reason = None
        if curr_price >= tp:
            close_reason = f"🎯 TAKE PROFIT RAGGIUNTO ({fmt_price(curr_price)})"
        elif curr_price <= sl:
            if sl > entry:
                close_reason = f"🔒 PROFIT LOCK / TRAILING STOP INCASSATO ({fmt_price(curr_price)})"
            else:
                close_reason = f"🛑 STOP LOSS TOCCATO ({fmt_price(curr_price)})"
        elif net_pnl_pct >= SCALP_MIN_NET_PROFIT_PCT and momentum_fading_15m:
            close_reason = f"💰 FAST-SCALP PROFIT TAKE (+{net_pnl_pct:.2f}% netto su rallentamento 15m)"
        elif hours_open >= TIME_STOP_PROFIT_HOURS and net_pnl_pct >= 0.35:
            close_reason = f"⏱️ TIME-STOP SCALP IN PROFITTO (+{net_pnl_pct:.2f}% dopo {hours_open:.1f}h per liberare capitale)"
        elif hours_open >= TIME_STOP_STALE_HOURS and open_eval["ai_prob"] < 0.46 and net_pnl_pct > -0.60:
            close_reason = f"⏱️ ROTAZIONE CAPITALE ({hours_open:.1f}h in stallo, AI={open_eval['ai_prob']*100:.0f}%)"
        elif open_eval["final_score"] <= 3 and open_eval["ai_prob"] < 0.40:
            close_reason = f"⚠️ USCITA DIFENSIVA AI (Score {open_code} sceso a {open_eval['final_score']}/10)"

        if close_reason:
            prec = COIN_MAP.get(open_code, COINS[0])["prec"]
            sell_size = round(pos["amount_coin"], prec) if prec > 0 else int(pos["amount_coin"])
            order_res = pionex_place_market_order(pos["symbol"], "SELL", size=sell_size)

            stats["total_trades"] += 1
            if net_pnl_pct >= 0:
                stats["wins"] += 1
            else:
                stats["losses"] += 1
            stats["total_pnl_pct"] = round(stats["total_pnl_pct"] + net_pnl_pct, 2)
            stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + pnl_usdt, 2)
            stats["daily_pnl_pct"] = round(stats.get("daily_pnl_pct", 0.0) + net_pnl_pct, 2)
            stats["last_action"] = f"CHIUSO {open_code} ({net_pnl_pct:+.2f}%) il {now_str}"

            state.setdefault("history", []).insert(0, {
                "coin": open_code,
                "entry": entry,
                "exit": curr_price,
                "pnl_pct": round(net_pnl_pct, 2),
                "pnl_usdt": round(pnl_usdt, 2),
                "reason": close_reason,
                "closed_at": now_str,
                "mode": pos.get("mode", "SIMULATION")
            })
            state["history"] = state["history"][:25]

            pos["is_open"] = False
            save_state(state)
            return {
                "action_state": "CLOSED_NOW",
                "headline": f"🔔 POSIZIONE {open_code} CHIUSA AUTOMATICAMENTE",
                "detail": f"{close_reason} | PnL Netto: {net_pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT) | {order_res['msg']}",
                "pnl_pct": net_pnl_pct,
                "pnl_usdt": pnl_usdt,
                "current_r": current_r,
                "alerts_fired": alerts_fired
            }
        else:
            save_state(state)
            extra = f" | {trail_msg}" if trail_msg else ""
            return {
                "action_state": "HOLDING",
                "headline": f"⏳ MANTENIMENTO POSIZIONE LONG SU {open_code} ({pos['mode']})",
                "detail": f"Entry {fmt_price(entry)} -> Attuale {fmt_price(curr_price)} ({net_pnl_pct:+.2f}% netto / {current_r:+.2f}R){extra}",
                "pnl_pct": net_pnl_pct,
                "pnl_usdt": pnl_usdt,
                "current_r": current_r,
                "alerts_fired": alerts_fired
            }

    # CASO B: FLAT -> VERIFICA APERTURA NUOVA POSIZIONE FAST-SCALP
    else:
        score = best_eval["final_score"]
        raw_100 = best_eval["raw_100"]
        ai_prob = best_eval["ai_prob"]
        timer_ok = best_eval["scanner_timer"]["triggered"]
        risk_ok = best_risk["passed"]

        if score >= Entry_Score_Min and raw_100 >= Entry_Raw_100_Min and ai_prob >= 0.50 and timer_ok and risk_ok:
            coin_cfg = best_eval["coin"]
            code = best_eval["code"]
            price = best_eval["price"]
            usdt_to_invest = round(best_risk["position_usdt"], 2)

            order_res = pionex_place_market_order(coin_cfg["pionex"], "BUY", amount=usdt_to_invest)
            exec_mode = "LIVE_PIONEX" if order_res["ok"] else "SIMULATION"
            prec = coin_cfg["prec"]
            amount_coin = round((usdt_to_invest / price) * 0.999, max(prec, 2))
            one_r = max(price - best_eval["sl_price"], price * 0.009)

            pos.update({
                "is_open": True,
                "coin_code": code,
                "symbol": coin_cfg["pionex"],
                "entry_price": price,
                "amount_coin": amount_coin,
                "invested_usdt": usdt_to_invest,
                "sl_price": best_eval["sl_price"],
                "tp_price": best_eval["tp_price"],
                "initial_sl": best_eval["sl_price"],
                "one_r": one_r,
                "highest_price": price,
                "breakeven_locked": False,
                "tp1_locked": False,
                "tp2_locked": False,
                "alerts_triggered": [],
                "opened_at": now_str,
                "opened_ts": now_ts,
                "mode": exec_mode,
                "order_id": order_res.get("order_id", "SIM")
            })
            stats["daily_trades"] = stats.get("daily_trades", 0) + 1
            stats["last_action"] = f"APERTO LONG {code} a {fmt_price(price)} il {now_str}"
            save_state(state)

            return {
                "action_state": "OPENED_NOW",
                "headline": f"🚀 NUOVA POSIZIONE FAST-SCALP APERTA SU {code} ({exec_mode})",
                "detail": f"Acquistati ~{amount_coin} {code} ({usdt_to_invest:.2f} USDT) a {fmt_price(price)} | SL {fmt_price(best_eval['sl_price'])} | TP {fmt_price(best_eval['tp_price'])}",
                "pnl_pct": 0.0,
                "pnl_usdt": 0.0,
                "current_r": 0.0,
                "alerts_fired": [f"🔔 Fast-Scalp Entry: Posizione aperta su {code} a {fmt_price(price)}"]
            }
        else:
            save_state(state)
            wait_reasons = []
            if raw_100 < Entry_Raw_100_Min:
                wait_reasons.append(f"Score {best_eval['code']} {raw_100}/100 < {Entry_Raw_100_Min}/100")
            if ai_prob < 0.50:
                wait_reasons.append(f"AI {ai_prob*100:.0f}% < 50%")
            if not timer_ok:
                wait_reasons.append(f"Timer 15m ({best_eval['scanner_timer']['triggers']}/4 trigger)")
            if not risk_ok:
                wait_reasons.append(best_risk["status_str"])

            reason_str = " | ".join(wait_reasons) if wait_reasons else "In attesa di conferma"
            return {
                "action_state": "WAITING",
                "headline": "🛡️ CAPITALE PROTETTO IN USDT (Nessun acquisto ora)",
                "detail": f"Migliore: {best_eval['code']} ({score}/10 - {raw_100}/100) -> Non apro perche': {reason_str}.",
                "pnl_pct": 0.0,
                "pnl_usdt": 0.0,
                "current_r": 0.0,
                "alerts_fired": []
            }


# ==============================================================================
# 10. GENERAZIONE WEB CONSOLE LIVE IN TEMPO REALE (index.html)
# ==============================================================================
def build_manual_action_advisor(state, best_eval, all_evals, trade_status):
    pos = state["position"]
    if pos["is_open"]:
        code = pos["coin_code"]
        ev = all_evals.get(code, best_eval)
        pnl = trade_status["pnl_pct"]
        curr_r = trade_status["current_r"]

        if ev["final_score"] <= 4 or ev["ai_prob"] < 0.44:
            return {
                "badge": "⚠️ CONSIGLIO MANUALE: VALUTA CHIUSURA ANTICIPATA",
                "color": "#f59e0b",
                "bg": "rgba(245,158,11,0.14)",
                "instruction": f"Il punteggio di {code} si sta indebolendo ({ev['final_score']}/10, AI {ev['ai_prob']*100:.1f}%). Se operi manualmente su Pionex, valuta di chiudere o stringere lo Stop-Loss."
            }
        elif pnl >= SCALP_MIN_NET_PROFIT_PCT:
            return {
                "badge": "💰 POSIZIONE IN ZONA SCALP PROFIT",
                "color": "#10b981",
                "bg": "rgba(16,185,129,0.14)",
                "instruction": f"La posizione su {code} e' in profitto netto ({pnl:+.2f}% / {curr_r:+.2f}R). Il bot incassera' automaticamente al primo segnale di inversione 15m o al raggiungimento del TP ({fmt_price(pos['tp_price'])})."
            }
        else:
            return {
                "badge": "✅ MANTIENI POSIZIONE (HOLD)",
                "color": "#38bdf8",
                "bg": "rgba(56,189,248,0.14)",
                "instruction": f"Posizione aperta su {code} (PnL netto {pnl:+.2f}%). Mantieni attiva la protezione con Stop-Loss a {fmt_price(pos['sl_price'])} e Take-Profit a {fmt_price(pos['tp_price'])}."
            }
    else:
        score = best_eval["final_score"]
        raw_100 = best_eval["raw_100"]
        code = best_eval["code"]
        rm = best_eval["risk_manager"]
        tm = best_eval["scanner_timer"]

        if score >= Entry_Score_Min and raw_100 >= Entry_Raw_100_Min and best_eval["ai_prob"] >= 0.50 and rm["passed"] and tm["triggered"]:
            return {
                "badge": f"🚀 SEGNALE ATTIVO: APRI LONG SU {code}",
                "color": "#10b981",
                "bg": "rgba(16,185,129,0.15)",
                "instruction": f"Tutti i semafori sono VERDI su {code} ({score}/10 - {raw_100}/100). Se operi manualmente su Pionex: Apri LONG Spot su {best_eval['coin']['pionex']} con {rm['position_usdt']:.2f} USDT, SL {fmt_price(best_eval['sl_price'])} e TP {fmt_price(best_eval['tp_price'])}."
            }
        elif raw_100 >= 54:
            return {
                "badge": f"👀 QUASI PRONTO SU {code} ({raw_100}/100) - ATTENDI",
                "color": "#f59e0b",
                "bg": "rgba(245,158,11,0.14)",
                "instruction": f"{code} e' vicino alla soglia di ingresso ({raw_100}/100), ma manca conferma sul Timer 15m ({tm['summary']}). Attendi il prossimo segnale prima di entrare."
            }
        else:
            return {
                "badge": "🛡️ RESTA FLAT IN USDT (NON APRIRE ORA)",
                "color": "#94a3b8",
                "bg": "rgba(148,163,184,0.12)",
                "instruction": f"Nessuna delle 10 coin offre un vantaggio sufficiente in questo momento (Migliore: {code} con {raw_100}/100). Non aprire posizioni manuali."
            }


def generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str):
    best = ranked_evals[0]
    advisor = build_manual_action_advisor(state, best, {e["code"]: e for e in ranked_evals}, trade_status)
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

    coins_payload = []
    for e in ranked_evals:
        rm = e.get("risk_manager") or skill_budget_and_risk_manager(
            INVEST_EUR, 1.08, e["price"], e["sl_price"], e["tp_price"], state, e["high_soon"]
        )
        ep = e.get("exit_plan") or skill_exit_planner(e["price"], e["sl_price"], e["tp_price"])
        coins_payload.append({
            "code": e["code"],
            "name": e["coin"]["name"],
            "pionex": e["coin"]["pionex"],
            "kraken": e["coin"]["kraken"],
            "ws": e["coin"]["ws"],
            "price": e["price"],
            "sl_price": e["sl_price"],
            "tp_price": e["tp_price"],
            "final_score": e["final_score"],
            "raw_100": e["raw_100"],
            "ai_prob": round(e["ai_prob"] * 100, 1),
            "prob_mlp": round(e["prob_mlp"] * 100, 1),
            "prob_knn": round(e["prob_knn"] * 100, 1),
            "prob_mc": round(e["prob_mc"] * 100, 1),
            "rsi_1h": round(e["rsi_1h"], 1),
            "rsi_15m": round(e["rsi_15m"], 1),
            "ob_imb": round(e["ob"]["imbalance"] * 100, 1),
            "tm_wr": round(e["tm_stats"]["win_rate"], 1),
            "tm_exp": round(e["tm_stats"]["expectancy_r"], 2),
            "timer_ok": e["scanner_timer"]["triggered"],
            "timer_triggers": e["scanner_timer"]["triggers"],
            "timer_summary": e["scanner_timer"]["summary"],
            "vwap_15m": e["scanner_timer"]["vwap_15m"],
            "risk_ok": rm["passed"],
            "risk_status": rm["status_str"],
            "rr_ratio": round(rm["rr_ratio"], 2),
            "pos_usdt": round(rm["position_usdt"], 2),
            "be_trigger": ep["be_trigger"],
            "tp1_trigger": ep["tp1_trigger"],
            "tp2_trigger": ep["tp2_trigger"]
        })

    payload = {
        "updated_at": now_str,
        "session": session_str,
        "fng_val": fng_val,
        "fng_class": fng_class,
        "advisor": advisor,
        "position": state["position"],
        "stats": state["stats"],
        "history": state.get("history", []),
        "coins": coins_payload
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pionex AI Live Trading Console v7.3</title>
<style>
  :root {
    --bg: #070b14;
    --card: #111827;
    --card-border: #1f2937;
    --text: #f8fafc;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
    --amber: #f59e0b;
    --red: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(circle at top, #131c31 0%, var(--bg) 70%);
    color: var(--text);
    padding: 14px;
    max-width: 960px;
    margin: 0 auto;
  }
  .topbar {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    gap: 10px; padding: 12px 16px; background: rgba(17,24,39,0.9);
    border: 1px solid var(--card-border); border-radius: 14px; margin-bottom: 14px;
  }
  .brand { font-size: 17px; font-weight: 800; display: flex; align-items: center; gap: 8px; }
  .live-dot {
    width: 10px; height: 10px; border-radius: 50%; background: var(--green);
    box-shadow: 0 0 10px var(--green); display: inline-block; animation: pulse 1.6s infinite;
  }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.35; } 100% { opacity: 1; } }
  .meta-bar { font-size: 12px; color: var(--muted); display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .btn-refresh {
    background: rgba(56,189,248,0.15); color: var(--accent); border: 1px solid rgba(56,189,248,0.4);
    padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer;
  }
  .advisor-box {
    border-radius: 14px; padding: 16px; margin-bottom: 14px;
    border: 1px solid var(--card-border);
  }
  .advisor-badge { font-size: 15px; font-weight: 800; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; }
  .advisor-text { font-size: 13.5px; line-height: 1.45; color: #e2e8f0; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
  @media (max-width: 720px) { .grid-2 { grid-template-columns: 1fr; } }
  .card {
    background: rgba(17, 24, 39, 0.9); border: 1px solid var(--card-border);
    border-radius: 14px; padding: 15px; box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  }
  .card-title {
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px;
    color: var(--muted); font-weight: 700; margin-bottom: 12px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .stat-row { display: flex; justify-content: space-between; font-size: 13px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
  .stat-row:last-child { border-bottom: none; }
  .val-bold { font-weight: 700; }
  .pnl-big { font-size: 24px; font-weight: 800; margin: 6px 0; }
  .progress-wrap { background: #1e293b; height: 10px; border-radius: 8px; overflow: hidden; margin: 10px 0 4px; position: relative; }
  .progress-bar { height: 100%; transition: width 0.4s ease; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; padding: 9px 8px; color: var(--muted); border-bottom: 1px solid var(--card-border); font-size: 11px; text-transform: uppercase; }
  td { padding: 9px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
  tr.clickable-row { cursor: pointer; transition: background 0.15s; }
  tr.clickable-row:hover { background: rgba(56,189,248,0.08); }
  .pill { padding: 3px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; display: inline-block; }
  .pill-green { background: rgba(16,185,129,0.18); color: #34d399; }
  .pill-amber { background: rgba(245,158,11,0.18); color: #fbbf24; }
  .pill-red { background: rgba(239,68,68,0.18); color: #f87171; }
  .flash-up { color: #34d399 !important; transition: color 0.2s; }
  .flash-dn { color: #f87171 !important; transition: color 0.2s; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <span class="live-dot" id="ws-dot"></span>
    <span>PIONEX AI FAST-SCALP CONSOLE v7.3</span>
  </div>
  <div class="meta-bar">
    <span id="ws-status">Connessione Kraken Live...</span>
    <span>|</span>
    <span id="session-label"></span>
    <button class="btn-refresh" onclick="forcePollKraken()">🔄 Aggiorna Prezzi Live</button>
  </div>
</div>

<div class="advisor-box" id="advisor-box">
  <div class="advisor-badge">
    <span id="advisor-badge-text"></span>
    <span style="font-size:11px; font-weight:600; opacity:0.85;" id="snapshot-time"></span>
  </div>
  <div class="advisor-text" id="advisor-instruction"></div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title">
      <span>📌 POSIZIONE APERTA IN TEMPO REALE</span>
      <span id="pos-status-pill" class="pill"></span>
    </div>
    <div id="open-pos-content"></div>
  </div>

  <div class="card">
    <div class="card-title">
      <span>📊 STATISTICHE & RISCHIO PORTAFOGLIO</span>
      <span id="fng-badge" class="pill pill-amber"></span>
    </div>
    <div id="portfolio-stats-content"></div>
  </div>
</div>

<div class="card" style="margin-bottom:14px;">
  <div class="card-title">
    <span>📡 SCANNER LIVE 10 CRIPTOVALUTE (CLICCA UNA RIGA PER I DETTAGLI)</span>
    <span style="color:var(--accent);">Prezzi Live via Kraken WebSocket</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Coin</th>
          <th>Prezzo Live</th>
          <th>Score</th>
          <th>AI Prob</th>
          <th>Timer 15m</th>
          <th>R:R & Risk</th>
          <th>Azione Manuale Consigliata</th>
        </tr>
      </thead>
      <tbody id="scanner-tbody"></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-bottom:14px;" id="detail-card">
  <div class="card-title">
    <span id="detail-title">🔍 PIANO OPERATIVO DETTAGLIATO</span>
    <span id="detail-pair" style="color:var(--accent);"></span>
  </div>
  <div id="detail-content"></div>
</div>

<div class="card">
  <div class="card-title">
    <span>📜 STORICO POSIZIONI CHIUSE</span>
    <span id="history-count"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Data Chiusura</th>
          <th>Coin</th>
          <th>Prezzo Entry</th>
          <th>Prezzo Exit</th>
          <th>PnL Netto</th>
          <th>Motivo Chiusura</th>
        </tr>
      </thead>
      <tbody id="history-tbody"></tbody>
    </table>
  </div>
</div>

<script>
const DATA = __CONSOLE_PAYLOAD__;
const livePrices = {};
const wsMap = {};
DATA.coins.forEach(c => {
  livePrices[c.code] = c.price;
  wsMap[c.ws] = c.code;
});
let selectedCoinCode = DATA.position.is_open ? DATA.position.coin_code : DATA.coins[0].code;

function fmtPrice(p) {
  p = Number(p || 0);
  if (p <= 0) return "$0.00";
  if (p >= 1000) return "$" + p.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  if (p >= 10) return "$" + p.toFixed(2);
  if (p >= 1) return "$" + p.toFixed(3);
  return "$" + p.toFixed(4);
}

function renderAdvisor() {
  const box = document.getElementById("advisor-box");
  box.style.background = DATA.advisor.bg;
  box.style.borderColor = DATA.advisor.color;
  const badge = document.getElementById("advisor-badge-text");
  badge.style.color = DATA.advisor.color;
  badge.textContent = DATA.advisor.badge;
  document.getElementById("snapshot-time").textContent = "Ultimo calcolo AI: " + DATA.updated_at;
  document.getElementById("advisor-instruction").textContent = DATA.advisor.instruction;
  document.getElementById("session-label").textContent = DATA.session;
  document.getElementById("fng-badge").textContent = "Fear & Greed: " + DATA.fng_val + " (" + DATA.fng_class + ")";
}

function renderOpenPosition() {
  const pos = DATA.position;
  const pill = document.getElementById("pos-status-pill");
  const container = document.getElementById("open-pos-content");

  if (!pos.is_open) {
    pill.className = "pill pill-amber";
    pill.textContent = "FLAT (100% USDT)";
    container.innerHTML = `
      <div style="padding:18px 4px; text-align:center; color:var(--muted); font-size:13.5px;">
        Nessuna posizione aperta al momento.<br>
        <span style="color:#e2e8f0; font-weight:600; display:block; margin-top:6px;">
          Il capitale e' protetto in USDT in attesa di un segnale Fast-Scalp.
        </span>
      </div>
    `;
    return;
  }

  const code = pos.coin_code;
  const cur = livePrices[code] || pos.entry_price;
  const entry = pos.entry_price;
  const sl = pos.sl_price;
  const tp = pos.tp_price;
  const oneR = pos.one_r || Math.max(entry - pos.initial_sl, entry * 0.009);

  const grossPct = ((cur - entry) / entry) * 100.0;
  const netPct = grossPct - 0.10;
  const pnlUsdt = pos.invested_usdt * (netPct / 100.0);
  const curR = oneR > 0 ? (cur - entry) / oneR : 0.0;

  pill.className = "pill " + (netPct >= 0 ? "pill-green" : "pill-red");
  pill.textContent = "LONG " + code + " (" + pos.mode + ")";

  const totalSpan = Math.max(tp - sl, 1e-8);
  const progressPct = Math.max(0, Math.min(100, ((cur - sl) / totalSpan) * 100));
  const barColor = netPct >= 0 ? "var(--green)" : "var(--red)";

  let manualPosAdvice = "✅ MANTIENI POSIZIONE (In linea con il piano)";
  if (cur <= sl) manualPosAdvice = "🛑 ATTENZIONE: Prezzo sotto lo Stop-Loss! Chiudi subito se non automatico.";
  else if (cur >= tp) manualPosAdvice = "🎯 TARGET RAGGIUNTO: Prendi profitto!";

  container.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:baseline;">
      <div>
        <div style="font-size:12px; color:var(--muted);">PnL Netto Live (${code})</div>
        <div class="pnl-big" style="color:${barColor};">
          ${netPct >= 0 ? "+" : ""}${netPct.toFixed(2)}% (${pnlUsdt >= 0 ? "+" : ""}${pnlUsdt.toFixed(2)} USDT)
        </div>
      </div>
      <div style="text-align:right;">
        <div style="font-size:12px; color:var(--muted);">Multiplo R Live</div>
        <div style="font-size:18px; font-weight:800; color:var(--accent);">${curR >= 0 ? "+" : ""}${curR.toFixed(2)}R</div>
      </div>
    </div>

    <div class="progress-wrap">
      <div class="progress-bar" style="width:${progressPct.toFixed(1)}%; background:${barColor};"></div>
    </div>
    <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-bottom:10px;">
      <span>🛑 SL: ${fmtPrice(sl)}</span>
      <span>📍 Live: <strong style="color:#fff;">${fmtPrice(cur)}</strong></span>
      <span>🎯 TP: ${fmtPrice(tp)}</span>
    </div>

    <div class="stat-row"><span>Prezzo Ingresso (Aperta il ${pos.opened_at})</span><span class="val-bold">${fmtPrice(entry)}</span></div>
    <div class="stat-row"><span>Capitale Investito / Quantita'</span><span class="val-bold">${pos.invested_usdt.toFixed(2)} USDT (${pos.amount_coin} ${code})</span></div>
    <div class="stat-row"><span>Stato Protezione Trailing</span><span class="val-bold">${pos.tp2_locked ? "🚀 +1.45R Locked" : (pos.tp1_locked ? "🔒 +1R Locked" : (pos.breakeven_locked ? "🛡️ Break-Even Netto" : "Iniziale"))}</span></div>
    <div class="stat-row"><span>Indicazione Operativa</span><span class="val-bold" style="color:var(--accent);">${manualPosAdvice}</span></div>
  `;
}

function renderPortfolioStats() {
  const s = DATA.stats;
  const wr = s.total_trades > 0 ? ((s.wins / s.total_trades) * 100).toFixed(1) + "%" : "N/A";
  const pnlColor = s.total_pnl_pct >= 0 ? "var(--green)" : "var(--red)";
  const dailyColor = (s.daily_pnl_pct || 0) >= 0 ? "var(--green)" : "var(--red)";

  document.getElementById("portfolio-stats-content").innerHTML = `
    <div class="stat-row"><span>Operazioni Concluse Totali</span><span class="val-bold">${s.total_trades} (${s.wins} Vinte / ${s.losses} Perse)</span></div>
    <div class="stat-row"><span>Win Rate Reale</span><span class="val-bold">${wr}</span></div>
    <div class="stat-row"><span>PnL Netto Cumulato</span><span class="val-bold" style="color:${pnlColor};">${s.total_pnl_pct >= 0 ? "+" : ""}${s.total_pnl_pct.toFixed(2)}% (${s.total_pnl_usdt >= 0 ? "+" : ""}${s.total_pnl_usdt.toFixed(2)} USDT)</span></div>
    <div class="stat-row"><span>PnL Giornaliero (Max Loss -6%)</span><span class="val-bold" style="color:${dailyColor};">${(s.daily_pnl_pct || 0) >= 0 ? "+" : ""}${(s.daily_pnl_pct || 0).toFixed(2)}% (${s.daily_trades || 0}/5 trade oggi)</span></div>
    <div class="stat-row"><span>Ultima Azione Bot</span><span class="val-bold" style="font-size:12px;">${s.last_action}</span></div>
  `;
}

function renderScannerTable() {
  const tbody = document.getElementById("scanner-tbody");
  tbody.innerHTML = "";

  DATA.coins.forEach(c => {
    const cur = livePrices[c.code] || c.price;
    const scoreClass = c.final_score >= 7 ? "pill-green" : (c.final_score >= 6 ? "pill-amber" : "pill-red");
    const timerBadge = c.timer_ok
      ? `<span class="pill pill-green">⚡ OK (${c.timer_triggers}/4)</span>`
      : `<span class="pill pill-amber">⏳ ${c.timer_triggers}/4</span>`;
    const riskBadge = c.risk_ok
      ? `<span class="pill pill-green">${c.rr_ratio}x PASS</span>`
      : `<span class="pill pill-red">${c.rr_ratio}x STOP</span>`;

    let manualAdvice = "🛡️ Attendi (Non aprire)";
    let adviceColor = "var(--muted)";
    if (DATA.position.is_open && DATA.position.coin_code === c.code) {
      manualAdvice = "📌 POSIZIONE ATTIVA";
      adviceColor = "var(--accent)";
    } else if (c.final_score >= 6 && c.raw_100 >= 58 && c.ai_prob >= 50 && c.timer_ok && c.risk_ok) {
      manualAdvice = "🚀 APRI LONG ORA";
      adviceColor = "var(--green)";
    } else if (c.raw_100 >= 54) {
      manualAdvice = "👀 Quasi pronto (Attendi 15m)";
      adviceColor = "var(--amber)";
    }

    const tr = document.createElement("tr");
    tr.className = "clickable-row";
    if (c.code === selectedCoinCode) tr.style.background = "rgba(56,189,248,0.12)";
    tr.onclick = () => { selectedCoinCode = c.code; renderScannerTable(); renderCoinDetail(); };

    tr.innerHTML = `
      <td><strong>${c.code}</strong> <span style="color:var(--muted); font-size:11px;">${c.name}</span></td>
      <td id="price-cell-${c.code}" class="val-bold">${fmtPrice(cur)}</td>
      <td><span class="pill ${scoreClass}">${c.final_score}/10 (${c.raw_100})</span></td>
      <td class="val-bold">${c.ai_prob}%</td>
      <td>${timerBadge}</td>
      <td>${riskBadge}</td>
      <td style="font-weight:700; color:${adviceColor};">${manualAdvice}</td>
    `;
    tbody.appendChild(tr);
  });
}

function renderCoinDetail() {
  const c = DATA.coins.find(x => x.code === selectedCoinCode) || DATA.coins[0];
  const cur = livePrices[c.code] || c.price;
  document.getElementById("detail-title").textContent = `🔍 PIANO OPERATIVO DETTAGLIATO: ${c.name.toUpperCase()} (${c.code})`;
  document.getElementById("detail-pair").textContent = `Pionex Spot: ${c.pionex}`;

  document.getElementById("detail-content").innerHTML = `
    <div class="grid-2" style="margin-bottom:0;">
      <div>
        <div class="stat-row"><span>Prezzo Live / VWAP 15m</span><span class="val-bold">${fmtPrice(cur)} / ${fmtPrice(c.vwap_15m)}</span></div>
        <div class="stat-row"><span>Budget Consigliato (con Riserva 5%)</span><span class="val-bold">${c.pos_usdt.toFixed(2)} USDT</span></div>
        <div class="stat-row"><span>Stop-Loss Iniziale</span><span class="val-bold" style="color:var(--red);">${fmtPrice(c.sl_price)}</span></div>
        <div class="stat-row"><span>Take-Profit Target (+3 Stadi R)</span><span class="val-bold" style="color:var(--green);">${fmtPrice(c.tp_price)}</span></div>
        <div class="stat-row"><span>Livelli Profit-Lock (BE / +1R / +1.45R)</span><span class="val-bold">${fmtPrice(c.be_trigger)} / ${fmtPrice(c.tp1_trigger)} / ${fmtPrice(c.tp2_trigger)}</span></div>
      </div>
      <div>
        <div class="stat-row"><span>Reti Neurali (MLP / k-NN / MonteCarlo)</span><span class="val-bold">${c.prob_mlp}% / ${c.prob_knn}% / ${c.prob_mc}%</span></div>
        <div class="stat-row"><span>Time Machine (20 pattern simili)</span><span class="val-bold">WinRate ${c.tm_wr}% (${c.tm_exp >= 0 ? "+" : ""}${c.tm_exp}R)</span></div>
        <div class="stat-row"><span>RSI 1H / RSI 15m / Orderbook L2</span><span class="val-bold">${c.rsi_1h} / ${c.rsi_15m} / ${c.ob_imb >= 0 ? "+" : ""}${c.ob_imb}%</span></div>
        <div class="stat-row"><span>Dettaglio Entry Timer 15m</span><span class="val-bold">${c.timer_summary}</span></div>
        <div class="stat-row"><span>Stato Risk Manager</span><span class="val-bold">${c.risk_status}</span></div>
      </div>
    </div>
  `;
}

function renderHistoryTable() {
  const hist = DATA.history || [];
  document.getElementById("history-count").textContent = hist.length + " operazioni registrate";
  const tbody = document.getElementById("history-tbody");
  if (hist.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--muted); padding:16px;">Nessuna posizione chiusa nello storico recente.</td></tr>`;
    return;
  }
  tbody.innerHTML = hist.map(h => {
    const cls = h.pnl_pct >= 0 ? "pill-green" : "pill-red";
    const sign = h.pnl_pct >= 0 ? "+" : "";
    return `
      <tr>
        <td>${h.closed_at}</td>
        <td><strong>${h.coin}</strong> <span style="font-size:10px; color:var(--muted);">(${h.mode || "SIM"})</span></td>
        <td>${fmtPrice(h.entry)}</td>
        <td>${fmtPrice(h.exit)}</td>
        <td><span class="pill ${cls}">${sign}${Number(h.pnl_pct).toFixed(2)}% (${sign}${Number(h.pnl_usdt || 0).toFixed(2)} USDT)</span></td>
        <td>${h.reason}</td>
      </tr>
    `;
  }).join("");
}

function updateLivePrice(code, newPrice) {
  if (!newPrice || newPrice <= 0) return;
  const oldPrice = livePrices[code] || newPrice;
  livePrices[code] = newPrice;

  const cell = document.getElementById("price-cell-" + code);
  if (cell) {
    cell.textContent = fmtPrice(newPrice);
    if (newPrice > oldPrice) {
      cell.classList.add("flash-up");
      setTimeout(() => cell.classList.remove("flash-up"), 350);
    } else if (newPrice < oldPrice) {
      cell.classList.add("flash-dn");
      setTimeout(() => cell.classList.remove("flash-dn"), 350);
    }
  }
  if (DATA.position.is_open && DATA.position.coin_code === code) {
    renderOpenPosition();
  }
  if (selectedCoinCode === code) {
    renderCoinDetail();
  }
}

function connectKrakenWebSocket() {
  try {
    const ws = new WebSocket("wss://ws.kraken.com");
    ws.onopen = () => {
      document.getElementById("ws-status").textContent = "🟢 Streaming Live Kraken Attivo";
      ws.send(JSON.stringify({
        event: "subscribe",
        pair: DATA.coins.map(c => c.ws),
        subscription: { name: "ticker" }
      }));
    };
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (Array.isArray(msg) && msg.length >= 4 && msg[1] && msg[1].c) {
        const pairName = msg[3];
        const price = parseFloat(msg[1].c[0]);
        const code = wsMap[pairName];
        if (code && price > 0) updateLivePrice(code, price);
      }
    };
    ws.onerror = () => {
      document.getElementById("ws-status").textContent = "🟡 Fallback Polling REST Live";
    };
    ws.onclose = () => {
      setTimeout(connectKrakenWebSocket, 5000);
    };
  } catch (e) {
    console.warn("WS error:", e);
  }
}

async function forcePollKraken() {
  try {
    const pairs = DATA.coins.map(c => c.kraken).join(",");
    const res = await fetch("https://api.kraken.com/0/public/Ticker?pair=" + pairs);
    const j = await res.json();
    if (j && j.result) {
      Object.keys(j.result).forEach(k => {
        const p = parseFloat(j.result[k].c[0]);
        DATA.coins.forEach(c => {
          if (k.includes(c.code) || (c.code === "BTC" && k.includes("XBT")) || (c.code === "DOGE" && k.includes("XDG"))) {
            updateLivePrice(c.code, p);
          }
        });
      });
    }
  } catch (e) {
    console.warn("Poll error:", e);
  }
}

renderAdvisor();
renderOpenPosition();
renderPortfolioStats();
renderScannerTable();
renderCoinDetail();
renderHistoryTable();
connectKrakenWebSocket();
setInterval(forcePollKraken, 8000);
</script>
</body>
</html>"""

    html = html.replace("__CONSOLE_PAYLOAD__", payload_json)
    try:
        with open(WEBAPP_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    except Exception as e:
        print(f"[ERROR] Scrittura {WEBAPP_FILE} fallita: {e}")
        return False


# ==============================================================================
# 11. FORMATTAZIONE E INVIO TELEGRAM (SOLO SU APERTURA / CHIUSURA / TEST MANUALE)
# ==============================================================================
def build_telegram_report(ranked_evals, state, trade_status, session_str):
    best = ranked_evals[0]
    advisor = build_manual_action_advisor(state, best, {e["code"]: e for e in ranked_evals}, trade_status)
    pos = state["position"]
    stats = state["stats"]

    rank_lines = []
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for idx, ev in enumerate(ranked_evals[:5]):
        m = medals[idx]
        t_icon = "⚡" if ev["scanner_timer"]["triggered"] else "⏳"
        rank_lines.append(
            f"{m} <b>{ev['code']}</b> ({fmt_price(ev['price'])}): <b>{ev['final_score']}/10</b> ({ev['raw_100']}/100) | AI {ev['ai_prob']*100:.0f}% | 15m {t_icon}"
        )
    ranking_block = "\n".join(rank_lines)

    if pos["is_open"]:
        open_code = pos["coin_code"]
        pos_block = (
            f"📌 <b>POSIZIONE APERTA SU {open_code} ({pos['mode']}):</b>\n"
            f"• Entry: <b>{fmt_price(pos['entry_price'])}</b> | Investito: <b>{pos['invested_usdt']:.2f} USDT</b>\n"
            f"• Stop-Loss Attivo: <b>{fmt_price(pos['sl_price'])}</b> | Take-Profit: <b>{fmt_price(pos['tp_price'])}</b>\n"
            f"• PnL Netto Live: <b>{trade_status['pnl_pct']:+.2f}% ({trade_status['pnl_usdt']:+.2f} USDT / {trade_status['current_r']:+.2f}R)</b>"
        )
    else:
        pos_block = "📌 <b>POSIZIONE APERTA:</b> Nessuna (100% Liquidita' in USDT)"

    wr = (stats["wins"] / stats["total_trades"] * 100.0) if stats["total_trades"] > 0 else 0.0
    hist = state.get("history", [])
    if hist:
        last_h = hist[0]
        closed_block = (
            f"📜 <b>ULTIMA POSIZIONE CHIUSA:</b> {last_h['coin']} il {last_h['closed_at']} -> "
            f"<b>{last_h['pnl_pct']:+.2f}% ({last_h.get('pnl_usdt', 0.0):+.2f} USDT)</b> ({last_h['reason']})"
        )
    else:
        closed_block = "📜 <b>ULTIMA POSIZIONE CHIUSA:</b> Nessuna operazione chiusa di recente"

    alerts_block = ""
    if trade_status.get("alerts_fired"):
        alerts_block = "\n" + "\n".join(trade_status["alerts_fired"]) + "\n"

    msg = f"""🤖 <b>PIONEX FAST-SCALP AI v7.3</b>
🕒 <i>{session_str}</i>
━━━━━━━━━━━━━━━━━━━━
<b>{trade_status['headline']}</b>
{trade_status['detail']}{alerts_block}

🧭 <b>COSA FARE MANUALMENTE ADESSO:</b>
<b>{advisor['badge']}</b>
<i>{advisor['instruction']}</i>

{pos_block}
{closed_block}
• Bilancio Storico: <b>{stats['total_trades']} Trade</b> (Win Rate <b>{wr:.0f}%</b> | PnL Netto: <b>{stats['total_pnl_pct']:+.2f}% / {stats['total_pnl_usdt']:+.2f} USDT</b>)

🏆 <b>TOP 5 CLASSIFICA FAST-SCALP (SU 10 COIN):</b>
{ranking_block}
━━━━━━━━━━━━━━━━━━━━
📲 <i>Apri il file <b>Console_Live_AI_Bot.html</b> qui sotto per lo streaming live dei prezzi!</i>"""
    return msg


def sanitize_telegram_html(text):
    text = text.replace("&", "&amp;")
    tags = ["b", "/b", "i", "/i", "code", "/code", "pre", "/pre"]
    for t in tags:
        text = re.sub(fr"&lt;{t}&gt;", f"<{t}>", text, flags=re.IGNORECASE)
    return text


def send_telegram_report_and_console(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Credenziali Telegram mancanti nei Secrets.")
        return

    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url_msg,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": False
            },
            timeout=HTTP_TIMEOUT
        )
        if r.status_code != 200:
            plain = re.sub(r"<[^>]+>", "", msg)
            requests.post(
                url_msg,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": plain, "disable_notification": False},
                timeout=HTTP_TIMEOUT
            )
    except Exception as e:
        print(f"[ERROR] Invio messaggio Telegram fallito: {e}")

    if os.path.exists(WEBAPP_FILE):
        url_doc = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        try:
            with open(WEBAPP_FILE, "rb") as f:
                requests.post(
                    url_doc,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": "🖥️ Console Web Interattiva in Tempo Reale (Tocca per aprirla: prezzi live, posizioni aperte/chiuse e guida manuale)",
                        "disable_notification": False
                    },
                    files={"document": ("Console_Live_AI_Bot.html", f, "text/html")},
                    timeout=HTTP_TIMEOUT
                )
        except Exception as e:
            print(f"[ERROR] Invio Console HTML fallito: {e}")


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
def main():
    print("[INFO] Avvio Pionex Fast-Scalp AI Engine v7.3 (3-4 Trade/Day + Notifiche Solo su Apertura/Chiusura)...")
    state = load_state()

    eur_usd = fetch_eur_usd_rate()
    fng_val, fng_class = fetch_fear_and_greed()
    macro_events = fetch_macro_calendar()
    futures_map = fetch_all_kraken_futures()
    session_str = skill_nyse_bell_and_session()

    all_evals = scan_all_coins(futures_map, fng_val, macro_events)
    if not all_evals:
        print("[ERROR] Nessuna coin analizzata con successo.")
        return

    ranked_evals = sorted(
        all_evals.values(),
        key=lambda x: (x["raw_100"], x["scanner_timer"]["triggered"], x["ai_prob"]),
        reverse=True
    )
    best_eval = ranked_evals[0]

    for ev in ranked_evals:
        ev["risk_manager"] = skill_budget_and_risk_manager(
            INVEST_EUR, eur_usd, ev["price"], ev["sl_price"], ev["tp_price"], state, ev["high_soon"]
        )
        ev["exit_plan"] = skill_exit_planner(ev["price"], ev["sl_price"], ev["tp_price"])

    trade_status = manage_autonomous_trading(state, best_eval, all_evals, eur_usd)
    generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str)

    action_state = trade_status["action_state"]
    is_manual_run = (GITHUB_EVENT_NAME == "workflow_dispatch")

    # REGOLA RICHIESTA DALL'UTENTE:
    # Invia messaggio su Telegram SOLO quando apre o chiude una posizione,
    # oppure se l'utente ha premuto manualmente "Run workflow" per fare un controllo.
    if action_state in ("OPENED_NOW", "CLOSED_NOW") or is_manual_run:
        print(f"[INFO] Invio notifica Telegram (Motivo: action_state={action_state}, manual_run={is_manual_run})...")
        msg = build_telegram_report(ranked_evals, state, trade_status, session_str)
        send_telegram_report_and_console(msg)
    else:
        print(f"[INFO] Giro automatico completato (Stato: {action_state} | Migliore: {best_eval['code']} {best_eval['raw_100']}/100). Nessuna apertura/chiusura -> Messaggio Telegram non inviato.")

    print("[INFO] Esecuzione completata con successo.")


if __name__ == "__main__":
    main()
