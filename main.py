#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Coin AI Quant Bot v2 (Kraken dati -> Pionex esecuzione -> Telegram)
Novità v2: sizing a rischio frazionario, trailing Chandelier ATR, validazione walk-forward
dell'AI con shrinkage, Time Machine con block-bootstrap e costi reali, rate-limit/retry API,
stato atomico, esecuzione ordini a singolo canale con gestione errori, monitor di emergenza.
NON è consulenza finanziaria: default = PAPER TRADING.
"""
import os
import re
import json
import math
import time
import hmac
import hashlib
import html
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def env_float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return float(default)


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

INVEST_EUR = env_float("INVEST_EUR", 50)
MIN_ORDER_USDT = 5.0
MIN_POSITION_USDT = 10.0         # sotto questa size il trade non vale fee/slippage
BTC_PAIR = "XBTUSD"

# Risk & Budget Manager
MAX_LOSS_PCT_PER_TRADE = 3.6     # % del capitale perso se scatta lo stop (sizing ADATTIVO)
DAILY_MAX_LOSS_PCT = 6.0
MAX_TRADES_PER_DAY = 3
MIN_REWARD_RISK_R = 1.35
CASH_BUFFER_PCT = 5.0
KILL_SWITCH_DRAWDOWN_PCT = env_float("KILL_SWITCH_DD", 12.0)   # DD cumulato storico -> stop nuove aperture
FEE_ROUNDTRIP_PCT = 0.10         # Pionex taker 0.05% x2
SLIPPAGE_PCT = 0.06              # stima slippage round-trip market order
COST_PCT = FEE_ROUNDTRIP_PCT + SLIPPAGE_PCT

# Trailing Chandelier
CHANDELIER_ATR_MULT = 2.8

COIN_UNIVERSE = [
    {"coin": "SOL",  "name": "Solana",    "kraken": "SOLUSD",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT"},
    {"coin": "BTC",  "name": "Bitcoin",   "kraken": "XBTUSD",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT"},
    {"coin": "ETH",  "name": "Ethereum",  "kraken": "ETHUSD",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT"},
    {"coin": "XRP",  "name": "Ripple",    "kraken": "XRPUSD",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT"},
    {"coin": "SUI",  "name": "Sui",       "kraken": "SUIUSD",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT"},
    {"coin": "DOGE", "name": "Dogecoin",  "kraken": "XDGUSD",  "futures": "PF_XDGUSD",  "pionex": "DOGE_USDT"},  # fix: su Kraken Futures è XDG
    {"coin": "ADA",  "name": "Cardano",   "kraken": "ADAUSD",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT"},
    {"coin": "AVAX", "name": "Avalanche", "kraken": "AVAXUSD", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT"},
    {"coin": "LINK", "name": "Chainlink", "kraken": "LINKUSD", "futures": "PF_LINKUSD", "pionex": "LINK_USDT"},
    {"coin": "DOT",  "name": "Polkadot",  "kraken": "DOTUSD",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT"},
]
COIN_BY_SYMBOL = {c["coin"]: c for c in COIN_UNIVERSE}

NYSE_HOLIDAYS_2026_2027 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

AUTO_TRADE_ENABLED = (os.getenv("AUTO_TRADE_ENABLED") or "false").strip().lower() == "true"
PIONEX_WEBHOOK_URL = os.getenv("PIONEX_WEBHOOK_URL") or ""
PIONEX_SIGNAL_TOKEN = os.getenv("PIONEX_SIGNAL_TOKEN") or ""
PIONEX_API_KEY = os.getenv("PIONEX_API_KEY") or ""
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET") or ""
STATE_FILE = os.getenv("STATE_FILE") or "bot_state.json"
DASHBOARD_EVERY_HOURS = env_float("DASHBOARD_EVERY_HOURS", 6)   # evita spam del file HTML su Telegram

TARGET_STEP_PCT = 0.32
PIONEX_ROUNDTRIP_FEE_PCT = FEE_ROUNDTRIP_PCT
MIN_NET_STEP_PCT = 0.15

NEWS_BLOCK_BEFORE_MIN = 60
NEWS_COOLDOWN_MIN = 30
NEWS_SURPRISE_COOLDOWN_MIN = 90
NEWS_LOOKAHEAD_HOURS = 12
NEWS_MAX_15M_RANGE_PCT = 1.25
NEWS_MAX_15M_MOVE_PCT = 1.00

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
]

HIGH_KEYWORDS = [
    "CPI", "Core CPI", "PPI", "Core PPI", "Core PCE", "PCE Price",
    "Non-Farm", "Nonfarm", "Unemployment Rate", "FOMC", "Federal Funds Rate",
    "Fed Chair", "Powell", "Interest Rate Decision", "GDP", "Retail Sales",
    "ISM Manufacturing", "ISM Services", "JOLTS", "ADP Non-Farm", "Jackson Hole",
]
MEDIUM_KEYWORDS = [
    "Unemployment Claims", "Initial Jobless", "Consumer Confidence",
    "Prelim UoM", "Revised UoM", "Durable Goods", "Empire State",
    "Philly Fed", "Flash Manufacturing PMI", "Flash Services PMI",
]
EVENT_TRANSLATIONS = [
    ("Non-Farm Employment Change", "NFP Nuovi Lavori USA"),
    ("Nonfarm Payrolls", "NFP Nuovi Lavori USA"),
    ("Unemployment Rate", "Tasso Disoccupazione USA"),
    ("Unemployment Claims", "Richieste Sussidi Disoccupazione"),
    ("Initial Jobless Claims", "Richieste Sussidi Disoccupazione"),
    ("Average Hourly Earnings", "Salario Orario Medio USA"),
    ("Core PCE Price Index", "Inflazione Core PCE (Fed)"),
    ("PCE Price Index", "Inflazione PCE"),
    ("Core CPI", "Inflazione Core CPI"),
    ("CPI", "Inflazione CPI USA"),
    ("Core PPI", "Inflazione Produttori Core PPI"),
    ("PPI", "Inflazione Produttori PPI"),
    ("Federal Funds Rate", "Decisione Tassi Fed"),
    ("FOMC Statement", "Comunicato FOMC (Fed)"),
    ("FOMC Press Conference", "Conferenza Stampa Fed"),
    ("FOMC Meeting Minutes", "Verbali Riunione Fed (FOMC)"),
    ("Fed Chair Powell Speaks", "Discorso Powell (Fed)"),
    ("Powell Speaks", "Discorso Powell (Fed)"),
    ("Advance GDP", "PIL USA (Preliminare)"),
    ("Prelim GDP", "PIL USA (Revisione)"),
    ("Final GDP", "PIL USA (Finale)"),
    ("GDP", "PIL USA"),
    ("Core Retail Sales", "Vendite al Dettaglio Core"),
    ("Retail Sales", "Vendite al Dettaglio USA"),
    ("ISM Manufacturing PMI", "PMI Manifatturiero ISM"),
    ("ISM Services PMI", "PMI Servizi ISM"),
    ("JOLTS Job Openings", "Nuove Offerte Lavoro (JOLTS)"),
    ("ADP Non-Farm Employment Change", "Occupazione Privata ADP"),
    ("CB Consumer Confidence", "Fiducia dei Consumatori"),
    ("Prelim UoM Consumer Sentiment", "Fiducia Consumatori Michigan"),
    ("Flash Manufacturing PMI", "PMI Manifatturiero Flash"),
    ("Flash Services PMI", "PMI Servizi Flash"),
]

# ==============================================================================
# HTTP ROBUSTO: retry/backoff (solo GET), rate-limit Kraken, cache breve
# ==============================================================================

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; QuantBot/2.0)"})
_adapter = HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),   # i POST (ordini) NON vengono mai ritentati automaticamente
))
SESSION.mount("https://", _adapter)

KRAKEN_MIN_INTERVAL = 0.25
_K_LOCK = threading.Lock()
_K_LAST = [0.0]
_OHLC_CACHE = {}
_CACHE_LOCK = threading.Lock()


def kraken_get(path, params):
    with _K_LOCK:
        wait = KRAKEN_MIN_INTERVAL - (time.time() - _K_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _K_LAST[0] = time.time()
    r = SESSION.get("https://api.kraken.com" + path, params=params, timeout=18)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Kraken {path}: {j['error']}")
    return j["result"]


def fetch_ohlc(pair, interval=60):
    key = (pair, interval)
    with _CACHE_LOCK:
        hit = _OHLC_CACHE.get(key)
        if hit and time.time() - hit[0] < 90:
            return hit[1]
    res = kraken_get("/0/public/OHLC", {"pair": pair, "interval": interval})
    k = [x for x in res.keys() if x != "last"][0]
    rows = res[k]
    with _CACHE_LOCK:
        _OHLC_CACHE[key] = (time.time(), rows)
    return rows


def fetch_last_price(pair):
    res = kraken_get("/0/public/Ticker", {"pair": pair})
    return float(list(res.values())[0]["c"][0])


# ==============================================================================
# FORMATTAZIONE PREZZI
# ==============================================================================

def price_decimals(val):
    v = abs(float(val or 0.0))
    if v >= 10.0:
        return 2
    if v >= 1.0:
        return 3
    if v >= 0.05:
        return 4
    return 5


def round_p(val):
    return round(float(val), price_decimals(val))


def fmt_p(val):
    return f"{float(val):.{price_decimals(val)}f}"


# ==============================================================================
# STATO (scrittura atomica) ED ESECUZIONE ORDINI (UN SOLO CANALE)
# ==============================================================================

def default_state():
    return {
        "active": False, "coin": "SOL", "pionex_symbol": "SOL_USDT",
        "entry_price": 0.0, "highest_price": 0.0, "lower": 0.0, "upper": 0.0,
        "stop": 0.0, "initial_stop": 0.0, "one_r_usd": 0.0, "grids": 0,
        "qty_coin": 0.0, "notional_usdt": 0.0, "atr_abs": 0.0, "last_price": 0.0,
        "trailing_locked": False, "alert_1r_sent": False, "alert_2r_sent": False,
        "opened_at": "", "opened_date": "", "last_action": "Nessuna azione",
        "last_dashboard_ts": 0.0, "pending_close_fail": 0, "history": [],
    }


def load_bot_state():
    st = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            st.update(data)
            if "qty_sol" in data and not data.get("qty_coin"):
                st["qty_coin"] = data["qty_sol"]
        except Exception as e:
            print("Stato corrotto, backup e reset:", e)
            try:
                os.replace(STATE_FILE, STATE_FILE + ".corrupt")
            except Exception:
                pass
    return st


def save_bot_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)   # atomico: niente file a metà se il job viene interrotto
    except Exception as e:
        print("Errore salvataggio stato:", e)


def is_live():
    return AUTO_TRADE_ENABLED and bool((PIONEX_API_KEY and PIONEX_API_SECRET) or PIONEX_WEBHOOK_URL)


def trigger_pionex_webhook(action, pionex_symbol, price, qty_coin):
    payload = {
        "token": PIONEX_SIGNAL_TOKEN,
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": pionex_symbol,
        "action": action.lower(),
        "price": fmt_p(price),
        "contracts": str(round(qty_coin, 6)),
        "position_size": str(round(qty_coin, 6)) if action.lower() == "buy" else "0",
    }
    try:
        r = SESSION.post(PIONEX_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code == 200:
            return True, f"Segnale Webhook Pionex ({pionex_symbol}) inviato ✅"
        return False, f"Errore Webhook Pionex ({r.status_code})"
    except Exception as e:
        return False, f"Eccezione Webhook: {e}"


def pionex_spot_market_order(side, pionex_symbol, amount_usdt=None, size_coin=None):
    base_url, path = "https://api.pionex.com", "/api/v1/trade/order"
    ts = str(int(time.time() * 1000))
    query = f"timestamp={ts}"
    body = {"symbol": pionex_symbol, "side": side.upper(), "type": "MARKET",
            "clientOrderId": f"qb{ts}{side[0].lower()}"}   # idempotenza lato exchange
    if side.upper() == "BUY" and amount_usdt:
        body["amount"] = str(round(amount_usdt, 2))
    elif side.upper() == "SELL" and size_coin:
        body["size"] = str(math.floor(size_coin * 1e6) / 1e6)
    else:
        return False, "Parametri ordine mancanti"
    body_str = json.dumps(body, separators=(",", ":"))
    sig = hmac.new(PIONEX_API_SECRET.encode(), f"POST{path}?{query}{body_str}".encode(), hashlib.sha256).hexdigest()
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": sig, "Content-Type": "application/json"}
    try:
        r = SESSION.post(f"{base_url}{path}?{query}", headers=headers, data=body_str, timeout=15)
        res = r.json()
        if res.get("result"):
            return True, f"Ordine API Pionex {side.upper()} {pionex_symbol} eseguito ✅"
        return False, f"Errore API Pionex: {res.get('message', res.get('code', 'Sconosciuto'))}"
    except Exception as e:
        return False, f"Eccezione API Pionex: {e}"


def execute_order(side, symbol, price, qty_coin, amount_usdt=None):
    """Un SOLO canale (prima API, poi webhook): niente doppia esecuzione. Ritorna (ok, nota)."""
    if not is_live():
        return True, "Paper Trading (Simulazione)"
    if PIONEX_API_KEY and PIONEX_API_SECRET:
        if side.upper() == "BUY":
            return pionex_spot_market_order("BUY", symbol, amount_usdt=amount_usdt)
        return pionex_spot_market_order("SELL", symbol, size_coin=qty_coin * 0.998)  # -0.2%: fee prelevate in coin
    return trigger_pionex_webhook(side, symbol, price, qty_coin)


# ==============================================================================
# CALENDARIO MACRO USA & NYSE
# ==============================================================================

def translate_title(title):
    for eng, ita in EVENT_TRANSLATIONS:
        if eng.lower() in title.lower():
            return f"{ita} ({title})"
    return title


def parse_num(val):
    if not val:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(val))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def classify_event_impact(title, raw_impact):
    impact = (raw_impact or "").strip().lower()
    if impact in ("high", "red", "3"):
        return "High"
    if impact in ("medium", "orange", "2"):
        return "Medium"
    if impact in ("low", "yellow", "1"):
        return "Low"
    t_low = title.lower()
    if any(kw.lower() in t_low for kw in HIGH_KEYWORDS):
        return "High"
    if any(kw.lower() in t_low for kw in MEDIUM_KEYWORDS):
        return "Medium"
    return "Low"


def parse_event_datetime(date_str, time_str=""):
    s = (date_str or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    clean_time = (time_str or "").strip().lower()
    if not clean_time or "all day" in clean_time or "tentative" in clean_time:
        return None
    combined = f"{s} {clean_time.upper()}"
    for fmt in ("%m-%d-%Y %I:%M%p", "%Y-%m-%d %I:%M%p", "%b %d, %Y %I:%M%p"):
        try:
            naive = datetime.strptime(combined, fmt)
            return naive.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def fetch_raw_calendar():
    last_err = None
    for url in FF_URLS:
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code == 200 and r.text.strip().startswith("["):
                return r.json(), None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    try:
        r = SESSION.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=15)
        if r.status_code == 200 and "<weeklyevents" in r.text:
            root = ET.fromstring(r.text)
            items = []
            for ev in root.findall(".//event"):
                items.append({k: (ev.findtext(k) or "").strip() for k in
                              ("title", "country", "date", "time", "impact", "forecast", "previous", "actual")})
            return items, None
    except Exception as e:
        last_err = str(e)
    return None, last_err or "Calendario non raggiungibile"


def is_us_market_open_window(now_utc):
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    if ny.weekday() >= 5 or ny.strftime("%Y-%m-%d") in NYSE_HOLIDAYS_2026_2027:
        return False
    m = ny.hour * 60 + ny.minute
    return (9 * 60 + 20) <= m <= (10 * 60 + 5)


def empty_news(now_rome, err=None, us_open=False):
    return {"status": "NO_DATA", "events": [], "imminent": [], "recent": [], "today_events": [],
            "next_event": None, "surprise_detected": False, "surprise_details": [],
            "us_open_warning": us_open, "now_rome": now_rome, "error": err}


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    rome_tz = ZoneInfo("Europe/Rome")
    now_rome = now_utc.astimezone(rome_tz)
    raw_items, err = fetch_raw_calendar()
    us_open_warning = is_us_market_open_window(now_utc)
    if raw_items is None:
        return empty_news(now_rome, err, us_open_warning)

    relevant, imminent, recent, today_events = [], [], [], []
    surprise_detected, surprise_details = False, []

    for item in raw_items:
        if (item.get("country") or "").strip().upper() != "USD":
            continue
        title = (item.get("title") or "").strip()
        impact = classify_event_impact(title, item.get("impact"))
        if impact not in ("High", "Medium"):
            continue
        ev_utc = parse_event_datetime(item.get("date"), item.get("time"))
        if ev_utc is None:
            continue
        ev_rome = ev_utc.astimezone(rome_tz)
        diff_min = (ev_utc - now_utc).total_seconds() / 60.0

        actual_str = str(item.get("actual") or "").strip()
        forecast_str = str(item.get("forecast") or "").strip()
        previous_str = str(item.get("previous") or "").strip()
        act_val, for_val, prev_val = parse_num(actual_str), parse_num(forecast_str), parse_num(previous_str)

        is_surprise, surprise_txt = False, ""
        if act_val is not None and for_val is not None:
            denom = abs(for_val) if abs(for_val) > 0.05 else 1.0
            abs_diff = abs(act_val - for_val)
            if (abs_diff / denom >= 0.10 and abs_diff >= 0.1) or abs_diff >= 0.25:
                is_surprise, surprise_txt = True, f"Attuale {actual_str} vs Previsto {forecast_str}"
        elif act_val is not None and prev_val is not None and for_val is None:
            denom = abs(prev_val) if abs(prev_val) > 0.05 else 1.0
            if abs(act_val - prev_val) / denom >= 0.15:
                is_surprise, surprise_txt = True, f"Attuale {actual_str} vs Precedente {previous_str}"

        ev = {"title": translate_title(title), "raw_title": title, "impact": impact,
              "dt_utc": ev_utc, "dt_rome": ev_rome, "time_str": ev_rome.strftime("%H:%M"),
              "date_str": ev_rome.strftime("%d/%m"), "diff_min": diff_min,
              "actual": actual_str, "forecast": forecast_str, "previous": previous_str,
              "is_surprise": is_surprise, "surprise_txt": surprise_txt}

        if ev_rome.date() == now_rome.date():
            today_events.append(ev)
        if -180 <= diff_min <= NEWS_LOOKAHEAD_HOURS * 60:
            relevant.append(ev)
        if 0 <= diff_min <= (NEWS_BLOCK_BEFORE_MIN if impact == "High" else 30):
            imminent.append(ev)
        cooldown = NEWS_SURPRISE_COOLDOWN_MIN if (impact == "High" and is_surprise) else NEWS_COOLDOWN_MIN
        if -cooldown <= diff_min < 0:
            recent.append(ev)
            if is_surprise and impact == "High":
                surprise_detected = True
                surprise_details.append(f"{ev['title']}: {surprise_txt}")

    relevant.sort(key=lambda x: x["dt_utc"])
    today_events.sort(key=lambda x: x["dt_utc"])
    future = [e for e in relevant if e["diff_min"] >= 0]
    status = "IMMINENT" if imminent else ("COOLDOWN" if recent else "CLEAR")
    return {"status": status, "events": relevant, "imminent": imminent, "recent": recent,
            "today_events": today_events, "next_event": future[0] if future else None,
            "surprise_detected": surprise_detected, "surprise_details": surprise_details,
            "us_open_warning": us_open_warning, "now_rome": now_rome, "error": None}


def check_market_news_reaction(candles_5m):
    if not candles_5m or len(candles_5m) < 30:
        return {"calm": True, "range_15m_pct": 0.4, "move_15m_pct": 0.3, "vol_spike": False}
    r3 = candles_5m[-4:-1]          # ultime 3 candele CHIUSE (l'ultima di Kraken è in formazione)
    highs = [float(c[2]) for c in r3]
    lows = [float(c[3]) for c in r3]
    opens = [float(c[1]) for c in r3]
    last_close = float(r3[-1][4])
    range_pct = (max(highs) - min(lows)) / last_close * 100.0
    move_pct = abs(last_close - opens[0]) / opens[0] * 100.0
    vols = [float(c[6]) for c in candles_5m[-28:-4]]
    avg_vol = sum(vols) / len(vols) if vols else 1.0
    recent_vol = sum(float(c[6]) for c in r3) / 3.0
    vol_spike = recent_vol > avg_vol * 2.3
    calm = range_pct <= NEWS_MAX_15M_RANGE_PCT and move_pct <= NEWS_MAX_15M_MOVE_PCT and not vol_spike
    return {"calm": calm, "range_15m_pct": range_pct, "move_15m_pct": move_pct, "vol_spike": vol_spike}


# ==============================================================================
# INDICATORI
# ==============================================================================

def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def sma(values, period):
    if len(values) < period:
        return sum(values) / max(len(values), 1)
    return sum(values[-period:]) / period


def stdev(values, period):
    if len(values) < period or period <= 1:
        return 0.0
    sub = values[-period:]
    m = sum(sub) / period
    return math.sqrt(sum((x - m) ** 2 for x in sub) / period)


def rsi_series(closes, n=14):
    if len(closes) <= n:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    first = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    out = [50.0] * n + [first]
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        out.append(100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l))
    return out


def stoch_rsi(closes, rsi_len=14, stoch_len=14):
    rsis = rsi_series(closes, rsi_len)
    if len(rsis) < stoch_len:
        return 50.0
    w = rsis[-stoch_len:]
    lo, hi = min(w), max(w)
    return 50.0 if hi - lo < 1e-9 else (rsis[-1] - lo) / (hi - lo) * 100.0


def macd_hist_series(closes, fast=12, slow=26, sig=9):
    macd = [f - s for f, s in zip(ema_series(closes, fast), ema_series(closes, slow))]
    return [m - s for m, s in zip(macd, ema_series(macd, sig))]


def bollinger(closes, n=20, mult=2.0):
    mid, sd = sma(closes, n), stdev(closes, n)
    up, lo = mid + mult * sd, mid - mult * sd
    width_pct = (up - lo) / mid * 100.0 if mid else 0.0
    pct_b = (closes[-1] - lo) / (up - lo) * 100.0 if (up - lo) > 0 else 50.0
    return lo, mid, up, width_pct, pct_b


def atr_series(highs, lows, closes, n=14):
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    if len(trs) < n:
        return trs or [0.0]
    cur = sum(trs[:n]) / n
    out = [cur]
    for tr in trs[n:]:
        cur = (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def adx_di(highs, lows, closes, n=14):
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr, pdm, mdm = sum(trs[:n]), sum(plus_dm[:n]), sum(minus_dm[:n])
    dx_list, pdi, mdi = [], 0.0, 0.0
    for i in range(n, len(trs)):
        atr = atr - atr / n + trs[i]
        pdm = pdm - pdm / n + plus_dm[i]
        mdm = mdm - mdm / n + minus_dm[i]
        pdi = 100.0 * pdm / atr if atr else 0.0
        mdi = 100.0 * mdm / atr if atr else 0.0
        d = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / d if d else 0.0)
    if len(dx_list) < n:
        return (sum(dx_list) / max(len(dx_list), 1)), pdi, mdi
    adx = sum(dx_list[:n]) / n
    for dx in dx_list[n:]:
        adx = (adx * (n - 1) + dx) / n
    return adx, pdi, mdi


def hurst_exponent(closes, max_lag=24):
    if len(closes) < 120:
        return 0.50
    log_p = [math.log(max(c, 1e-8)) for c in closes[-140:]]
    xs, ys = [], []
    for lag in (2, 4, 8, 12, 16, 24):
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        if len(diffs) < 2:
            continue
        m = sum(diffs) / len(diffs)
        sd = math.sqrt(sum((d - m) ** 2 for d in diffs) / len(diffs))
        if sd > 1e-12:
            xs.append(math.log(lag))
            ys.append(math.log(sd))
    if len(xs) < 3:
        return 0.50
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    h = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den > 1e-12 else 0.50
    return max(0.20, min(0.80, h))


def variance_ratio_mr(closes, lag=6):
    if len(closes) < 80:
        return 1.0
    r1 = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rk = [math.log(closes[i] / closes[i - lag]) for i in range(lag, len(closes))]
    m1, mk = sum(r1) / len(r1), sum(rk) / len(rk)
    v1 = sum((r - m1) ** 2 for r in r1) / max(len(r1) - 1, 1)
    vk = sum((r - mk) ** 2 for r in rk) / max(len(rk) - 1, 1)
    return 1.0 if v1 <= 1e-12 else vk / (lag * v1)


def vwap_rolling(highs, lows, closes, vols, period=24):
    pv = v_sum = 0.0
    for h, l, c, v in zip(highs[-period:], lows[-period:], closes[-period:], vols[-period:]):
        pv += (h + l + c) / 3.0 * v
        v_sum += v
    return pv / v_sum if v_sum > 0 else closes[-1]


def efficiency_ratio(closes, n=20):
    """Kaufman ER: ~0 = rumore/range (buono per griglia), ~1 = trend direzionale pulito."""
    if len(closes) <= n:
        return 0.5
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes)))
    return abs(closes[-1] - closes[-1 - n]) / path if path > 0 else 0.0


# ==============================================================================
# SKILL: CHART SCANNER + ENTRY TIMER
# ==============================================================================

def run_chart_scanner_and_entry_timer(opens_1h, highs_1h, lows_1h, closes_1h, vols_1h,
                                      opens_15m, highs_15m, lows_15m, closes_15m, vols_15m,
                                      ema20_1h, bb_pct_b, pos_in_channel):
    price = closes_15m[-1]
    prev_close, prev_open = closes_15m[-2], opens_15m[-2]
    bar_green = prev_close >= prev_open          # ultima 15m COMPLETATA
    or_high, or_low = max(highs_15m[-6:-2]), min(lows_15m[-6:-2])
    or_mid = (or_high + or_low) / 2.0
    vol_avg = sum(vols_15m[-22:-2]) / 20.0 if len(vols_15m) >= 22 else 1.0
    vol_confirm = vols_15m[-2] >= vol_avg * 0.85    # fix: non usa più la candela in formazione

    recent_flag = (max(highs_1h[-6:-1]) - min(lows_1h[-6:-1])) / max(price, 1e-8) * 100.0
    prior_pole = (max(highs_1h[-14:-5]) - min(lows_1h[-18:-10])) / max(price, 1e-8) * 100.0

    if (bb_pct_b <= 42 or pos_in_channel <= 40) and price > or_mid and bar_green:
        name, trig = "Oversold Bounce (Rimbalzo Ipervenduto)", True
        txt = f"TRIGGERED ✅ (Chiusura 15m sopra micro-mid ${fmt_p(or_mid)})"
    elif prior_pole >= 3.0 and recent_flag <= 2.8 and (prev_close > or_high or price > or_high):
        name, trig = "Bull Flag Breakout (Bandiera Rialzista)", True
        txt = f"TRIGGERED ✅ (Breakout sopra ${fmt_p(or_high)})"
    elif closes_1h[-2] <= ema20_1h and price > ema20_1h and bar_green:
        name, trig = "EMA20 Reclaim (Recupero Media 20)", True
        txt = f"TRIGGERED ✅ (Recuperata EMA20 ${fmt_p(ema20_1h)})"
    elif 15 <= pos_in_channel <= 62 and (bar_green or price >= or_mid) and vol_confirm:
        name, trig = "Grid Equilibrium (Oscillazione Ideale)", True
        txt = f"TRIGGERED ✅ (Conferma 15m a ${fmt_p(price)})"
    else:
        dist = (or_high - price) / max(price, 1e-8) * 100.0
        name, trig = "Consolidamento in Osservazione", False
        txt = f"WAITING ⏳ (a {dist:+.2f}% dal trigger ${fmt_p(or_high)})"
    return {"setup_name": name, "triggered": trig, "status_txt": txt,
            "or_high": or_high, "or_low": or_low, "or_mid": or_mid, "vol_confirm": vol_confirm}


# ==============================================================================
# SKILL: EXIT PLANNER
# ==============================================================================

def run_exit_planner(price, lower, upper, atr_1h, step_usd, spread_bps, net_step_pct, grids):
    spread_usd = price * spread_bps / 10000.0
    min_dist = max(spread_usd * 8.0, atr_1h * 0.85, price * 0.012)
    raw_stop = lower - max(atr_1h * 1.05, step_usd * 1.25)
    if price - raw_stop < min_dist:
        raw_stop = price - min_dist
    stop_price = round_p(raw_stop)
    one_r = max(price - stop_price, 1e-8)
    one_r_pct = one_r / price * 100.0

    grid_yield_pct = max(3.0, grids * 0.65) * net_step_pct
    grid_yield_usd = price * grid_yield_pct / 100.0
    directional = max(upper - price, 0.0)
    rr_total = (directional + grid_yield_usd) / one_r
    rr_directional = directional / one_r

    valid, reason = True, "Piano Exit valido ✅"
    if stop_price >= price:
        valid, reason = False, "Stop sopra o uguale al prezzo di entrata"
    elif upper <= price:
        valid, reason = False, "Target sotto o uguale al prezzo di entrata"
    elif one_r < spread_usd * 5.0:
        valid, reason = False, "Stop troppo stretto per sopravvivere allo spread"

    return {"valid": valid, "reason": reason, "stop_price": stop_price, "target_price": upper,
            "one_r_usd": one_r, "one_r_pct": one_r_pct, "rr_ratio": rr_total,
            "rr_directional": rr_directional, "level_1r": round_p(price + one_r),
            "level_2r": round_p(price + 2 * one_r), "grid_yield_pct": grid_yield_pct}


# ==============================================================================
# SKILL: TIME MACHINE (Replay storico + Monte Carlo con BLOCK BOOTSTRAP e costi)
# ==============================================================================

def _tm_default(verdict="DATI PARZIALI"):
    # fix: prima ritornava chiavi errate (KeyError) e valori OTTIMISTICI -> ora conservativo
    return {"win_or_grid_prob": 40.0, "target_first_prob": 20.0, "stop_first_prob": 40.0,
            "in_range_prob": 40.0, "expected_r": 0.0, "hist_win_rate": 40.0, "verdict": verdict}


def run_time_machine(highs_1h, lows_1h, closes_1h, entry_price, stop_price, target_price,
                     one_r_usd, rr_ratio, n_sims=1500, horizon_hours=24, block=4):
    if len(closes_1h) < 80 or one_r_usd <= 0 or entry_price <= 0:
        return _tm_default()

    up_pct = (target_price - entry_price) / entry_price
    dn_pct = (stop_price - entry_price) / entry_price
    cost_r = (COST_PCT / 100.0) * entry_price / one_r_usd     # fee+slippage espressi in R

    # 1) Replay storico (ultime ~320 ore): chi viene colpito prima, stop o target?
    wins = losses = in_grid = 0
    start_h = max(20, len(closes_1h) - 320)
    end_h = len(closes_1h) - horizon_hours - 1
    for i in range(start_h, end_h):
        p0 = closes_1h[i]
        t_lvl, s_lvl = p0 * (1 + up_pct), p0 * (1 + dn_pct)
        outcome = 0
        for k in range(1, horizon_hours + 1):
            if lows_1h[i + k] <= s_lvl:        # conservativo: stop prima del target nella stessa barra
                outcome = -1
                break
            if highs_1h[i + k] >= t_lvl:
                outcome = 1
                break
        wins += outcome == 1
        losses += outcome == -1
        in_grid += outcome == 0
    tot = max(wins + losses + in_grid, 1)
    hist_success = (wins + in_grid * 0.75) / tot * 100.0

    # 2) Monte Carlo con BLOCK bootstrap (conserva volatility clustering/autocorrelazione)
    shocks = []
    for i in range(max(1, len(closes_1h) - 240), len(closes_1h)):
        cp = closes_1h[i - 1]
        if cp > 0:
            shocks.append(((closes_1h[i] - cp) / cp, (highs_1h[i] - cp) / cp, (lows_1h[i] - cp) / cp))
    if len(shocks) <= block + 5:
        return _tm_default()

    rng = random.Random(int(entry_price * 1000) + len(closes_1h))
    mc_t = mc_s = mc_r = 0
    sum_r = 0.0
    for _ in range(n_sims):
        sim_p, hit, osc, step = entry_price, 0, 0, 0
        while step < horizon_hours and hit == 0:
            s0 = rng.randrange(0, len(shocks) - block)
            for rc, rh, rl in shocks[s0:s0 + block]:
                if step >= horizon_hours:
                    break
                step += 1
                bar_hi, bar_lo = sim_p * (1 + rh), sim_p * (1 + rl)
                if (bar_hi - bar_lo) / sim_p >= 0.0045:
                    osc += 1
                if bar_lo <= stop_price:
                    hit = -1
                    break
                if bar_hi >= target_price:
                    hit = 1
                    break
                sim_p *= 1 + rc
        if hit == 1:
            mc_t += 1
            sum_r += rr_ratio - cost_r
        elif hit == -1:
            mc_s += 1
            sum_r -= 1.0 + cost_r
        else:
            mc_r += 1
            grid_gain = min(rr_ratio * 0.85, osc * 0.08)
            price_r = (sim_p - entry_price) / one_r_usd
            sum_r += max(-0.6, price_r + grid_gain) - cost_r

    t_p, s_p, r_p = mc_t / n_sims * 100.0, mc_s / n_sims * 100.0, mc_r / n_sims * 100.0
    win_or_grid = 0.65 * (t_p + r_p * 0.80) + 0.35 * hist_success
    ev = sum_r / n_sims
    if ev >= 0.55 and s_p <= 28.0:
        verdict = "OTTIMO EDGE STATISTICO 🟢"
    elif ev >= 0.25 and s_p <= 38.0:
        verdict = "VANTAGGIO POSITIVO ✅"
    elif ev >= 0.05:
        verdict = "MARGINALE / COIN FLIP 🟡"
    else:
        verdict = "ASPETTATIVA NEGATIVA 🔴"
    return {"win_or_grid_prob": win_or_grid, "target_first_prob": t_p, "stop_first_prob": s_p,
            "in_range_prob": r_p, "expected_r": ev, "hist_win_rate": hist_success, "verdict": verdict}


# ==============================================================================
# SKILL: BUDGET & RISK MANAGER (sizing a rischio frazionario + Kelly cap + gatekeeper)
# ==============================================================================

def compute_position_size(invest_usdt, price, exit_plan, tm):
    """
    Fixed-fractional risk sizing: perdita allo stop <= MAX_LOSS_PCT_PER_TRADE del capitale,
    con cap Kelly frazionario (stime MC) e cash buffer. Esposizione reale = 100% della size
    (il vecchio fattore 0.65 sottostimava il rischio: l'ordine eseguito è un market buy pieno).
    """
    usable = invest_usdt * (1.0 - CASH_BUFFER_PCT / 100.0)
    stop_dist_pct = exit_plan["one_r_pct"] / 100.0 + COST_PCT / 100.0
    risk_budget = invest_usdt * MAX_LOSS_PCT_PER_TRADE / 100.0
    notional_risk = risk_budget / max(stop_dist_pct, 1e-6)

    p = tm["target_first_prob"] / 100.0
    q = tm["stop_first_prob"] / 100.0
    b = max(exit_plan["rr_ratio"], 1e-6)
    kelly = max(0.0, p - q / b)
    kelly_factor = max(0.35, min(1.0, 0.35 + 2.0 * kelly))   # mai sotto 35%, mai sopra 100%

    notional = min(usable, notional_risk, usable * kelly_factor)
    return round(notional, 2), kelly, kelly_factor, round(notional * stop_dist_pct, 2), round(risk_budget, 2)


def run_budget_and_risk_manager(coin_sym, pionex_sym, price, lower, upper, grids, invest_usdt,
                                exit_plan, time_machine, news, state):
    notional, kelly, kfac, risk_usdt, max_loss_usdt = compute_position_size(invest_usdt, price, exit_plan, time_machine)
    qty_coin = round(notional / max(price, 1e-8), 6)

    today_str = news["now_rome"].strftime("%d/%m")
    history = state.get("history", [])
    today_trades = [h for h in history if str(h.get("closed_at", "")).startswith(today_str)]
    today_pnl_pct = sum(float(h.get("pnl_usdt", 0.0)) for h in today_trades) / max(invest_usdt, 1e-8) * 100.0
    trades_today = len(today_trades) + (1 if state.get("active") and state.get("opened_date") == today_str else 0)
    cum_pnl = sum(float(h.get("pnl_usdt", 0.0)) for h in history)
    peak, run, dd = 0.0, 0.0, 0.0
    for h in reversed(history):         # history è ordinata dal più recente
        run += float(h.get("pnl_usdt", 0.0))
        peak = max(peak, run)
        dd = max(dd, peak - run)
    dd_pct = dd / max(invest_usdt, 1e-8) * 100.0

    blocks = []
    if not exit_plan["valid"]:
        blocks.append(f"Exit Plan invalido ({exit_plan['reason']})")
    if notional < MIN_POSITION_USDT:
        blocks.append(f"Size ({notional:.1f}$) sotto il minimo operativo ({MIN_POSITION_USDT:.0f}$)")
    if exit_plan["rr_ratio"] < MIN_REWARD_RISK_R:
        blocks.append(f"Reward:Risk ({exit_plan['rr_ratio']:.2f}R) < minimo ({MIN_REWARD_RISK_R:.2f}R)")
    if time_machine["expected_r"] < 0.15 or time_machine["stop_first_prob"] > 42.0:
        blocks.append(f"Time Machine sfavorevole (E[R]={time_machine['expected_r']:+.2f}R, StopFirst={time_machine['stop_first_prob']:.0f}%)")
    if today_pnl_pct <= -DAILY_MAX_LOSS_PCT:
        blocks.append(f"Daily Loss Limit raggiunto ({today_pnl_pct:+.2f}%)")
    if trades_today >= MAX_TRADES_PER_DAY:
        blocks.append(f"Max trade giornalieri ({trades_today}/{MAX_TRADES_PER_DAY})")
    if dd_pct >= KILL_SWITCH_DRAWDOWN_PCT:
        blocks.append(f"KILL-SWITCH: drawdown storico {dd_pct:.1f}% >= {KILL_SWITCH_DRAWDOWN_PCT:.0f}%")
    if news["status"] == "IMMINENT" and any(e["impact"] == "High" for e in news["imminent"]):
        blocks.append("Notizia Macro USA High-Impact imminente")

    passed = not blocks
    return {
        "passed": passed, "status": "PASS ✅" if passed else "BLOCK ⛔",
        "reason": "Tutti i controlli di rischio superati" if passed else " | ".join(blocks),
        "risk_usdt": risk_usdt, "max_loss_usdt": max_loss_usdt,
        "today_pnl_pct": round(today_pnl_pct, 2), "trades_today": trades_today,
        "drawdown_pct": round(dd_pct, 2), "kelly": kelly, "kelly_factor": kfac,
        "bracket_order": {
            "symbol": pionex_sym, "side": "BUY", "type": "MARKET+VIRTUAL_BRACKET",
            "qty_coin": qty_coin, "order_usdt": notional, "entry_price": round_p(price),
            "stop_loss": exit_plan["stop_price"], "take_profit": exit_plan["target_price"],
            "risk_usdt": risk_usdt, "max_loss_allowed_usdt": max_loss_usdt,
            "rr_ratio": round(exit_plan["rr_ratio"], 2),
        },
    }


# ==============================================================================
# FONTI DATI
# ==============================================================================

def fetch_eur_usdt_rate():
    try:
        return fetch_last_price("EURUSD")
    except Exception:
        return 1.08


def fetch_orderbook_imbalance(pair="SOLUSD", depth=50):
    bad = {"imbalance": 0.0, "bid_ask_ratio": 1.0, "spread_bps": 2.0, "ok": False}
    try:
        res = kraken_get("/0/public/Depth", {"pair": pair, "count": depth})
        book = list(res.values())[0]
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return bad
        bb, ba = float(bids[0][0]), float(asks[0][0])
        mid = (bb + ba) / 2.0
        spread_bps = (ba - bb) / mid * 10000.0
        wb = sum(float(v) * math.exp(-max(abs(mid - float(p)) / mid, 0.0005) * 150.0) for p, v, *_ in bids)
        wa = sum(float(v) * math.exp(-max(abs(float(p) - mid) / mid, 0.0005) * 150.0) for p, v, *_ in asks)
        tot = wb + wa
        return {"imbalance": (wb - wa) / tot * 100.0 if tot > 0 else 0.0,
                "bid_ask_ratio": wb / wa if wa > 0 else 1.0, "spread_bps": spread_bps, "ok": True}
    except Exception:
        return bad


def fetch_all_futures_metrics():
    out = {}
    try:
        r = SESSION.get("https://futures.kraken.com/derivatives/api/v3/tickers", timeout=12)
        for t in r.json().get("tickers", []):
            sym = t.get("symbol", "")
            if not sym.startswith("PF_"):
                continue
            fr = float(t.get("fundingRate") or 0.0)         # assoluto per ora, in USD
            mark = float(t.get("markPrice") or 0.0)
            if mark <= 0:
                continue
            out[sym] = {"funding_8h_pct": fr / mark * 100.0 * 8.0,   # fix: sempre relativo al mark
                        "open_interest": float(t.get("openInterest") or 0.0), "ok": True}
    except Exception:
        pass
    return out


def fetch_fear_and_greed():
    try:
        d = SESSION.get("https://api.alternative.me/fng/?limit=1", timeout=10).json().get("data", [])
        if d:
            return {"value": int(d[0].get("value", 50)), "label": d[0].get("value_classification", "Neutral"), "ok": True}
    except Exception:
        pass
    return {"value": 50, "label": "Neutral", "ok": False}


# ==============================================================================
# MOTORE AI: MLP + LORENTZIAN k-NN con VALIDAZIONE WALK-FORWARD e SHRINKAGE
# ==============================================================================

def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))


def _clip(v, lim=3.0):
    return max(-lim, min(lim, v))


def _mlp_forward(W1, b1, W2, b2, x):
    h = [math.tanh(_clip(sum(w * xi for w, xi in zip(W1[j], x)) + b1[j], 15.0)) for j in range(len(W1))]
    return _sigmoid(sum(W2[j] * h[j] for j in range(len(h))) + b2), h


def _knn_prob(x, X, y, k=15):
    d = sorted(((sum(math.log(1.0 + abs(a - b)) for a, b in zip(x, f)), l) for f, l in zip(X, y)), key=lambda t: t[0])[:k]
    if not d:
        return 0.5
    ws = [1.0 / (dist + 0.05) for dist, _ in d]
    return sum(w * l for w, (_, l) in zip(ws, d)) / sum(ws)


def run_neural_and_lorentzian_ai(highs, lows, closes, vols):
    n = len(closes)
    neutral = {"combined_prob": 0.50, "raw_prob": 0.50, "mlp_prob": 0.50, "knn_prob": 0.50,
               "samples": 0, "oos_skill": 0.0, "shrink": 0.25, "base_rate": 0.5}
    if n < 120:
        return neutral

    rsi_s, e20, e50, macd_s = rsi_series(closes, 14), ema_series(closes, 20), ema_series(closes, 50), macd_hist_series(closes)

    def feats(i):
        p = closes[i]
        vw = vols[max(0, i - 20):i + 1]
        vavg = sum(vw) / len(vw) if vw else 1.0
        return [_clip((rsi_s[i] - 50.0) / 25.0),
                _clip((p - e20[i]) / max(e20[i], 1e-8) * 25.0),
                _clip((e20[i] - e50[i]) / max(e50[i], 1e-8) * 25.0),
                _clip(macd_s[i] / max(p, 1e-8) * 300.0),
                _clip((p - closes[i - 4]) / max(closes[i - 4], 1e-8) * 20.0),
                _clip(vols[i] / max(vavg, 1e-8) - 1.0)]

    horizon = 6
    X, y = [], []
    for i in range(max(55, n - 450), n - horizon - 1):
        p = closes[i]
        fh, fl = max(highs[i + 1:i + 1 + horizon]), min(lows[i + 1:i + 1 + horizon])
        X.append(feats(i))
        y.append(1.0 if (fh >= p * 1.0065 and fl >= p * 0.982) else 0.0)
    if len(X) < 60:
        neutral["samples"] = len(X)
        return neutral
    x_now = feats(n - 1)

    # Split walk-forward: train sul passato, test sul futuro con EMBARGO pari all'orizzonte
    # (evita leakage delle label sovrapposte).
    cut = int(len(X) * 0.78)
    Xtr, ytr = X[:cut - horizon], y[:cut - horizon]
    Xte, yte = X[cut:], y[cut:]
    base = sum(ytr) / len(ytr)

    rng = random.Random(42)
    n_in, n_hid = 6, 5
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(n_in)] for _ in range(n_hid)]
    b1 = [0.0] * n_hid
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(n_hid)]
    b2 = 0.0
    lr = 0.045
    order = list(range(len(Xtr)))
    for _ in range(30):
        rng.shuffle(order)             # fix: SGD con ordine fisso -> bias verso gli ultimi campioni
        for idx in order:
            xv, t = Xtr[idx], ytr[idx]
            pred, h = _mlp_forward(W1, b1, W2, b2, xv)
            dz2 = pred - t
            for j in range(n_hid):
                dh = dz2 * W2[j] * (1.0 - h[j] ** 2)
                W2[j] -= lr * dz2 * h[j]
                b1[j] -= lr * dh
                for i in range(n_in):
                    W1[j][i] -= lr * dh * xv[i]
            b2 -= lr * dz2

    # Out-of-sample: Brier score vs baseline (rate costante) -> "skill"
    brier = brier_base = 0.0
    for xv, t in zip(Xte, yte):
        pm, _ = _mlp_forward(W1, b1, W2, b2, xv)
        pk = _knn_prob(xv, Xtr, ytr)
        pc = 0.6 * pm + 0.4 * pk
        brier += (pc - t) ** 2
        brier_base += (base - t) ** 2
    skill = 1.0 - brier / brier_base if brier_base > 0 else 0.0
    shrink = max(0.25, min(1.0, 0.5 + 5.0 * skill))    # skill<=0 -> la AI pesa solo il 25-50%

    mlp_p, _ = _mlp_forward(W1, b1, W2, b2, x_now)
    knn_p = _knn_prob(x_now, X, y)
    raw = 0.6 * mlp_p + 0.4 * knn_p
    adj = base + (raw - base) * shrink
    return {"combined_prob": adj, "raw_prob": raw, "mlp_prob": mlp_p, "knn_prob": knn_p,
            "samples": len(X), "oos_skill": skill, "shrink": shrink, "base_rate": base}


# ==============================================================================
# ANALISI MULTI-COIN E SCORING 100 PUNTI
# ==============================================================================

def analyze_btc_context():
    try:
        c1h, c5m = fetch_ohlc(BTC_PAIR, 60), fetch_ohlc(BTC_PAIR, 5)
        highs, lows = [float(c[2]) for c in c1h], [float(c[3]) for c in c1h]
        closes = [float(c[4]) for c in c1h]
        price = closes[-1]
        chg4h = (price - closes[-5]) / closes[-5] * 100.0
        chg24h = (price - closes[-25]) / closes[-25] * 100.0
        ema50, ema200 = ema_series(closes, 50)[-1], ema_series(closes, 200)[-1]
        adx, pdi, mdi = adx_di(highs, lows, closes, 14)
        return {"price": price, "chg4h": chg4h, "chg24h": chg24h, "above_ema50": price >= ema50,
                "dumping": chg4h <= -2.8 or chg24h <= -6.0,
                "strong_bear": price < ema200 and mdi > pdi and adx > 28,
                "news_reaction": check_market_news_reaction(c5m), "ok": True}
    except Exception as e:
        print("BTC context non disponibile:", e)
        return {"price": 0.0, "chg4h": 0.0, "chg24h": 0.0, "above_ema50": True, "dumping": False,
                "strong_bear": False, "ok": False,
                "news_reaction": {"calm": True, "range_15m_pct": 0.4, "move_15m_pct": 0.3, "vol_spike": False}}


def analyze_single_coin(cfg, btc, eur_usdt, invest_usdt, all_futures, fng, news, state):
    pair, coin = cfg["kraken"], cfg["coin"]
    c1h, c4h, c15m = fetch_ohlc(pair, 60), fetch_ohlc(pair, 240), fetch_ohlc(pair, 15)
    ob = fetch_orderbook_imbalance(pair, 50)
    futures = all_futures.get(cfg["futures"], {"funding_8h_pct": 0.0, "open_interest": 0.0, "ok": False})

    col = lambda rows, i: [float(c[i]) for c in rows]
    opens_1h, highs_1h, lows_1h, closes_1h, vols_1h = col(c1h, 1), col(c1h, 2), col(c1h, 3), col(c1h, 4), col(c1h, 6)
    highs_4h, lows_4h, closes_4h = col(c4h, 2), col(c4h, 3), col(c4h, 4)
    opens_15m, highs_15m, lows_15m, closes_15m, vols_15m = col(c15m, 1), col(c15m, 2), col(c15m, 3), col(c15m, 4), col(c15m, 6)
    if len(closes_1h) < 210 or len(closes_4h) < 60 or len(closes_15m) < 30:
        raise RuntimeError(f"storico insufficiente per {coin}")

    price = closes_1h[-1]
    chg24 = (price - closes_1h[-25]) / closes_1h[-25] * 100.0
    chg4h = (price - closes_1h[-5]) / closes_1h[-5] * 100.0

    ema20_1h, ema50_1h, ema200_1h = (ema_series(closes_1h, p)[-1] for p in (20, 50, 200))
    ema50_4h, ema200_4h = ema_series(closes_4h, 50)[-1], ema_series(closes_4h, 200)[-1]
    rsi_1h = rsi_series(closes_1h, 14)[-1]
    rsi15 = rsi_series(closes_15m, 14)
    rsi_15m, rsi_15m_prev = rsi15[-1], rsi15[-2]
    srsi_15m = stoch_rsi(closes_15m, 14, 14)
    macd_h = macd_hist_series(closes_1h)
    macd_rising = macd_h[-1] > macd_h[-2]
    bb_lo, bb_mid, bb_up, bb_width, bb_pct_b = bollinger(closes_1h, 20, 2.0)

    atrs = atr_series(highs_1h, lows_1h, closes_1h, 14)
    atr_1h = atrs[-1]
    atr_pct = atr_1h / price * 100.0
    atr_mean = sum(atrs[-168:]) / min(len(atrs), 168)
    atr_ratio = atr_1h / atr_mean if atr_mean else 1.0
    kc_width = atr_1h * 3.0 / bb_mid * 100.0 if bb_mid else 0.0
    in_squeeze = bb_width < kc_width and atr_ratio < 0.70

    adx_1h, pdi_1h, mdi_1h = adx_di(highs_1h, lows_1h, closes_1h, 14)
    adx_4h, _, _ = adx_di(highs_4h, lows_4h, closes_4h, 14)
    hurst = hurst_exponent(closes_1h, 24)
    vr_6 = variance_ratio_mr(closes_1h, 6)
    er = efficiency_ratio(closes_1h, 20)
    vwap_24h = vwap_rolling(highs_1h, lows_1h, closes_1h, vols_1h, 24)
    vwap_diff_pct = (price - vwap_24h) / vwap_24h * 100.0

    ai_res = run_neural_and_lorentzian_ai(highs_1h, lows_1h, closes_1h, vols_1h)
    ai_prob = ai_res["combined_prob"]

    vol_sma20 = sma(vols_1h[:-1], 20)
    reds = [vols_1h[i] / vol_sma20 if vol_sma20 else 1.0 for i in range(-3, 0) if closes_1h[i] < opens_1h[i]]
    max_red_vol_ratio = max(reds) if reds else 0.0
    panic_selling = (max_red_vol_ratio >= 2.3 and chg4h <= -2.0) or chg4h <= -3.8
    ema9_15m = ema_series(closes_15m, 9)[-1]
    bounce_15m = closes_15m[-1] >= opens_15m[-1] or closes_15m[-1] > ema9_15m or rsi_15m > rsi_15m_prev

    # Range ottimale
    max_grids = max(4, min(10, int(invest_usdt // MIN_ORDER_USDT)))
    swing_low, swing_high = min(lows_1h[-72:]), max(highs_1h[-72:])
    span_pct = min(max(max_grids * TARGET_STEP_PCT, atr_pct * 4.2, 3.2), 7.2)
    span_usd = price * span_pct / 100.0
    if price <= bb_mid:
        raw_low = max(swing_low * 0.995, price - span_usd * 0.42)
        raw_high = raw_low + span_usd
    else:
        raw_high = min(swing_high * 1.005, price + span_usd * 0.48)
        raw_low = raw_high - span_usd
    lower, upper = round_p(raw_low), round_p(raw_high)
    if upper <= lower:
        upper = round_p(lower * 1.04)
    range_pct = (upper - lower) / price * 100.0
    grids = max_grids
    while grids > 4 and (range_pct / grids - PIONEX_ROUNDTRIP_FEE_PCT) < MIN_NET_STEP_PCT:
        grids -= 1
    gross_step_pct = range_pct / grids
    net_step_pct = gross_step_pct - PIONEX_ROUNDTRIP_FEE_PCT
    step_usd = (upper - lower) / grids
    pos_in_channel = (price - lower) / (upper - lower) * 100.0 if upper > lower else 50.0
    rotation_ratio = (max(highs_1h[-24:]) - min(lows_1h[-24:])) / max(upper - lower, 1e-8)
    alpha_vs_btc = chg24 - btc["chg24h"] if btc["ok"] else 0.0

    entry_timer = run_chart_scanner_and_entry_timer(
        opens_1h, highs_1h, lows_1h, closes_1h, vols_1h,
        opens_15m, highs_15m, lows_15m, closes_15m, vols_15m, ema20_1h, bb_pct_b, pos_in_channel)
    exit_plan = run_exit_planner(price, lower, upper, atr_1h, step_usd, ob["spread_bps"], net_step_pct, grids)
    stop_loss = exit_plan["stop_price"]
    tm = run_time_machine(highs_1h, lows_1h, closes_1h, price, stop_loss, upper,
                          exit_plan["one_r_usd"], exit_plan["rr_ratio"], n_sims=1500, horizon_hours=24)
    risk_check = run_budget_and_risk_manager(coin, cfg["pionex"], price, lower, upper, grids,
                                             invest_usdt, exit_plan, tm, news, state)

    # ---------------- RUBRICA 10 PILASTRI ----------------
    pts, bd = 0.0, []

    p1 = 14.0 if ai_prob >= 0.64 else 11.5 if ai_prob >= 0.57 else 8.5 if ai_prob >= 0.51 else 4.5 if ai_prob >= 0.45 else 1.0
    pts += p1
    bd.append(f"🧠 <b>AI MLP + Lorentzian k-NN ({p1:.0f}/14)</b>: Prob. Long <b>{ai_prob*100:.1f}%</b> "
              f"(grezza {ai_res['raw_prob']*100:.0f}%, skill OOS {ai_res['oos_skill']:+.3f}, peso {ai_res['shrink']:.2f})")

    ev_r, sf_p = tm["expected_r"], tm["stop_first_prob"]
    p_tm = 12.0 if (ev_r >= 0.60 and sf_p <= 25) else 9.5 if (ev_r >= 0.35 and sf_p <= 34) else 6.5 if (ev_r >= 0.15 and sf_p <= 40) else 2.0
    pts += p_tm
    bd.append(f"🕰️ <b>Time Machine ({p_tm:.0f}/12)</b>: E[R] netto costi <b>{ev_r:+.2f}R</b> | "
              f"Successo <b>{tm['win_or_grid_prob']:.0f}%</b> (StopFirst {sf_p:.0f}%)")

    p2 = 0.0
    p2 += 5.0 if price >= ema50_4h else 3.0 if price >= ema200_4h else 0.0
    p2 += 4.0 if price >= ema200_1h else 2.0 if price >= ema50_1h else 0.0
    p2 += 4.0 if pdi_1h >= mdi_1h else 2.5 if adx_1h < 22 else 0.0
    pts += p2
    lbl = "Rialzista/Neutro" if p2 >= 9 else "Misto" if p2 >= 5 else "Debole/Ribassista"
    bd.append(f"📐 <b>Struttura Multi-TF ({p2:.0f}/13)</b>: {lbl} (ADX 1H {adx_1h:.1f}, EMA50 4H ${fmt_p(ema50_4h)})")

    p3 = 4.5 if hurst <= 0.47 else 3.0 if hurst <= 0.53 else 1.5
    p3 += 3.0 if vr_6 <= 0.95 else 2.0 if vr_6 <= 1.10 else 0.0
    p3 += 3.5 if (not in_squeeze and 0.72 <= atr_ratio <= 1.45) else 1.5 if not in_squeeze else 0.0
    if er <= 0.30:
        p3 += 1.0                      # ER basso = rumore laterale, ideale per mean reversion/grid
    p3 = min(p3, 11.0)
    pts += p3
    regime = "Mean-Reverting (Ideale)" if hurst < 0.49 else "Equilibrato" if hurst <= 0.53 else "Trending"
    bd.append(f"🔬 <b>Econometria ({p3:.0f}/11)</b>: Hurst <b>{hurst:.2f}</b> ({regime}), VR {vr_6:.2f}, ER {er:.2f}, ATR ratio {atr_ratio:.2f}x")

    p4 = 6.5 if 15 <= pos_in_channel <= 58 else 4.0 if pos_in_channel <= 68 and pos_in_channel > 58 else 3.5 if 8 <= pos_in_channel < 15 else 1.0
    p4 += 3.0 if -2.2 <= vwap_diff_pct <= 0.8 else 2.0 if vwap_diff_pct <= 1.8 and vwap_diff_pct > 0.8 else 0.0
    p4 += 2.5 if 18 <= bb_pct_b <= 68 else 0.0
    pts += p4
    bd.append(f"🎯 <b>Valore & Posizione ({p4:.0f}/12)</b>: canale <b>{pos_in_channel:.0f}%</b>, VWAP {vwap_diff_pct:+.2f}%, BB %B {bb_pct_b:.0f}%")

    p5 = 4.5 if entry_timer["triggered"] else 2.0 if bounce_15m else 0.0
    p5 += 3.5 if macd_rising else 0.0
    p5 += 3.0 if (34 <= rsi_1h <= 64 and srsi_15m < 85) else 0.0
    pts += p5
    bd.append(f"⏱️ <b>Entry Timer ({p5:.0f}/11)</b>: <b>{entry_timer['setup_name']}</b> — {entry_timer['status_txt']}")

    p6 = 5.0 if ob["imbalance"] >= 8.0 else 3.5 if ob["imbalance"] >= -5.0 else 1.5 if ob["imbalance"] >= -15.0 else 0.0
    p6 += 4.0 if (not panic_selling and max_red_vol_ratio < 1.6) else 2.0 if not panic_selling else 0.0
    pts += p6
    bd.append(f"🏛️ <b>Orderbook L2 & Flussi ({p6:.0f}/9)</b>: Imbalance <b>{ob['imbalance']:+.1f}%</b> (Spread {ob['spread_bps']:.1f} bps)")

    if not btc["dumping"] and not btc["strong_bear"]:
        p7 = 3.5 + (2.5 if alpha_vs_btc >= 0 else 1.5 if alpha_vs_btc >= -1.5 else 0.0)
    else:
        p7 = 2.0 if not btc["dumping"] else 0.0
    pts += p7
    bd.append(f"₿ <b>Alpha vs BTC ({p7:.0f}/6)</b>: BTC 24h {btc['chg24h']:+.1f}% | Alpha {coin} {alpha_vs_btc:+.1f}%")

    fr = futures["funding_8h_pct"]
    p8 = (3.0 if -0.025 <= fr <= 0.022 else 2.5 if fr < -0.025 else 1.0) + (2.0 if 22 <= fng["value"] <= 74 else 1.0)
    pts += p8
    bd.append(f"🔮 <b>Futures & Sentiment ({p8:.0f}/5)</b>: Funding 8h <b>{fr:+.4f}%</b> | F&G <b>{fng['value']}</b> ({fng['label']})")

    p9 = (4.5 if (exit_plan["rr_ratio"] >= 1.75 and net_step_pct >= 0.18) else 3.0 if exit_plan["rr_ratio"] >= MIN_REWARD_RISK_R else 0.0)
    p9 += 2.5 if 0.45 <= rotation_ratio <= 1.40 else 0.0
    pts += p9
    bd.append(f"💶 <b>Exit Plan & R:R ({p9:.0f}/7)</b>: R:R <b>{exit_plan['rr_ratio']:.2f}R</b> "
              f"(direzionale {exit_plan['rr_directional']:.2f}R, 1R = -{exit_plan['one_r_pct']:.2f}%) | Netto/grid <b>+{net_step_pct:.2f}%</b>")

    score = max(1, min(10, round(pts / 10.0)))

    veto = []
    if btc["dumping"]:
        score = min(score, 2)
        veto.append(f"⛔ <b>VETO BTC</b>: Bitcoin in forte calo ({btc['chg4h']:+.1f}% in 4h)")
    elif btc["strong_bear"]:
        score = min(score, 3)
        veto.append("⛔ <b>VETO BTC</b>: Bitcoin in deciso trend ribassista")
    if panic_selling:
        score = min(score, 2)
        veto.append(f"⛔ <b>VETO VOLUMI</b>: vendite anomale su {coin} ({chg4h:+.1f}% in 4h)")
    if price < ema50_4h and price < ema200_1h and mdi_1h > pdi_1h and (adx_1h > 25 or chg24 < -5.0):
        score = min(score, 3)
        veto.append(f"⛔ <b>VETO TREND</b>: trend ribassista allineato 4H/1H su {coin}")
    if in_squeeze:
        score = min(score, 4)
        veto.append("⚠️ <b>COMPRESSIONE</b>: Keltner Squeeze (attesa esplosione)")
    if pos_in_channel > 74:
        score = min(score, 5)
        veto.append(f"⚠️ <b>TETTO RANGE</b>: prezzo alto nel canale ({pos_in_channel:.0f}%)")
    if ai_prob < 0.38:
        score = min(score, 4)
        veto.append(f"⚠️ <b>VETO AI</b>: probabilità sfavorevole ({ai_prob*100:.0f}%)")
    if not risk_check["passed"]:
        score = min(score, 5)
        veto.append(f"🛡️ <b>RISK MANAGER BLOCK</b>: {html.escape(risk_check['reason'])}")

    return {
        "coin": coin, "name": cfg["name"], "pionex_symbol": cfg["pionex"], "price": price,
        "chg24": chg24, "chg4h": chg4h, "vwap_24h": vwap_24h, "rsi_1h": rsi_1h, "rsi_15m": rsi_15m,
        "adx_1h": adx_1h, "adx_4h": adx_4h, "hurst": hurst, "vr_6": vr_6, "atr_pct": atr_pct,
        "atr_abs": atr_1h, "atr_ratio": atr_ratio, "ai_prob": ai_prob, "ai_res": ai_res, "ob": ob,
        "futures": futures, "fng": fng, "btc": btc, "alpha_vs_btc": alpha_vs_btc, "eur_usdt": eur_usdt,
        "invest_usdt": invest_usdt, "lower": lower, "upper": upper, "grids": grids, "range_pct": range_pct,
        "net_step_pct": net_step_pct, "stop_loss": stop_loss, "pos_in_channel": pos_in_channel,
        "entry_timer": entry_timer, "exit_plan": exit_plan, "time_machine": tm, "risk_check": risk_check,
        "pts_100": pts, "score": score, "breakdown": bd, "veto_reasons": veto,
        "news_reaction": btc["news_reaction"],
    }


def apply_news_cap(raw_score, news, nr):
    s = raw_score
    if news["status"] == "IMMINENT":
        s = min(s, 2 if any(e["impact"] == "High" for e in news["imminent"]) else 4)
    elif news["status"] == "COOLDOWN":
        if news["surprise_detected"] and not nr["calm"]:
            s = min(s, 2)
        elif not nr["calm"]:
            s = min(s, 3)
        elif news["surprise_detected"]:
            s = min(s, 5)
    if news["us_open_warning"] and not nr["calm"]:
        s = min(s, 4)
    return s


def scan_all_coins(news):
    state = load_bot_state()
    btc = analyze_btc_context()
    eur_usdt = fetch_eur_usdt_rate()
    invest_usdt = round(INVEST_EUR * eur_usdt, 2)
    all_futures, fng = fetch_all_futures_metrics(), fetch_fear_and_greed()

    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        fmap = {ex.submit(analyze_single_coin, cfg, btc, eur_usdt, invest_usdt, all_futures, fng, news, state): cfg
                for cfg in COIN_UNIVERSE}
        for fut in as_completed(fmap):
            cfg = fmap[fut]
            try:
                res = fut.result()
                res["final_score"] = apply_news_cap(res["score"], news, res["news_reaction"])
                results.append(res)
            except Exception as e:
                print(f"Errore scansione {cfg['coin']}: {e}")
    if not results:
        raise RuntimeError("Nessuna criptovaluta scansionata correttamente dalle API.")
    results.sort(key=lambda x: (1 if x["risk_check"]["passed"] else 0, x["final_score"],
                                x["pts_100"], x["time_machine"]["expected_r"]), reverse=True)
    best = results[0]
    best["all_coins"] = results
    return best


# ==============================================================================
# GESTIONE POSIZIONE (comune a ciclo normale e monitor di emergenza)
# ==============================================================================

def manage_open_position(state, price, atr_abs, ai_prob, final_score, now_str):
    """Aggiorna trailing, valuta chiusura. Ritorna (closed, log_lines)."""
    coin, sym = state["coin"], state.get("pionex_symbol", f"{state['coin']}_USDT")
    entry, up_b, low_b = state["entry_price"], state["upper"], state["lower"]
    stop_b = state["stop"]
    one_r = state.get("one_r_usd") or max(entry - state.get("initial_stop", stop_b), entry * 0.02)
    notional = state.get("notional_usdt") or 0.0
    high_p = max(state.get("highest_price", entry), price)
    state["highest_price"], state["last_price"] = high_p, price
    if atr_abs and atr_abs > 0:
        state["atr_abs"] = atr_abs
    atr_use = state.get("atr_abs") or 0.0

    pnl_pct = (price - entry) / entry * 100.0 if entry > 0 else 0.0
    cur_r = (price - entry) / one_r if one_r > 0 else 0.0
    pnl_usdt = notional * (pnl_pct - COST_PCT) / 100.0
    log = []

    if (cur_r >= 1.0 or pnl_pct >= 2.4) and not state.get("alert_1r_sent"):
        be = round_p(entry * 1.003)
        if stop_b < be:
            stop_b = state["stop"] = be
        state["trailing_locked"] = state["alert_1r_sent"] = True
        log.append(f"🔔 <b>+1R RAGGIUNTO ({coin})</b>: {pnl_pct:+.1f}% ({cur_r:+.1f}R), Stop a <b>Break-Even ${fmt_p(stop_b)}</b> 🔒")

    if cur_r >= 1.8 or pnl_pct >= 4.2 or state.get("alert_2r_sent"):
        trail = high_p - CHANDELIER_ATR_MULT * atr_use if atr_use > 0 else high_p * 0.976   # Chandelier ATR
        trail = round_p(min(trail, price * 0.999))
        if trail > stop_b:
            stop_b = state["stop"] = trail
            if not state.get("alert_2r_sent"):
                log.append(f"🔔 <b>+2R TRAILING CHANDELIER ({coin})</b>: massimo ${fmt_p(high_p)}, stop <b>${fmt_p(stop_b)}</b> 📈")
        state["alert_2r_sent"] = True

    reason = None
    if price <= stop_b:
        reason = f"🛑 STOP/TRAILING COLPITO (${fmt_p(price)} <= ${fmt_p(stop_b)})"
    elif price >= up_b:
        reason = f"🎯 TARGET/UPPER RANGE COLPITO (${fmt_p(price)} >= ${fmt_p(up_b)})"
    elif ai_prob is not None and final_score is not None and final_score <= 2 and ai_prob < 0.44:
        reason = f"🚨 USCITA D'EMERGENZA AI/MACRO (Voto {coin} {final_score}/10)"

    if reason:
        ok, note = execute_order("sell", sym, price, state.get("qty_coin", 0.0))
        if not ok:   # fix: se la vendita live fallisce la posizione resta APERTA e viene ritentata al prossimo ciclo
            state["pending_close_fail"] = state.get("pending_close_fail", 0) + 1
            log.append(f"❌ <b>CHIUSURA {coin} FALLITA</b> ({reason}) — {html.escape(note)}. Riprovo al prossimo ciclo (tentativi: {state['pending_close_fail']}). "
                       f"⚠️ CONTROLLA PIONEX A MANO!")
            return False, log
        trade = {"coin": coin, "opened_at": state.get("opened_at", "-"), "closed_at": now_str,
                 "entry_price": entry, "exit_price": price, "pnl_pct": round(pnl_pct - COST_PCT, 2),
                 "pnl_usdt": round(pnl_usdt, 2), "r_multiple": round(cur_r, 2), "reason": reason}
        hist = state.get("history", [])
        hist.insert(0, trade)
        state["history"] = hist[:60]
        state["active"] = False
        state["pending_close_fail"] = 0
        state["last_action"] = f"[{now_str}] Chiuso {coin} a ${fmt_p(price)} ({pnl_pct:+.2f}% / {cur_r:+.1f}R) - {reason}"
        log.append(f"🔔 <b>CHIUSURA AUTOMATICA {coin}/USDT</b>\n   • Motivo: {reason}\n"
                   f"   • ${fmt_p(entry)} ➡️ <b>${fmt_p(price)}</b>\n"
                   f"   • Risultato netto costi: <b>{pnl_pct - COST_PCT:+.2f}% ({pnl_usdt:+.2f} USDT | {cur_r:+.2f}R)</b>\n"
                   f"   • Esecuzione: {html.escape(note)}")
        return True, log

    log.append(f"🤖 <b>POSIZIONE ATTIVA {coin}/USDT</b> (aperta {state['opened_at']}, size {notional:.1f}$)\n"
               f"   • Entrata <b>${fmt_p(entry)}</b> | Ora <b>${fmt_p(price)}</b> (<b>{pnl_pct:+.2f}%</b> / {pnl_usdt:+.2f}$ / <b>{cur_r:+.2f}R</b>)\n"
               f"   • Range ${fmt_p(low_b)} – ${fmt_p(up_b)} | Stop <b>${fmt_p(stop_b)}</b>{' 🔒' if state.get('trailing_locked') else ''} | Target <b>${fmt_p(up_b)}</b>")
    return False, log


def execute_autonomous_cycle(best_a, news):
    state = load_bot_state()
    all_coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in all_coins}
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    now_str, today_str = now_rome.strftime("%d/%m %H:%M"), now_rome.strftime("%d/%m")
    log, changed = [], False

    if state["active"]:
        ac = state.get("coin", "SOL")
        cur = coin_map.get(ac)
        if cur:
            price, atr_abs, ai_p, fs = cur["price"], cur["atr_abs"], cur["ai_prob"], cur["final_score"]
        else:   # fix: prima si usava il prezzo di UN'ALTRA moneta come fallback
            ai_p = fs = None
            atr_abs = 0.0
            try:
                price = fetch_last_price(COIN_BY_SYMBOL[ac]["kraken"])
            except Exception as e:
                price = None
                log.append(f"⚠️ Prezzo {ac} non disponibile ({e}): posizione NON valutata in questo ciclo.")
        if price:
            was_active = state["active"]
            closed, lines = manage_open_position(state, price, atr_abs, ai_p, fs, now_str)
            log += lines
            changed = closed or (state["active"] != was_active)
    else:
        w = best_a
        rk, et, tm, ep = w["risk_check"], w["entry_timer"], w["time_machine"], w["exit_plan"]
        if w["final_score"] >= 7 and w["ai_prob"] >= 0.52 and rk["passed"] and et["triggered"]:
            bo = rk["bracket_order"]
            ok, note = execute_order("buy", w["pionex_symbol"], w["price"], bo["qty_coin"], amount_usdt=bo["order_usdt"])
            if ok:   # fix: lo stato diventa attivo SOLO se l'ordine è andato a buon fine
                state.update({
                    "active": True, "coin": w["coin"], "pionex_symbol": w["pionex_symbol"],
                    "entry_price": w["price"], "highest_price": w["price"], "last_price": w["price"],
                    "lower": w["lower"], "upper": w["upper"], "stop": w["stop_loss"], "initial_stop": w["stop_loss"],
                    "one_r_usd": ep["one_r_usd"], "grids": w["grids"], "qty_coin": bo["qty_coin"],
                    "notional_usdt": bo["order_usdt"], "atr_abs": w["atr_abs"],
                    "trailing_locked": False, "alert_1r_sent": False, "alert_2r_sent": False,
                    "pending_close_fail": 0, "opened_at": now_str, "opened_date": today_str,
                    "last_action": f"[{now_str}] Aperto {w['coin']}/USDT a ${fmt_p(w['price'])} (Voto {w['final_score']}/10 | {ep['rr_ratio']:.2f}R)"})
                changed = True
                log.append(f"🚀 <b>APERTURA {w['coin']}/USDT</b> (Voto {w['final_score']}/10 | {rk['status']})\n"
                           f"   • Setup: <b>{et['setup_name']}</b>\n"
                           f"   • Entry <b>${fmt_p(w['price'])}</b> | Stop <b>${fmt_p(w['stop_loss'])}</b> | Target <b>${fmt_p(w['upper'])}</b>\n"
                           f"   • Size <b>{bo['order_usdt']:.1f}$</b> ({bo['qty_coin']} {w['coin']}, Kelly-factor {rk['kelly_factor']:.2f}) | Rischio a stop <b>${rk['risk_usdt']:.2f}</b> (max ${rk['max_loss_usdt']:.2f})\n"
                           f"   • Time Machine: <b>{tm['win_or_grid_prob']:.0f}%</b> | E[R] <b>{tm['expected_r']:+.2f}R</b>\n"
                           f"   • Esecuzione: {html.escape(note)}")
            else:
                log.append(f"❌ <b>APERTURA {w['coin']} FALLITA</b>: {html.escape(note)} — nessuna posizione registrata.")
        else:
            log.append(f"⏳ <b>IN ATTESA DI SEGNALE CONFERMATO</b> (10 cripto)\n"
                       f"   • Top #1: <b>{w['coin']}/USDT</b> — Voto <b>{w['final_score']}/10</b> ({w['pts_100']:.0f}/100 | AI {w['ai_prob']*100:.0f}%)\n"
                       f"   • Entry Timer: <b>{et['status_txt']}</b>\n"
                       f"   • Risk Manager: <b>{rk['status']}</b> ({html.escape(rk['reason'])})\n"
                       f"   • Time Machine: <b>{tm['verdict']}</b> (E[R] {tm['expected_r']:+.2f}R)\n"
                       f"   • Ultima operazione: <i>{html.escape(state.get('last_action', 'Nessuna'))}</i>")

    save_bot_state(state)
    best_a["auto_state"], best_a["auto_log"], best_a["state_changed"] = state, log, changed
    return best_a["final_score"]


def emergency_position_monitor():
    """Se l'analisi completa fallisce (API giù) lo stop della posizione aperta va comunque controllato."""
    state = load_bot_state()
    if not state.get("active"):
        return []
    cfg = COIN_BY_SYMBOL.get(state["coin"])
    try:
        price = fetch_last_price(cfg["kraken"])
    except Exception as e:
        return [f"⚠️ Monitor emergenza: prezzo {state['coin']} non disponibile ({html.escape(str(e))}). Posizione NON valutata!"]
    now_str = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
    _, lines = manage_open_position(state, price, 0.0, None, None, now_str)
    save_bot_state(state)
    return ["🆘 <b>MONITOR DI EMERGENZA (analisi completa non disponibile)</b>"] + lines


# ==============================================================================
# DASHBOARD HTML
# ==============================================================================

def generate_webapp_dashboard(best_a, news, final_score):
    state = best_a.get("auto_state", load_bot_state())
    coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in coins}
    now_str = news["now_rome"].strftime("%d/%m/%Y %H:%M:%S")
    sc_col = "emerald" if final_score >= 7 else "amber" if final_score >= 5 else "rose"
    live = is_live()
    mode_badge = "LIVE TRADING PIONEX" if live else "SIMULAZIONE PAPER TRADING"
    mode_cls = ("bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if live
                else "bg-sky-500/20 text-sky-300 border-sky-500/40")
    tm, rk, ep, et = best_a["time_machine"], best_a["risk_check"], best_a["exit_plan"], best_a["entry_timer"]
    rk_col = "text-emerald-400" if rk["passed"] else "text-rose-400"
    esc = html.escape

    if state.get("active"):
        ac = state.get("coin", "SOL")
        cur_p = coin_map[ac]["price"] if ac in coin_map else state.get("last_price") or state["entry_price"]
        entry, low_b, up_b, stop_b = state["entry_price"], state["lower"], state["upper"], state["stop"]
        one_r = state.get("one_r_usd") or max(entry - stop_b, entry * 0.02)
        cur_r = (cur_p - entry) / one_r if one_r > 0 else 0.0
        pnl_pct = (cur_p - entry) / entry * 100.0 if entry > 0 else 0.0
        pnl_col = "text-emerald-400" if pnl_pct >= 0 else "text-rose-400"
        pos_pct = max(0.0, min(100.0, (cur_p - low_b) / max(up_b - low_b, 1e-8) * 100.0))
        box = lambda t, v, c="text-white": (f'<div class="bg-slate-900/70 p-4 rounded-xl"><div class="text-xs text-slate-400">{t}</div>'
                                            f'<div class="text-xl font-bold {c}">{v}</div></div>')
        pos_html = f"""
        <div class="bg-slate-800/90 border border-emerald-500/40 rounded-2xl p-6 shadow-xl">
          <div class="flex flex-wrap justify-between items-center gap-2 mb-4">
            <h2 class="text-xl font-bold text-white">🟢 POSIZIONE ATTIVA SU {ac}/USDT</h2>
            <span class="text-xs bg-slate-700 text-slate-300 px-3 py-1 rounded-full">Aperta il {esc(str(state.get('opened_at')))} · size {state.get('notional_usdt', 0):.1f}$</span>
          </div>
          <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            {box(f'Entrata ({ac})', '$' + fmt_p(entry))}{box('Prezzo Attuale', '$' + fmt_p(cur_p), 'text-sky-400')}
            {box('PnL (R)', f'{pnl_pct:+.2f}% ({cur_r:+.2f}R)', pnl_col)}{box('Stop dinamico', '$' + fmt_p(stop_b) + (' 🔒' if state.get('trailing_locked') else ''), 'text-amber-400')}
          </div>
          <div class="mb-2 flex justify-between text-xs text-slate-300"><span>Lower ${fmt_p(low_b)}</span><span>Range {pos_pct:.0f}%</span><span>Target ${fmt_p(up_b)}</span></div>
          <div class="w-full bg-slate-900 rounded-full h-4 p-0.5 border border-slate-700"><div class="bg-gradient-to-r from-sky-500 to-emerald-400 h-2.5 rounded-full" style="width:{pos_pct:.1f}%"></div></div>
        </div>"""
    else:
        pos_html = f"""
        <div class="bg-slate-800/80 border border-slate-700 rounded-2xl p-6 shadow-xl">
          <div class="flex flex-wrap justify-between gap-2 mb-2">
            <h2 class="text-lg font-bold text-slate-200">🟡 NESSUN TRADE APERTO — IN ATTESA DI SEGNALE + RISK PASS</h2>
            <span class="text-xs font-bold {rk_col}">Risk {best_a['coin']}: {rk['status']}</span></div>
          <p class="text-sm text-slate-400 mb-3">Setup su <b>{best_a['coin']}/USDT</b>: <b>{esc(et['setup_name'])}</b> ({esc(et['status_txt'])}).</p>
          <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs text-slate-300 bg-slate-900/70 p-4 rounded-xl border border-slate-700/60">
            <div><b>🕰️ Time Machine:</b><br>Successo {tm['win_or_grid_prob']:.1f}% · Stop first {tm['stop_first_prob']:.1f}%<br>E[R] <b>{tm['expected_r']:+.2f}R</b></div>
            <div><b>📐 Exit Planner:</b><br>Entry ${fmt_p(best_a['price'])} · Stop ${fmt_p(best_a['stop_loss'])}<br>Target ${fmt_p(best_a['upper'])} · R:R {ep['rr_ratio']:.2f}R</div>
            <div><b>🛡️ Risk Manager:</b><br>Size {rk['bracket_order']['order_usdt']:.1f}$ · Rischio ${rk['risk_usdt']:.2f} (max ${rk['max_loss_usdt']:.2f})<br>{esc(rk['reason'])}</div>
          </div></div>"""

    rows = ""
    for idx, c in enumerate(coins, 1):
        sc = c["final_score"]
        badge = ("bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if sc >= 7 else
                 "bg-amber-500/20 text-amber-300 border-amber-500/40" if sc >= 5 else "bg-rose-500/20 text-rose-300 border-rose-500/40")
        chg = "text-emerald-400" if c["chg24"] >= 0 else "text-rose-400"
        rows += f"""<tr class="border-b border-slate-800/80 text-xs hover:bg-slate-800/40">
          <td class="py-3 px-3 font-bold text-white">{'👑 ' if idx == 1 else f'#{idx} '}{c['coin']}/USDT</td>
          <td class="py-3 px-3 text-slate-200">${fmt_p(c['price'])} <span class="{chg}">({c['chg24']:+.1f}%)</span></td>
          <td class="py-3 px-3 text-sky-400 font-bold">{c['ai_prob']*100:.0f}%</td>
          <td class="py-3 px-3 text-purple-300">{c['time_machine']['expected_r']:+.2f}R ({c['time_machine']['win_or_grid_prob']:.0f}%)</td>
          <td class="py-3 px-3 text-slate-300">{c['exit_plan']['rr_ratio']:.2f}R</td>
          <td class="py-3 px-3 text-slate-300">{'PASS ✅' if c['risk_check']['passed'] else 'BLOCK ⛔'}</td>
          <td class="py-3 px-3"><span class="px-2.5 py-1 rounded-full border font-bold {badge}">{sc}/10 ({c['pts_100']:.0f}pt)</span></td></tr>"""

    pillars = "".join(f'<li class="bg-slate-900/60 border border-slate-700/60 rounded-xl p-3 text-sm text-slate-200">{b}</li>'
                      for b in best_a["breakdown"])
    hist = ""
    for h in state.get("history", [])[:30]:
        c = "text-emerald-400" if h.get("pnl_pct", 0) >= 0 else "text-rose-400"
        hist += f"""<tr class="border-b border-slate-800 text-xs">
          <td class="py-2.5 px-3 font-bold text-white">{esc(str(h.get('coin', 'SOL')))}/USDT</td>
          <td class="py-2.5 px-3">{esc(str(h.get('opened_at')))} &rarr; {esc(str(h.get('closed_at')))}</td>
          <td class="py-2.5 px-3">${fmt_p(h.get('entry_price', 0))} &rarr; ${fmt_p(h.get('exit_price', 0))}</td>
          <td class="py-2.5 px-3 font-bold {c}">{h.get('pnl_pct', 0):+.2f}% ({h.get('pnl_usdt', 0):+.2f}$ | {h.get('r_multiple', 0):+.1f}R)</td>
          <td class="py-2.5 px-3 text-slate-400">{esc(h.get('reason', ''))}</td></tr>"""
    if not hist:
        hist = '<tr><td colspan="5" class="py-4 text-center text-slate-500 text-sm">Nessun trade chiuso.</td></tr>'
    ai = best_a["ai_res"]

    doc = f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Coin Quant AI Bot v2</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans p-4 md:p-8"><div class="max-w-6xl mx-auto space-y-6">
  <div class="flex flex-wrap justify-between items-center gap-4 bg-slate-900/90 border border-slate-800 p-5 rounded-2xl">
    <div><h1 class="text-xl md:text-2xl font-extrabold text-white">🤖 Multi-Coin AI Quant Bot v2</h1>
    <p class="text-xs text-slate-400 mt-1">Aggiornato: <b>{now_str}</b> · 10 cripto · Monte Carlo 1.500 sim · Risk Manager</p></div>
    <span class="text-xs font-bold px-3.5 py-1.5 rounded-full border {mode_cls}">{mode_badge}</span></div>
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="text-xs text-slate-400">👑 Vincitrice</div><div class="text-2xl font-black text-white mt-1">{best_a['coin']}/USDT</div><div class="text-xs text-slate-400 mt-1">${fmt_p(best_a['price'])} ({best_a['chg24']:+.2f}%)</div></div>
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="text-xs text-slate-400">Voto Quant &amp; Risk</div><div class="text-2xl font-black text-{sc_col}-400 mt-1">{final_score}/10 <span class="text-sm font-normal text-slate-400">({best_a['pts_100']:.0f}/100)</span></div><div class="text-xs {rk_col} mt-1 font-bold">{rk['status']}</div></div>
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="text-xs text-slate-400">🕰️ Time Machine</div><div class="text-2xl font-black text-purple-400 mt-1">{tm['expected_r']:+.2f}R</div><div class="text-xs text-slate-400 mt-1">Win {tm['win_or_grid_prob']:.0f}% · Stop {tm['stop_first_prob']:.0f}%</div></div>
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="text-xs text-slate-400">🧠 AI (validata OOS)</div><div class="text-2xl font-black text-sky-400 mt-1">{best_a['ai_prob']*100:.1f}%</div><div class="text-xs text-slate-400 mt-1">skill {ai['oos_skill']:+.3f} · peso {ai['shrink']:.2f}</div></div></div>
  {pos_html}
  <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6"><h3 class="text-base font-bold text-white mb-4">🏆 Classifica Live</h3><div class="overflow-x-auto"><table class="w-full text-left border-collapse">
    <thead><tr class="border-b border-slate-700 text-xs text-slate-400"><th class="py-2 px-3">Moneta</th><th class="py-2 px-3">Prezzo</th><th class="py-2 px-3">AI</th><th class="py-2 px-3">E[R]</th><th class="py-2 px-3">R:R</th><th class="py-2 px-3">Risk</th><th class="py-2 px-3">Voto</th></tr></thead><tbody>{rows}</tbody></table></div></div>
  <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6"><h3 class="text-base font-bold text-white mb-4">📊 Pagella 10 Pilastri — 👑 {best_a['coin']}/USDT</h3><ul class="grid grid-cols-1 md:grid-cols-2 gap-3">{pillars}</ul></div>
  <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6"><h3 class="text-base font-bold text-white mb-4">📜 Storico (netto costi)</h3><div class="overflow-x-auto"><table class="w-full text-left border-collapse">
    <thead><tr class="border-b border-slate-700 text-xs text-slate-400"><th class="py-2 px-3">Moneta</th><th class="py-2 px-3">Periodo</th><th class="py-2 px-3">In/Out</th><th class="py-2 px-3">Risultato</th><th class="py-2 px-3">Motivo</th></tr></thead><tbody>{hist}</tbody></table></div></div>
</div></body></html>"""
    try:
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(doc)
    except Exception as e:
        print("Errore creazione index.html:", e)


# ==============================================================================
# REPORT TELEGRAM
# ==============================================================================

def score_bar(score):
    f = max(1, min(10, score))
    return ("🟢" if f >= 7 else "🟡" if f >= 5 else "🔴") * f + "⚪" * (10 - f)


def format_news_section(news, nr):
    lines = ["📰 <b>CALENDARIO MACRO USA &amp; NYSE BELL</b>"]
    if news["status"] == "NO_DATA":
        lines.append("⚠️ Calendario macro non raggiungibile (filtro news NON attivo).")
        return "\n".join(lines)
    if news["us_open_warning"]:
        lines.append("🔔 <b>NYSE Opening Bell (09:30 NY)</b>: finestra di alta volatilità.")
    if news["imminent"]:
        for ev in news["imminent"]:
            lines.append(f"🚨 <b>TRA {max(1, int(round(ev['diff_min'])))} MIN ({ev['time_str']})</b>: "
                         f"{'🔴' if ev['impact'] == 'High' else '🟠'} <b>{html.escape(ev['title'])}</b>")
    elif news["recent"]:
        for ev in news["recent"]:
            act = f" | Dati: <b>{html.escape(ev['actual'])}</b> (Prev: {html.escape(ev['forecast'] or '-')})" if ev["actual"] else ""
            lines.append(f"⏳ <b>USCITA {max(1, int(round(abs(ev['diff_min']))))} MIN FA ({ev['time_str']})</b>: <b>{html.escape(ev['title'])}</b>{act}")
        lines.append(f"{'🟢' if nr['calm'] else '🔴'} <b>Reazione post-news</b>: {'CALMO' if nr['calm'] else 'AGITATO'} (15m: {nr['range_15m_pct']:.2f}%)")
    else:
        lines.append("✅ <b>Nessuna notizia macro bloccante.</b>")
    ne = news["next_event"]
    if ne and not news["imminent"]:
        h, m = int(ne["diff_min"] // 60), int(ne["diff_min"] % 60)
        lines.append(f"🗓️ Prossimo: {'🔴' if ne['impact'] == 'High' else '🟠'} <b>{html.escape(ne['title'])}</b> "
                     f"({ne['date_str']} {ne['time_str']}, {'tra %dh %dm' % (h, m) if h else 'tra %d min' % m})")
    return "\n".join(lines)


def format_message(best_a, news):
    now = news["now_rome"].strftime("%d/%m/%Y %H:%M")
    nr, fs = best_a["news_reaction"], best_a["final_score"]
    coins = best_a.get("all_coins", [best_a])
    tm, rk, ep, et, ai = best_a["time_machine"], best_a["risk_check"], best_a["exit_plan"], best_a["entry_timer"], best_a["ai_res"]

    override = None
    if news["status"] == "IMMINENT":
        override = ("⛔ <b>BLOCCO NEWS</b>: notizia USA ad ALTO impatto imminente!"
                    if any(e["impact"] == "High" for e in news["imminent"]) else "⚠️ <b>PRUDENZA NEWS</b>: notizia a medio impatto imminente.")
    elif news["status"] == "COOLDOWN" and not nr["calm"]:
        override = "⏳ <b>COOLDOWN NEWS</b>: notizia appena uscita e mercato instabile."
    elif news["us_open_warning"] and not nr["calm"]:
        override = "⚠️ <b>NYSE OPENING BELL</b>: volatilità elevata."

    c = best_a["coin"]
    if fs >= 8 and rk["passed"]:
        verdict = f"🟢 <b>SEGNALE FORTE SU {c}/USDT</b>"
    elif fs == 7 and rk["passed"]:
        verdict = f"🟢 <b>SEGNALE POSITIVO SU {c}/USDT</b>"
    elif fs == 6:
        verdict = f"🟡 <b>QUASI PRONTO ({c} 6/10): ATTESA CONSIGLIATA</b>"
    elif fs == 5:
        verdict = "🟡 <b>NEUTRALE (5/10): MERCATO INCERTO O BLOCCO RISK MANAGER</b>"
    elif fs >= 3:
        verdict = "🟠 <b>SCONSIGLIATO: CONDIZIONI SFAVOREVOLI</b>"
    else:
        verdict = "🔴 <b>PERICOLO / BLOCCO: NON APRIRE ADESSO</b>"

    rank = ["🏆 <b>CLASSIFICA AI + TIME MACHINE</b>"]
    for i, x in enumerate(coins, 1):
        s = x["final_score"]
        rank.append(f"{'👑' if i == 1 else f'{i}.'} {'🟢' if s >= 7 else '🟡' if s >= 5 else '🔴'} <b>{x['coin']}</b>: <b>{s}/10</b> ({x['pts_100']:.0f}pt) | "
                    f"E[R] <b>{x['time_machine']['expected_r']:+.2f}R</b> | AI <b>{x['ai_prob']*100:.0f}%</b> | Risk {'✅' if x['risk_check']['passed'] else '⛔'}")

    veto = list(best_a["veto_reasons"]) + ([override] if override else [])
    veto_txt = ("\n\n🚨 <b>FILTRI DI SICUREZZA &amp; RISK MANAGER</b>\n" + "\n".join(veto)) if veto else ""
    mode = "🟢 LIVE PIONEX" if is_live() else "🧪 PAPER TRADING"
    bo = rk["bracket_order"]
    return (
        f"🤖 <b>MULTI-COIN AI BOT v2 — TIME MACHINE &amp; RISK MANAGER</b>\n🕐 {now} (ora italiana) | <b>{mode}</b>\n\n"
        f"{verdict}\n👑 <b>Migliore: {c}/USDT ({best_a['name']})</b>\n📊 <b>Voto: {fs}/10</b> (Quant {best_a['pts_100']:.0f}/100)\n{score_bar(fs)}\n\n"
        f"⚙️ <b>GESTIONE AUTONOMA &amp; ALERTS</b>\n" + "\n".join(best_a.get("auto_log", [])) + "\n\n"
        f"🕰️ <b>TIME MACHINE &amp; RISK GATEKEEPER ({c})</b>\n"
        f"• Setup: <b>{et['setup_name']}</b> ({et['status_txt']})\n"
        f"• Prob. profitto: <b>{tm['win_or_grid_prob']:.1f}%</b> | Stop first: <b>{tm['stop_first_prob']:.1f}%</b>\n"
        f"• E[R] netto costi: <b>{tm['expected_r']:+.2f}R</b> ({tm['verdict']})\n"
        f"• AI: skill OOS <b>{ai['oos_skill']:+.3f}</b> → peso {ai['shrink']:.2f} (campioni {ai['samples']})\n"
        f"• Risk: <b>{rk['status']}</b> — Size <b>{bo['order_usdt']:.1f}$</b> | Rischio <b>${rk['risk_usdt']:.2f}</b> (max ${rk['max_loss_usdt']:.2f}) | R:R <b>{ep['rr_ratio']:.2f}R</b> (dir. {ep['rr_directional']:.2f}R)\n"
        f"• Drawdown storico <b>{rk['drawdown_pct']:.1f}%</b> (kill-switch {KILL_SWITCH_DRAWDOWN_PCT:.0f}%)\n\n"
        + "\n".join(rank) + "\n\n" + format_news_section(news, nr) + "\n\n"
        f"⚙️ <b>PIANO PER 👑 {c}/USDT ({INVEST_EUR:g}€ ≈ {best_a['invest_usdt']:.2f} USDT)</b>\n"
        f"• Entrata <b>${fmt_p(best_a['price'])}</b> (Qty {bo['qty_coin']} {c})\n"
        f"• Range <b>${fmt_p(best_a['lower'])} – ${fmt_p(best_a['upper'])}</b> ({best_a['grids']} griglie, netto/grid +{best_a['net_step_pct']:.2f}%)\n"
        f"• Alert +1R: <b>${fmt_p(ep['level_1r'])}</b> | Stop: <b>${fmt_p(best_a['stop_loss'])}</b>\n\n"
        f"📋 <b>PAGELLA 10 PILASTRI ({c}/USDT)</b>\n" + "\n".join(f"• {b}" for b in best_a["breakdown"]) + veto_txt
    )


def _split_message(msg, limit=3800):
    chunks, cur = [], ""
    for line in msg.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for part in _split_message(msg):          # fix: limite Telegram 4096 caratteri
        try:
            r = SESSION.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": part, "parse_mode": "HTML"}, timeout=20)
            if r.status_code != 200:
                plain = re.sub(r"<[^>]+>", "", html.unescape(part))
                SESSION.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": plain}, timeout=20)
        except Exception as e:
            print("Errore invio Telegram:", e)
        time.sleep(0.4)


def send_telegram_webapp(final_score, best_a):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not os.path.exists("index.html"):
        return
    state = best_a.get("auto_state", {})
    due = (time.time() - float(state.get("last_dashboard_ts", 0))) >= DASHBOARD_EVERY_HOURS * 3600
    if not (best_a.get("state_changed") or due):    # fix: evita di spedire l'HTML ad ogni esecuzione
        return
    stato = (f"🟢 TRADE ATTIVO SU {state.get('coin')}/USDT" if state.get("active")
             else f"⏳ IN ATTESA (Top AI: {best_a['coin']} {final_score}/10)")
    try:
        with open("index.html", "rb") as f:
            r = SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                             data={"chat_id": TELEGRAM_CHAT_ID, "caption": f"🖥️ Dashboard Multi-Coin AI v2 | {stato}"},
                             files={"document": ("Dashboard_AI_Bot.html", f, "text/html")}, timeout=25)
        if r.status_code == 200:
            st = load_bot_state()
            st["last_dashboard_ts"] = time.time()
            save_bot_state(st)
    except Exception as e:
        print("Errore invio WebApp Telegram:", e)


def main():
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    try:
        news = fetch_macro_calendar()
    except Exception as e:
        news = empty_news(now_rome, str(e))

    final_score, best = 0, None
    try:
        best = scan_all_coins(news)
        final_score = execute_autonomous_cycle(best, news)
        generate_webapp_dashboard(best, news, final_score)
        msg = format_message(best, news)
    except Exception as e:
        emergency = []
        try:
            emergency = emergency_position_monitor()
        except Exception as e2:
            emergency = [f"⚠️ Anche il monitor di emergenza è fallito: {html.escape(str(e2))}"]
        msg = (f"⚠️ <b>MULTI-COIN AI BOT v2</b>\n🕐 {now_rome.strftime('%d/%m/%Y %H:%M')} (ora italiana)\n\n"
               f"Errore analisi ({type(e).__name__}): {html.escape(str(e))}\n\n" + "\n".join(emergency))

    send_telegram(msg)
    if best is not None:
        send_telegram_webapp(final_score, best)


if __name__ == "__main__":
    main()
