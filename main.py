import os
import re
import json
import math
import time
import hmac
import hashlib
import html
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

INVEST_EUR = 50
MIN_ORDER_USDT = 5.0
BTC_PAIR = "XBTUSD"

# Paniere di 10 Criptovalute Top scansionate dall'AI ad ogni ciclo
COIN_UNIVERSE = [
    {"coin": "SOL",  "name": "Solana",    "kraken": "SOLUSD",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT"},
    {"coin": "BTC",  "name": "Bitcoin",   "kraken": "XBTUSD",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT"},
    {"coin": "ETH",  "name": "Ethereum",  "kraken": "ETHUSD",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT"},
    {"coin": "XRP",  "name": "Ripple",    "kraken": "XRPUSD",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT"},
    {"coin": "SUI",  "name": "Sui",       "kraken": "SUIUSD",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT"},
    {"coin": "DOGE", "name": "Dogecoin",  "kraken": "XDGUSD",  "futures": "PF_DOGEUSD", "pionex": "DOGE_USDT"},
    {"coin": "ADA",  "name": "Cardano",   "kraken": "ADAUSD",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT"},
    {"coin": "AVAX", "name": "Avalanche", "kraken": "AVAXUSD", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT"},
    {"coin": "LINK", "name": "Chainlink", "kraken": "LINKUSD", "futures": "PF_LINKUSD", "pionex": "LINK_USDT"},
    {"coin": "DOT",  "name": "Polkadot",  "kraken": "DOTUSD",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT"},
]

# Range attivo fallback (gestisce anche Secrets vuoti su GitHub Actions)
ACTIVE_LOWER = float(os.getenv("ACTIVE_LOWER") or "0")
ACTIVE_UPPER = float(os.getenv("ACTIVE_UPPER") or "0")

# Configurazione Esecuzione Autonoma (Paper Trading / Live Pionex)
AUTO_TRADE_ENABLED = (os.getenv("AUTO_TRADE_ENABLED") or "false").strip().lower() == "true"
PIONEX_WEBHOOK_URL = os.getenv("PIONEX_WEBHOOK_URL") or ""
PIONEX_SIGNAL_TOKEN = os.getenv("PIONEX_SIGNAL_TOKEN") or ""
PIONEX_API_KEY = os.getenv("PIONEX_API_KEY") or ""
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET") or ""
STATE_FILE = "bot_state.json"

# Passo griglia target e commissioni Pionex (0.05% buy + 0.05% sell = 0.10%)
TARGET_STEP_PCT = 0.32
PIONEX_ROUNDTRIP_FEE_PCT = 0.10
MIN_NET_STEP_PCT = 0.15

# Finestre di sicurezza calendario macro
NEWS_BLOCK_BEFORE_MIN = 60
NEWS_COOLDOWN_MIN = 30
NEWS_SURPRISE_COOLDOWN_MIN = 90
NEWS_LOOKAHEAD_HOURS = 12

# Soglie per verificare se una notizia ha scosso il mercato (candele 5m)
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
# UTILITÀ FORMATTAZIONE PREZZI MULTI-DECIMALE (Per BTC, SOL, XRP, DOGE, ecc.)
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
    d = price_decimals(val)
    return f"{float(val):.{d}f}"


# ==============================================================================
# GESTIONE STATO AUTONOMO ED ESECUZIONE ORDINI (PIONEX / PAPER TRADING)
# ==============================================================================

def load_bot_state():
    default_state = {
        "active": False,
        "coin": "SOL",
        "pionex_symbol": "SOL_USDT",
        "entry_price": 0.0,
        "highest_price": 0.0,
        "lower": 0.0,
        "upper": 0.0,
        "stop": 0.0,
        "grids": 0,
        "qty_coin": 0.0,
        "trailing_locked": False,
        "opened_at": "",
        "last_action": "Nessuna azione",
        "history": [],
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
                if "qty_sol" in data and not data.get("qty_coin"):
                    default_state["qty_coin"] = data["qty_sol"]
        except Exception:
            pass
    return default_state


def save_bot_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("Errore salvataggio stato:", e)


def trigger_pionex_webhook(action, pionex_symbol, price, qty_coin):
    """Invia segnale buy/sell al Signal Bot / Webhook di Pionex."""
    if not AUTO_TRADE_ENABLED or not PIONEX_WEBHOOK_URL:
        return False, "Paper Trading (Simulazione)"
    payload = {
        "token": PIONEX_SIGNAL_TOKEN,
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": pionex_symbol,
        "action": action.lower(),
        "price": fmt_p(price),
        "contracts": str(round(qty_coin, 4)),
        "position_size": str(round(qty_coin, 4)) if action.lower() == "buy" else "0",
    }
    try:
        r = requests.post(PIONEX_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code == 200:
            return True, f"Segnale Webhook Pionex ({pionex_symbol}) inviato ✅"
        return False, f"Errore Webhook Pionex ({r.status_code})"
    except Exception as e:
        return False, f"Eccezione Webhook: {e}"


def pionex_spot_market_order(side, pionex_symbol, amount_usdt=None, size_coin=None):
    """Esegue un ordine Market diretto tramite Pionex Spot API (HMAC-SHA256)."""
    if not AUTO_TRADE_ENABLED or not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return False, "Paper Trading (Simulazione)"

    base_url = "https://api.pionex.com"
    path = "/api/v1/trade/order"
    ts = str(int(time.time() * 1000))
    query = f"timestamp={ts}"

    body_dict = {
        "symbol": pionex_symbol,
        "side": side.upper(),
        "type": "MARKET",
    }
    if side.upper() == "BUY" and amount_usdt:
        body_dict["amount"] = str(round(amount_usdt, 2))
    elif side.upper() == "SELL" and size_coin:
        body_dict["size"] = str(round(size_coin, 4))

    body_str = json.dumps(body_dict, separators=(",", ":"))
    sign_str = f"POST{path}?{query}{body_str}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(f"{base_url}{path}?{query}", headers=headers, data=body_str, timeout=15)
        res = r.json()
        if res.get("result"):
            return True, f"Ordine API Pionex {side.upper()} su {pionex_symbol} eseguito ✅"
        return False, f"Errore API Pionex: {res.get('message', 'Sconosciuto')}"
    except Exception as e:
        return False, f"Eccezione API Pionex: {e}"


# ==============================================================================
# CALENDARIO MACROECONOMICO USA
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
    for kw in HIGH_KEYWORDS:
        if kw.lower() in t_low:
            return "High"
    for kw in MEDIUM_KEYWORDS:
        if kw.lower() in t_low:
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
    try:
        clean_time = (time_str or "").strip().lower()
        if not clean_time or "all day" in clean_time or "tentative" in clean_time:
            return None
        combined = f"{s} {clean_time.upper()}"
        for fmt in ("%m-%d-%Y %I:%M%p", "%Y-%m-%d %I:%M%p", "%b %d, %Y %I:%M%p"):
            try:
                naive = datetime.strptime(combined, fmt)
                ny_dt = naive.replace(tzinfo=ZoneInfo("America/New_York"))
                return ny_dt.astimezone(timezone.utc)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def fetch_raw_calendar():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, application/xml, text/xml, */*",
    }
    last_err = None
    for url in FF_URLS:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and r.text.strip().startswith("["):
                return r.json(), None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)

    xml_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    try:
        r = requests.get(xml_url, headers=headers, timeout=15)
        if r.status_code == 200 and "<weeklyevents" in r.text:
            root = ET.fromstring(r.text)
            items = []
            for ev in root.findall(".//event"):
                items.append({
                    "title": (ev.findtext("title") or "").strip(),
                    "country": (ev.findtext("country") or "").strip(),
                    "date": (ev.findtext("date") or "").strip(),
                    "time": (ev.findtext("time") or "").strip(),
                    "impact": (ev.findtext("impact") or "").strip(),
                    "forecast": (ev.findtext("forecast") or "").strip(),
                    "previous": (ev.findtext("previous") or "").strip(),
                    "actual": (ev.findtext("actual") or "").strip(),
                })
            return items, None
    except Exception as e:
        last_err = str(e)

    return None, last_err or "Calendario non raggiungibile"


def is_us_market_open_window(now_utc):
    ny_time = now_utc.astimezone(ZoneInfo("America/New_York"))
    if ny_time.weekday() >= 5:
        return False
    minutes_from_midnight = ny_time.hour * 60 + ny_time.minute
    return (9 * 60 + 20) <= minutes_from_midnight <= (10 * 60 + 5)


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    rome_tz = ZoneInfo("Europe/Rome")
    now_rome = now_utc.astimezone(rome_tz)

    raw_items, err = fetch_raw_calendar()
    us_open_warning = is_us_market_open_window(now_utc)

    if raw_items is None:
        return {
            "status": "NO_DATA",
            "events": [],
            "imminent": [],
            "recent": [],
            "today_events": [],
            "next_event": None,
            "surprise_detected": False,
            "surprise_details": [],
            "us_open_warning": us_open_warning,
            "now_rome": now_rome,
            "error": err,
        }

    relevant_events = []
    imminent = []
    recent = []
    today_events = []
    surprise_detected = False
    surprise_details = []

    for item in raw_items:
        country = (item.get("country") or "").strip().upper()
        if country != "USD":
            continue

        title = (item.get("title") or "").strip()
        impact = classify_event_impact(title, item.get("impact"))
        if impact not in ("High", "Medium"):
            continue

        ev_dt_utc = parse_event_datetime(item.get("date"), item.get("time"))
        if ev_dt_utc is None:
            continue

        ev_dt_rome = ev_dt_utc.astimezone(rome_tz)
        diff_min = (ev_dt_utc - now_utc).total_seconds() / 60.0

        actual_str = str(item.get("actual") or "").strip()
        forecast_str = str(item.get("forecast") or "").strip()
        previous_str = str(item.get("previous") or "").strip()

        act_val = parse_num(actual_str)
        for_val = parse_num(forecast_str)
        prev_val = parse_num(previous_str)

        is_surprise = False
        surprise_txt = ""
        if act_val is not None and for_val is not None:
            denom = abs(for_val) if abs(for_val) > 0.05 else 1.0
            rel_diff = abs(act_val - for_val) / denom
            abs_diff = abs(act_val - for_val)
            if (rel_diff >= 0.10 and abs_diff >= 0.1) or abs_diff >= 0.25:
                is_surprise = True
                surprise_txt = f"Attuale {actual_str} vs Previsto {forecast_str}"
        elif act_val is not None and prev_val is not None and for_val is None:
            denom = abs(prev_val) if abs(prev_val) > 0.05 else 1.0
            if abs(act_val - prev_val) / denom >= 0.15:
                is_surprise = True
                surprise_txt = f"Attuale {actual_str} vs Precedente {previous_str}"

        ev_obj = {
            "title": translate_title(title),
            "raw_title": title,
            "impact": impact,
            "dt_utc": ev_dt_utc,
            "dt_rome": ev_dt_rome,
            "time_str": ev_dt_rome.strftime("%H:%M"),
            "date_str": ev_dt_rome.strftime("%d/%m"),
            "diff_min": diff_min,
            "actual": actual_str,
            "forecast": forecast_str,
            "previous": previous_str,
            "is_surprise": is_surprise,
            "surprise_txt": surprise_txt,
        }

        if ev_dt_rome.date() == now_rome.date():
            today_events.append(ev_obj)

        if -180 <= diff_min <= (NEWS_LOOKAHEAD_HOURS * 60):
            relevant_events.append(ev_obj)

        block_before = NEWS_BLOCK_BEFORE_MIN if impact == "High" else 30
        if 0 <= diff_min <= block_before:
            imminent.append(ev_obj)

        cooldown_window = NEWS_SURPRISE_COOLDOWN_MIN if (impact == "High" and is_surprise) else NEWS_COOLDOWN_MIN
        if -cooldown_window <= diff_min < 0:
            recent.append(ev_obj)
            if is_surprise and impact == "High":
                surprise_detected = True
                surprise_details.append(f"{ev_obj['title']}: {surprise_txt}")

    relevant_events.sort(key=lambda x: x["dt_utc"])
    today_events.sort(key=lambda x: x["dt_utc"])

    future_events = [e for e in relevant_events if e["diff_min"] >= 0]
    next_event = future_events[0] if future_events else None

    if imminent:
        status = "IMMINENT"
    elif recent:
        status = "COOLDOWN"
    else:
        status = "CLEAR"

    return {
        "status": status,
        "events": relevant_events,
        "imminent": imminent,
        "recent": recent,
        "today_events": today_events,
        "next_event": next_event,
        "surprise_detected": surprise_detected,
        "surprise_details": surprise_details,
        "us_open_warning": us_open_warning,
        "now_rome": now_rome,
        "error": None,
    }


def check_market_news_reaction(candles_5m):
    if not candles_5m or len(candles_5m) < 4:
        return {"calm": True, "range_15m_pct": 0.4, "move_15m_pct": 0.3, "vol_spike": False}

    recent_3 = candles_5m[-3:]
    highs = [float(c[2]) for c in recent_3]
    lows = [float(c[3]) for c in recent_3]
    opens = [float(c[1]) for c in recent_3]
    closes = [float(c[4]) for c in recent_3]

    last_close = closes[-1]
    range_15m_pct = ((max(highs) - min(lows)) / last_close) * 100.0
    move_15m_pct = (abs(last_close - opens[0]) / opens[0]) * 100.0

    all_vols = [float(c[6]) for c in candles_5m[-24:-3]]
    avg_vol = sum(all_vols) / len(all_vols) if all_vols else 1.0
    recent_vol = sum(float(c[6]) for c in recent_3) / 3.0
    vol_spike = recent_vol > (avg_vol * 2.3)

    calm = (
        range_15m_pct <= NEWS_MAX_15M_RANGE_PCT
        and move_15m_pct <= NEWS_MAX_15M_MOVE_PCT
        and not vol_spike
    )
    return {
        "calm": calm,
        "range_15m_pct": range_15m_pct,
        "move_15m_pct": move_15m_pct,
        "vol_spike": vol_spike,
    }


# ==============================================================================
# INDICATORI MATEMATICI, ECONOMETRICI E QUANTITATIVI
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
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    first_rsi = 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    out = [50.0] * n + [first_rsi]
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        rs = avg_g / avg_l if avg_l != 0 else 999.0
        out.append(100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + rs)))
    return out


def stoch_rsi(closes, rsi_len=14, stoch_len=14):
    rsis = rsi_series(closes, rsi_len)
    if len(rsis) < stoch_len:
        return 50.0
    window = rsis[-stoch_len:]
    lo, hi = min(window), max(window)
    if hi - lo < 1e-9:
        return 50.0
    return ((rsis[-1] - lo) / (hi - lo)) * 100.0


def macd_hist_series(closes, fast=12, slow=26, sig=9):
    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    sig_line = ema_series(macd_line, sig)
    return [m - s for m, s in zip(macd_line, sig_line)]


def bollinger(closes, n=20, mult=2.0):
    mid = sma(closes, n)
    sd = stdev(closes, n)
    up = mid + mult * sd
    lo = mid - mult * sd
    width_pct = ((up - lo) / mid) * 100.0 if mid else 0.0
    pct_b = ((closes[-1] - lo) / (up - lo)) * 100.0 if (up - lo) > 0 else 50.0
    return lo, mid, up, width_pct, pct_b


def atr_series(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < n:
        return trs
    out = []
    cur = sum(trs[:n]) / n
    out.append(cur)
    for tr in trs[n:]:
        cur = (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def adx_di(highs, lows, closes, n=14):
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    atr = sum(tr_list[:n])
    pdm = sum(plus_dm[:n])
    mdm = sum(minus_dm[:n])
    dx_list = []
    pdi, mdi = 0.0, 0.0

    for i in range(n, len(tr_list)):
        atr = atr - (atr / n) + tr_list[i]
        pdm = pdm - (pdm / n) + plus_dm[i]
        mdm = mdm - (mdm / n) + minus_dm[i]
        pdi = 100.0 * (pdm / atr) if atr else 0.0
        mdi = 100.0 * (mdm / atr) if atr else 0.0
        denom = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)

    if len(dx_list) < n:
        return (sum(dx_list) / max(len(dx_list), 1)), pdi, mdi

    adx_val = sum(dx_list[:n]) / n
    for dx in dx_list[n:]:
        adx_val = ((adx_val * (n - 1)) + dx) / n
    return adx_val, pdi, mdi


def hurst_exponent(closes, max_lag=24):
    if len(closes) < 120:
        return 0.50
    log_p = [math.log(max(c, 1e-8)) for c in closes[-140:]]
    lags = [2, 4, 8, 12, 16, 24]
    x_vals, y_vals = [], []
    for lag in lags:
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        if len(diffs) < 2:
            continue
        m = sum(diffs) / len(diffs)
        sd = math.sqrt(sum((d - m) ** 2 for d in diffs) / len(diffs))
        if sd > 1e-12:
            x_vals.append(math.log(lag))
            y_vals.append(math.log(sd))
    if len(x_vals) < 3:
        return 0.50
    mx = sum(x_vals) / len(x_vals)
    my = sum(y_vals) / len(y_vals)
    num = sum((x - mx) * (y - my) for x, y in zip(x_vals, y_vals))
    den = sum((x - mx) ** 2 for x in x_vals)
    h = num / den if den > 1e-12 else 0.50
    return max(0.20, min(0.80, h))


def variance_ratio_mr(closes, lag=6):
    if len(closes) < 80:
        return 1.0
    rets1 = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rets_k = [math.log(closes[i] / closes[i - lag]) for i in range(lag, len(closes))]
    m1 = sum(rets1) / len(rets1)
    mk = sum(rets_k) / len(rets_k)
    var1 = sum((r - m1) ** 2 for r in rets1) / max(len(rets1) - 1, 1)
    vark = sum((r - mk) ** 2 for r in rets_k) / max(len(rets_k) - 1, 1)
    if var1 <= 1e-12:
        return 1.0
    return vark / (lag * var1)


def vwap_rolling(highs, lows, closes, vols, period=24):
    h_sub = highs[-period:]
    l_sub = lows[-period:]
    c_sub = closes[-period:]
    v_sub = vols[-period:]
    pv_sum = 0.0
    v_sum = 0.0
    for h, l, c, v in zip(h_sub, l_sub, c_sub, v_sub):
        typ = (h + l + c) / 3.0
        pv_sum += typ * v
        v_sum += v
    return pv_sum / v_sum if v_sum > 0 else closes[-1]


# ==============================================================================
# FONTI DATI ESTERNE (SPOT, ORDERBOOK DEPTH, FUTURES FUNDING, FEAR & GREED)
# ==============================================================================

def fetch_ohlc(pair, interval=60):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    r = requests.get(url, timeout=18)
    r.raise_for_status()
    res = r.json()["result"]
    key = [k for k in res.keys() if k != "last"][0]
    return res[key]


def fetch_eur_usdt_rate():
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=EURUSD", timeout=10)
        res = r.json()["result"]
        key = list(res.keys())[0]
        return float(res[key]["c"][0])
    except Exception:
        return 1.08


def fetch_orderbook_imbalance(pair="SOLUSD", depth=50):
    try:
        url = f"https://api.kraken.com/0/public/Depth?pair={pair}&count={depth}"
        r = requests.get(url, timeout=12)
        res = r.json()["result"]
        key = list(res.keys())[0]
        bids = res[key]["bids"]
        asks = res[key]["asks"]
        if not bids or not asks:
            return {"imbalance": 0.0, "bid_ask_ratio": 1.0, "spread_bps": 2.0, "ok": False}

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid) * 10000.0

        w_bid_vol = 0.0
        for p_str, v_str, _ in bids:
            p, v = float(p_str), float(v_str)
            dist = max(abs(mid - p) / mid, 0.0005)
            w_bid_vol += v * math.exp(-dist * 150.0)

        w_ask_vol = 0.0
        for p_str, v_str, _ in asks:
            p, v = float(p_str), float(v_str)
            dist = max(abs(p - mid) / mid, 0.0005)
            w_ask_vol += v * math.exp(-dist * 150.0)

        tot = w_bid_vol + w_ask_vol
        imbalance = ((w_bid_vol - w_ask_vol) / tot) * 100.0 if tot > 0 else 0.0
        ratio = w_bid_vol / w_ask_vol if w_ask_vol > 0 else 1.0
        return {
            "imbalance": imbalance,
            "bid_ask_ratio": ratio,
            "spread_bps": spread_bps,
            "ok": True,
        }
    except Exception:
        return {"imbalance": 0.0, "bid_ask_ratio": 1.0, "spread_bps": 2.0, "ok": False}


def fetch_all_futures_metrics():
    """Scarica in una sola chiamata i Funding Rate di tutte le criptovalute da Kraken Futures."""
    out = {}
    try:
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        r = requests.get(url, timeout=12)
        tickers = r.json().get("tickers", [])
        for t in tickers:
            sym = t.get("symbol", "")
            if sym.startswith("PF_"):
                fr_raw = float(t.get("fundingRate", 0.0))
                mark_p = float(t.get("markPrice", 1.0))
                if abs(fr_raw) > 0.01 and mark_p > 1.0:
                    fr_pct_8h = (fr_raw / mark_p) * 100.0 * 8.0
                else:
                    fr_pct_8h = fr_raw * 100.0 * 8.0
                oi = float(t.get("openInterest", 0.0))
                out[sym] = {"funding_8h_pct": fr_pct_8h, "open_interest": oi, "ok": True}
    except Exception:
        pass
    return out


def fetch_fear_and_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json().get("data", [])
        if data:
            val = int(data[0].get("value", 50))
            label = data[0].get("value_classification", "Neutral")
            return {"value": val, "label": label, "ok": True}
    except Exception:
        pass
    return {"value": 50, "label": "Neutral", "ok": False}


# ==============================================================================
# MOTORE AI: RETE NEURALE (MLP) + CLASSIFICATORE LORENTZIAN DISTANCE k-NN
# ==============================================================================

def _sigmoid(x):
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def run_neural_and_lorentzian_ai(highs, lows, closes, vols):
    """
    Addestra in tempo reale sugli ultimi ~450 cicli orari della moneta:
    1) Classificatore k-NN nello spazio Lorentziano: d(x,y) = sum(ln(1 + |x_i - y_i|))
    2) Multi-Layer Perceptron (Rete Neurale 6 -> 5 -> 1 con tanh + backpropagation)
    """
    n_len = len(closes)
    if n_len < 120:
        return {"combined_prob": 0.55, "mlp_prob": 0.55, "knn_prob": 0.55, "samples": 0}

    rsi_s = rsi_series(closes, 14)
    ema20_s = ema_series(closes, 20)
    ema50_s = ema_series(closes, 50)
    macd_s = macd_hist_series(closes)

    def extract_features(idx):
        p = closes[idx]
        idx_rsi = min(idx, len(rsi_s) - 1)
        idx_e20 = min(idx, len(ema20_s) - 1)
        idx_e50 = min(idx, len(ema50_s) - 1)
        idx_macd = min(idx, len(macd_s) - 1)

        f1 = (rsi_s[idx_rsi] - 50.0) / 25.0
        f2 = ((p - ema20_s[idx_e20]) / max(ema20_s[idx_e20], 1e-8)) * 25.0
        f3 = ((ema20_s[idx_e20] - ema50_s[idx_e50]) / max(ema50_s[idx_e50], 1e-8)) * 25.0
        f4 = (macd_s[idx_macd] / max(p, 1e-8)) * 300.0
        ret_4h = ((p - closes[idx - 4]) / max(closes[idx - 4], 1e-8)) * 20.0
        v_window = vols[max(0, idx - 20):idx + 1]
        v_avg = (sum(v_window) / len(v_window)) if v_window else 1.0
        f6 = (vols[idx] / max(v_avg, 1e-8)) - 1.0
        return [
            max(-3.0, min(3.0, f1)),
            max(-3.0, min(3.0, f2)),
            max(-3.0, min(3.0, f3)),
            max(-3.0, min(3.0, f4)),
            max(-3.0, min(3.0, ret_4h)),
            max(-3.0, min(3.0, f6)),
        ]

    horizon = 6
    X_train, y_train = [], []
    start_idx = max(55, n_len - 450)
    end_idx = n_len - horizon - 1

    for i in range(start_idx, end_idx):
        feats = extract_features(i)
        p = closes[i]
        future_high = max(highs[i + 1: i + 1 + horizon])
        future_low = min(lows[i + 1: i + 1 + horizon])
        label = 1.0 if (future_high >= p * 1.0065 and future_low >= p * 0.982) else 0.0
        X_train.append(feats)
        y_train.append(label)

    x_curr = extract_features(n_len - 1)

    if len(X_train) < 40:
        return {"combined_prob": 0.55, "mlp_prob": 0.55, "knn_prob": 0.55, "samples": len(X_train)}

    # --- 1. LORENTZIAN DISTANCE k-NN ---
    distances = []
    for feats, label in zip(X_train, y_train):
        lor_dist = sum(math.log(1.0 + abs(a - b)) for a, b in zip(x_curr, feats))
        distances.append((lor_dist, label))
    distances.sort(key=lambda t: t[0])
    k = min(15, len(distances))
    top_k = distances[:k]
    w_sum, w_tot = 0.0, 0.0
    for d, lbl in top_k:
        w = 1.0 / (d + 0.05)
        w_sum += w * lbl
        w_tot += w
    knn_prob = w_sum / w_tot if w_tot > 0 else 0.50

    # --- 2. MULTI-LAYER PERCEPTRON (Rete Neurale 6 -> 5 -> 1) ---
    rng = random.Random(42)
    n_in, n_hid = 6, 5
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(n_in)] for _ in range(n_hid)]
    b1 = [0.0 for _ in range(n_hid)]
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(n_hid)]
    b2 = 0.0

    lr = 0.045
    epochs = 30
    for _ in range(epochs):
        for x_vec, target in zip(X_train, y_train):
            h_act = []
            for j in range(n_hid):
                z1 = sum(W1[j][i] * x_vec[i] for i in range(n_in)) + b1[j]
                h_act.append(math.tanh(max(-15.0, min(15.0, z1))))
            z2 = sum(W2[j] * h_act[j] for j in range(n_hid)) + b2
            pred = _sigmoid(z2)

            dz2 = pred - target
            for j in range(n_hid):
                grad_w2 = dz2 * h_act[j]
                dh = dz2 * W2[j] * (1.0 - h_act[j] ** 2)
                W2[j] -= lr * grad_w2
                b1[j] -= lr * dh
                for i in range(n_in):
                    W1[j][i] -= lr * (dh * x_vec[i])
            b2 -= lr * dz2

    h_curr = [
        math.tanh(max(-15.0, min(15.0, sum(W1[j][i] * x_curr[i] for i in range(n_in)) + b1[j])))
        for j in range(n_hid)
    ]
    mlp_prob = _sigmoid(sum(W2[j] * h_curr[j] for j in range(n_hid)) + b2)

    combined_prob = 0.60 * mlp_prob + 0.40 * knn_prob
    return {
        "combined_prob": combined_prob,
        "mlp_prob": mlp_prob,
        "knn_prob": knn_prob,
        "samples": len(X_train),
    }


# ==============================================================================
# ANALISI MULTI-COIN E SCORING SU 100 PUNTI PER OGNI MONETA
# ==============================================================================

def analyze_btc_context():
    try:
        c1h = fetch_ohlc(BTC_PAIR, 60)
        c5m = fetch_ohlc(BTC_PAIR, 5)
        highs = [float(c[2]) for c in c1h]
        lows = [float(c[3]) for c in c1h]
        closes = [float(c[4]) for c in c1h]

        price = closes[-1]
        chg4h = ((price - closes[-5]) / closes[-5]) * 100.0
        chg24h = ((price - closes[-25]) / closes[-25]) * 100.0
        ema50 = ema_series(closes, 50)[-1]
        ema200 = ema_series(closes, 200)[-1]
        adx, pdi, mdi = adx_di(highs, lows, closes, 14)

        dumping = (chg4h <= -2.8) or (chg24h <= -6.0)
        strong_bear = (price < ema200) and (mdi > pdi) and (adx > 28)

        return {
            "price": price,
            "chg4h": chg4h,
            "chg24h": chg24h,
            "above_ema50": price >= ema50,
            "dumping": dumping,
            "strong_bear": strong_bear,
            "news_reaction": check_market_news_reaction(c5m),
            "ok": True,
        }
    except Exception:
        return {
            "price": 0.0, "chg4h": 0.0, "chg24h": 0.0,
            "above_ema50": True, "dumping": False, "strong_bear": False,
            "news_reaction": {"calm": True, "range_15m_pct": 0.4, "move_15m_pct": 0.3, "vol_spike": False},
            "ok": False,
        }


def analyze_single_coin(coin_cfg, btc, eur_usdt, invest_usdt, all_futures, fng):
    pair = coin_cfg["kraken"]
    coin_sym = coin_cfg["coin"]
    c1h = fetch_ohlc(pair, 60)
    c4h = fetch_ohlc(pair, 240)
    c15m = fetch_ohlc(pair, 15)
    ob = fetch_orderbook_imbalance(pair, 50)
    futures = all_futures.get(coin_cfg["futures"], {"funding_8h_pct": 0.0, "open_interest": 0.0, "ok": False})

    opens_1h = [float(c[1]) for c in c1h]
    highs_1h = [float(c[2]) for c in c1h]
    lows_1h = [float(c[3]) for c in c1h]
    closes_1h = [float(c[4]) for c in c1h]
    vols_1h = [float(c[6]) for c in c1h]

    highs_4h = [float(c[2]) for c in c4h]
    lows_4h = [float(c[3]) for c in c4h]
    closes_4h = [float(c[4]) for c in c4h]

    opens_15m = [float(c[1]) for c in c15m]
    closes_15m = [float(c[4]) for c in c15m]

    price = closes_1h[-1]
    chg24 = ((price - closes_1h[-25]) / closes_1h[-25]) * 100.0
    chg4h = ((price - closes_1h[-5]) / closes_1h[-5]) * 100.0

    # --- Indicatori 1H & 4H ---
    ema50_1h = ema_series(closes_1h, 50)[-1]
    ema200_1h = ema_series(closes_1h, 200)[-1]
    ema50_4h = ema_series(closes_4h, 50)[-1]
    ema200_4h = ema_series(closes_4h, 200)[-1]

    rsi_1h_list = rsi_series(closes_1h, 14)
    rsi_1h = rsi_1h_list[-1]
    rsi_15m_list = rsi_series(closes_15m, 14)
    rsi_15m = rsi_15m_list[-1]
    rsi_15m_prev = rsi_15m_list[-2]
    srsi_15m = stoch_rsi(closes_15m, 14, 14)

    macd_h = macd_hist_series(closes_1h)
    macd_curr, macd_prev = macd_h[-1], macd_h[-2]
    macd_rising = macd_curr > macd_prev

    bb_lo, bb_mid, bb_up, bb_width, bb_pct_b = bollinger(closes_1h, 20, 2.0)

    atrs_1h = atr_series(highs_1h, lows_1h, closes_1h, 14)
    atr_1h = atrs_1h[-1]
    atr_pct = (atr_1h / price) * 100.0
    atr_mean_7d = sum(atrs_1h[-168:]) / min(len(atrs_1h), 168)
    atr_ratio = atr_1h / atr_mean_7d if atr_mean_7d else 1.0

    kc_width = ((atr_1h * 3.0) / bb_mid) * 100.0 if bb_mid else 0.0
    in_squeeze = (bb_width < kc_width) and (atr_ratio < 0.70)

    adx_1h, pdi_1h, mdi_1h = adx_di(highs_1h, lows_1h, closes_1h, 14)
    adx_4h, pdi_4h, mdi_4h = adx_di(highs_4h, lows_4h, closes_4h, 14)

    # --- Econometria & VWAP ---
    hurst = hurst_exponent(closes_1h, 24)
    vr_6 = variance_ratio_mr(closes_1h, 6)
    vwap_24h = vwap_rolling(highs_1h, lows_1h, closes_1h, vols_1h, 24)
    vwap_diff_pct = ((price - vwap_24h) / vwap_24h) * 100.0

    # --- AI Engine ---
    ai_res = run_neural_and_lorentzian_ai(highs_1h, lows_1h, closes_1h, vols_1h)
    ai_prob = ai_res["combined_prob"]

    # --- Volumi & Smart Money ---
    vol_sma20 = sma(vols_1h[:-1], 20)
    red_candle_vol_ratios = []
    for i in range(-3, 0):
        if closes_1h[i] < opens_1h[i]:
            red_candle_vol_ratios.append(vols_1h[i] / vol_sma20 if vol_sma20 else 1.0)
    max_red_vol_ratio = max(red_candle_vol_ratios) if red_candle_vol_ratios else 0.0
    panic_selling = (max_red_vol_ratio >= 2.3 and chg4h <= -2.0) or (chg4h <= -3.8)

    ema9_15m = ema_series(closes_15m, 9)[-1]
    candle_15m_green = closes_15m[-1] >= opens_15m[-1]
    bounce_15m = candle_15m_green or (closes_15m[-1] > ema9_15m) or (rsi_15m > rsi_15m_prev)

    # --- Calcolo Range Ottimale per 50€ ---
    max_grids = max(4, min(10, int(invest_usdt // MIN_ORDER_USDT)))
    swing_low_72h = min(lows_1h[-72:])
    swing_high_72h = max(highs_1h[-72:])

    ideal_span_pct = max(max_grids * TARGET_STEP_PCT, atr_pct * 4.2, 3.2)
    ideal_span_pct = min(ideal_span_pct, 7.2)
    span_usd = price * (ideal_span_pct / 100.0)

    if price <= bb_mid:
        raw_low = max(swing_low_72h * 0.995, price - span_usd * 0.42)
        raw_high = raw_low + span_usd
    else:
        raw_high = min(swing_high_72h * 1.005, price + span_usd * 0.48)
        raw_low = raw_high - span_usd

    lower = round_p(raw_low)
    upper = round_p(raw_high)
    if upper <= lower:
        upper = round_p(lower * 1.04)

    range_pct = ((upper - lower) / price) * 100.0

    grids = max_grids
    while grids > 4 and ((range_pct / grids) - PIONEX_ROUNDTRIP_FEE_PCT) < MIN_NET_STEP_PCT:
        grids -= 1

    gross_step_pct = range_pct / grids
    net_step_pct = gross_step_pct - PIONEX_ROUNDTRIP_FEE_PCT
    step_usd = (upper - lower) / grids
    usdt_per_grid = invest_usdt / grids

    pos_in_channel = ((price - lower) / (upper - lower)) * 100.0 if upper > lower else 50.0
    stop_loss = round_p(lower - max(atr_1h * 1.15, step_usd * 1.5))

    recent_24h_span = max(highs_1h[-24:]) - min(lows_1h[-24:])
    rotation_ratio = recent_24h_span / max(upper - lower, 1e-8)

    alpha_vs_btc = chg24 - btc["chg24h"] if btc["ok"] else 0.0

    # ==========================================================================
    # RUBRICA QUANTITATIVA A 9 PILASTRI (0 - 100 PUNTI -> VOTO 1 - 10)
    # ==========================================================================
    pts = 0.0
    breakdown = []

    # 1. RETE NEURALE (MLP) & LORENTZIAN k-NN (Max 15 pt)
    if ai_prob >= 0.64:
        p1 = 15.0
    elif ai_prob >= 0.57:
        p1 = 12.0
    elif ai_prob >= 0.51:
        p1 = 9.0
    elif ai_prob >= 0.45:
        p1 = 5.0
    else:
        p1 = 1.0
    pts += p1
    breakdown.append(
        f"🧠 <b>AI Neural MLP + Lorentzian k-NN ({p1:.0f}/15)</b>: Probabilità Long <b>{ai_prob*100:.1f}%</b> "
        f"(MLP {ai_res['mlp_prob']*100:.0f}% | k-NN {ai_res['knn_prob']*100:.0f}%)"
    )

    # 2. STRUTTURA MULTI-TIMEFRAME 4H & 1H (Max 15 pt)
    p2 = 0.0
    if price >= ema50_4h:
        p2 += 6.0
    elif price >= ema200_4h:
        p2 += 3.5
    if price >= ema200_1h:
        p2 += 4.5
    elif price >= ema50_1h:
        p2 += 2.5
    if pdi_1h >= mdi_1h:
        p2 += 4.5
    elif adx_1h < 22:
        p2 += 3.0
    pts += p2
    trend_lbl = "Rialzista/Neutro" if p2 >= 10 else ("Misto" if p2 >= 6 else "Debole/Ribassista")
    breakdown.append(
        f"📐 <b>Struttura Multi-TF 4H/1H ({p2:.0f}/15)</b>: {trend_lbl} (ADX 1H {adx_1h:.1f}, 4H EMA50 ${fmt_p(ema50_4h)})"
    )

    # 3. REGIME ECONOMETRICO: HURST, VARIANCE RATIO & VOLATILITÀ (Max 12 pt)
    p3 = 0.0
    if hurst <= 0.47:
        p3 += 5.0
    elif hurst <= 0.53:
        p3 += 3.5
    else:
        p3 += 1.5
    if vr_6 <= 0.95:
        p3 += 3.0
    elif vr_6 <= 1.10:
        p3 += 2.0
    if not in_squeeze and 0.72 <= atr_ratio <= 1.45:
        p3 += 4.0
    elif not in_squeeze:
        p3 += 2.0
    pts += p3
    regime_str = "Mean-Reverting (Ideale)" if hurst < 0.49 else ("Equilibrato" if hurst <= 0.53 else "Trending")
    breakdown.append(
        f"🔬 <b>Econometria & Volatilità ({p3:.0f}/12)</b>: Hurst <b>{hurst:.2f}</b> ({regime_str}), "
        f"VR {vr_6:.2f}, ATR Ratio {atr_ratio:.2f}x"
    )

    # 4. VALORE: POSIZIONE NEL CANALE, VWAP & BOLLINGER %B (Max 13 pt)
    p4 = 0.0
    if 15 <= pos_in_channel <= 58:
        p4 += 7.0
    elif 58 < pos_in_channel <= 68:
        p4 += 4.5
    elif 8 <= pos_in_channel < 15:
        p4 += 4.0
    else:
        p4 += 1.0
    if -2.2 <= vwap_diff_pct <= 0.8:
        p4 += 3.5
    elif vwap_diff_pct <= 1.8:
        p4 += 2.0
    if 18 <= bb_pct_b <= 68:
        p4 += 2.5
    pts += p4
    breakdown.append(
        f"🎯 <b>Valore & Posizione Prezzo ({p4:.0f}/13)</b>: Nel canale al <b>{pos_in_channel:.0f}%</b>, "
        f"vs VWAP {vwap_diff_pct:+.2f}%, BB %B {bb_pct_b:.0f}%"
    )

    # 5. MOMENTUM & MICRO-TIMING 15m (Max 12 pt)
    p5 = 0.0
    if macd_rising:
        p5 += 4.5
    if 36 <= rsi_1h <= 63:
        p5 += 3.5
    elif 30 <= rsi_1h <= 68:
        p5 += 2.0
    if bounce_15m and srsi_15m < 85:
        p5 += 4.0
    elif srsi_15m < 30:
        p5 += 2.5
    pts += p5
    breakdown.append(
        f"⚡ <b>Momentum & Timing 15m ({p5:.0f}/12)</b>: RSI 1H {rsi_1h:.1f} | RSI 15m {rsi_15m:.1f} | "
        f"MACD {'in salita ↗️' if macd_rising else 'in calo ↘️'}"
    )

    # 6. MICROSTRUTTURA ORDERBOOK DEPTH & SMART MONEY VOLUME (Max 10 pt)
    p6 = 0.0
    if ob["imbalance"] >= 8.0:
        p6 += 5.5
    elif ob["imbalance"] >= -5.0:
        p6 += 4.0
    elif ob["imbalance"] >= -15.0:
        p6 += 2.0
    if not panic_selling and max_red_vol_ratio < 1.6:
        p6 += 4.5
    elif not panic_selling:
        p6 += 2.5
    pts += p6
    breakdown.append(
        f"🏛️ <b>Orderbook L2 & Flussi ({p6:.0f}/10)</b>: Sbilanciamento Bid/Ask <b>{ob['imbalance']:+.1f}%</b> "
        f"(Ratio {ob['bid_ask_ratio']:.2f}x)"
    )

    # 7. FORZA RELATIVA VS BITCOIN - ALPHA (Max 8 pt)
    p7 = 0.0
    if not btc["dumping"] and not btc["strong_bear"]:
        p7 += 4.5
        if alpha_vs_btc >= 0.0:
            p7 += 3.5
        elif alpha_vs_btc >= -1.5:
            p7 += 2.0
    elif not btc["dumping"]:
        p7 += 2.5
    pts += p7
    breakdown.append(
        f"₿ <b>Correlazione & Alpha BTC ({p7:.0f}/8)</b>: BTC 24h {btc['chg24h']:+.1f}% | "
        f"Alpha {coin_sym} {alpha_vs_btc:+.1f}%"
    )

    # 8. DERIVATI PERPETUAL (FUNDING RATE) & SENTIMENT (Max 7 pt)
    p8 = 0.0
    fr = futures["funding_8h_pct"]
    if -0.025 <= fr <= 0.022:
        p8 += 4.0
    elif fr < -0.025:
        p8 += 3.5
    else:
        p8 += 1.0
    fg_val = fng["value"]
    if 22 <= fg_val <= 74:
        p8 += 3.0
    else:
        p8 += 1.5
    pts += p8
    breakdown.append(
        f"🔮 <b>Futures & Sentiment ({p8:.0f}/7)</b>: Funding 8h <b>{fr:+.4f}%</b> | "
        f"Fear&Greed <b>{fg_val}</b> ({fng['label']})"
    )

    # 9. EFFICIENZA ECONOMICA GRIGLIA 50€ (Max 8 pt)
    p9 = 0.0
    if net_step_pct >= 0.20:
        p9 += 4.5
    elif net_step_pct >= MIN_NET_STEP_PCT:
        p9 += 3.0
    if 0.50 <= rotation_ratio <= 1.35:
        p9 += 3.5
    elif rotation_ratio >= 0.35:
        p9 += 2.0
    pts += p9
    breakdown.append(
        f"💶 <b>Efficienza Griglia 50€ ({p9:.0f}/8)</b>: Netto/griglia <b>+{net_step_pct:.2f}%</b> | "
        f"Rotazione 24h {rotation_ratio:.2f}x"
    )

    raw_score_10 = round(pts / 10.0)
    score = max(1, min(10, raw_score_10))

    # --- REGOLE DI VETO E PRUDENZA ---
    veto_reasons = []
    if btc["dumping"]:
        score = min(score, 2)
        veto_reasons.append(f"⛔ <b>VETO BTC</b>: Bitcoin in forte calo ({btc['chg4h']:+.1f}% in 4h)")
    elif btc["strong_bear"]:
        score = min(score, 3)
        veto_reasons.append("⛔ <b>VETO BTC</b>: Bitcoin in deciso trend ribassista")

    if panic_selling:
        score = min(score, 2)
        veto_reasons.append(f"⛔ <b>VETO VOLUMI</b>: Pressione di vendita anomala su {coin_sym} ({chg4h:+.1f}% in 4h)")

    if price < ema50_4h and price < ema200_1h and mdi_1h > pdi_1h and (adx_1h > 25 or chg24 < -5.0):
        score = min(score, 3)
        veto_reasons.append(f"⛔ <b>VETO TREND</b>: Trend ribassista allineato su 4H e 1H per {coin_sym}")

    if in_squeeze:
        score = min(score, 4)
        veto_reasons.append("⚠️ <b>COMPRESSIONE</b>: Volatilità in Keltner Squeeze (attesa esplosione)")

    if pos_in_channel > 74:
        score = min(score, 5)
        veto_reasons.append(f"⚠️ <b>TETTO RANGE</b>: Prezzo troppo alto nel canale ({pos_in_channel:.0f}%)")

    if ai_prob < 0.38:
        score = min(score, 4)
        veto_reasons.append(f"⚠️ <b>VETO AI</b>: Probabilità neurale sfavorevole ({ai_prob*100:.0f}%)")

    return {
        "coin": coin_sym,
        "name": coin_cfg["name"],
        "pionex_symbol": coin_cfg["pionex"],
        "price": price,
        "chg24": chg24,
        "chg4h": chg4h,
        "vwap_24h": vwap_24h,
        "rsi_1h": rsi_1h,
        "rsi_15m": rsi_15m,
        "srsi_15m": srsi_15m,
        "adx_1h": adx_1h,
        "adx_4h": adx_4h,
        "hurst": hurst,
        "vr_6": vr_6,
        "atr_pct": atr_pct,
        "atr_ratio": atr_ratio,
        "ai_prob": ai_prob,
        "ai_res": ai_res,
        "ob": ob,
        "futures": futures,
        "fng": fng,
        "btc": btc,
        "alpha_vs_btc": alpha_vs_btc,
        "eur_usdt": eur_usdt,
        "invest_usdt": invest_usdt,
        "lower": lower,
        "upper": upper,
        "grids": grids,
        "range_pct": range_pct,
        "gross_step_pct": gross_step_pct,
        "net_step_pct": net_step_pct,
        "step_usd": step_usd,
        "usdt_per_grid": usdt_per_grid,
        "stop_loss": stop_loss,
        "pos_in_channel": pos_in_channel,
        "pts_100": pts,
        "score": score,
        "breakdown": breakdown,
        "veto_reasons": veto_reasons,
        "news_reaction": btc["news_reaction"],
    }


def apply_news_cap(raw_score, news, nr):
    final_score = raw_score
    if news["status"] == "IMMINENT":
        has_high = any(e["impact"] == "High" for e in news["imminent"])
        final_score = min(final_score, 2 if has_high else 4)
    elif news["status"] == "COOLDOWN":
        if news["surprise_detected"] and not nr["calm"]:
            final_score = min(final_score, 2)
        elif not nr["calm"]:
            final_score = min(final_score, 3)
        elif news["surprise_detected"] and nr["calm"]:
            final_score = min(final_score, 5)
    if news["us_open_warning"] and not nr["calm"]:
        final_score = min(final_score, 4)
    return final_score


def scan_all_coins(news):
    """Scansiona in parallelo le 10 Criptovalute e restituisce la classifica ordinata dall'AI."""
    btc = analyze_btc_context()
    eur_usdt = fetch_eur_usdt_rate()
    invest_usdt = round(INVEST_EUR * eur_usdt, 2)
    all_futures = fetch_all_futures_metrics()
    fng = fetch_fear_and_greed()

    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(analyze_single_coin, cfg, btc, eur_usdt, invest_usdt, all_futures, fng): cfg
            for cfg in COIN_UNIVERSE
        }
        for fut in as_completed(future_map):
            cfg = future_map[fut]
            try:
                res = fut.result()
                res["final_score"] = apply_news_cap(res["score"], news, res["news_reaction"])
                results.append(res)
            except Exception as e:
                print(f"Errore scansione {cfg['coin']}: {e}")

    if not results:
        raise RuntimeError("Nessuna criptovaluta scansionata correttamente dalle API.")

    # Ordina per: 1) Voto Finale (1-10), 2) Punteggio Quantitativo (0-100), 3) Probabilità Rete Neurale
    results.sort(key=lambda x: (x["final_score"], x["pts_100"], x["ai_prob"]), reverse=True)

    best = results[0]
    best["all_coins"] = results
    return best


# ==============================================================================
# MOTORE DI GESTIONE AUTONOMA MULTI-COIN (AUTO-OPEN SULLA #1, TRAILING, AUTO-CLOSE)
# ==============================================================================

def execute_autonomous_cycle(best_a, news):
    state = load_bot_state()
    all_coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in all_coins}
    now_str = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")

    auto_log = []

    if state["active"]:
        active_coin = state.get("coin", "SOL")
        active_sym = state.get("pionex_symbol", f"{active_coin}_USDT")
        cur_a = coin_map.get(active_coin, best_a)

        price = cur_a["price"]
        final_score = cur_a["final_score"]
        low_b = state["lower"]
        up_b = state["upper"]
        stop_b = state["stop"]
        entry_p = state["entry_price"]
        high_p = max(state.get("highest_price", entry_p), price)
        state["highest_price"] = high_p
        pnl_pct = ((price - entry_p) / entry_p) * 100.0 if entry_p > 0 else 0.0
        pnl_usdt = cur_a["invest_usdt"] * (pnl_pct / 100.0)

        # 1. Protezione Dinamica del Profitto (Break-Even & Trailing Stop)
        if pnl_pct >= 2.5 and stop_b < entry_p:
            new_stop = round_p(entry_p * 1.003)
            state["stop"] = new_stop
            state["trailing_locked"] = True
            stop_b = new_stop
            auto_log.append(
                f"🛡️ <b>AUTO BREAK-EVEN ({active_coin})</b>: Profitto a +{pnl_pct:.1f}%, "
                f"Stop alzato a pareggio (${fmt_p(new_stop)})!"
            )
        elif pnl_pct >= 4.5 and stop_b < round_p(high_p * 0.975):
            new_stop = round_p(high_p * 0.975)
            state["stop"] = new_stop
            stop_b = new_stop
            auto_log.append(
                f"📈 <b>AUTO TRAILING STOP ({active_coin})</b>: Nuovo massimo ${fmt_p(high_p)}, "
                f"Stop alzato a ${fmt_p(new_stop)}!"
            )

        # 2. Controllo Condizioni di Chiusura Automatica sulla moneta attiva
        close_reason = None
        if price <= stop_b:
            close_reason = f"🛑 STOP LOSS / TRAILING COLPITO (${fmt_p(price)} <= ${fmt_p(stop_b)})"
        elif price >= up_b * 1.008:
            close_reason = f"🎯 TAKE PROFIT SUPERIORE RAGGIUNTO (${fmt_p(price)} > ${fmt_p(up_b)})"
        elif final_score <= 2 and cur_a["ai_prob"] < 0.44:
            close_reason = f"🚨 USCITA D'EMERGENZA AI/MACRO (Voto {active_coin} sceso a {final_score}/10)"

        if close_reason:
            qty_c = state.get("qty_coin", 0.0)
            ok_w, msg_w = trigger_pionex_webhook("sell", active_sym, price, qty_c)
            ok_a, msg_a = pionex_spot_market_order("SELL", active_sym, size_coin=qty_c)
            exec_note = msg_w if PIONEX_WEBHOOK_URL else msg_a

            closed_trade = {
                "coin": active_coin,
                "opened_at": state.get("opened_at", "-"),
                "closed_at": now_str,
                "entry_price": entry_p,
                "exit_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usdt": round(pnl_usdt, 2),
                "reason": close_reason,
            }
            history = state.get("history", [])
            history.insert(0, closed_trade)
            state["history"] = history[:30]

            state["active"] = False
            state["last_action"] = f"[{now_str}] Chiuso {active_coin} a ${fmt_p(price)} ({pnl_pct:+.2f}%) - {close_reason}"
            auto_log.append(
                f"🔔 <b>CHIUSURA AUTOMATICA ESEGUITA SU {active_coin}/USDT</b>\n"
                f"   • Motivo: {close_reason}\n"
                f"   • Prezzo Entrata: ${fmt_p(entry_p)} ➡️ Uscita: <b>${fmt_p(price)}</b>\n"
                f"   • Risultato: <b>{pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT)</b>\n"
                f"   • Esecuzione: {exec_note}"
            )
        else:
            auto_log.append(
                f"🤖 <b>POSIZIONE ATTIVA SU {active_coin}/USDT</b> (Aperta il {state['opened_at']})\n"
                f"   • Entrata: <b>${fmt_p(entry_p)}</b> | Attuale: <b>${fmt_p(price)}</b> (<b>{pnl_pct:+.2f}%</b> / {pnl_usdt:+.2f}$)\n"
                f"   • Range Attivo: <b>${fmt_p(low_b)} – ${fmt_p(up_b)}</b> ({state['grids']} griglie)\n"
                f"   • Stop Protettivo: <b>${fmt_p(stop_b)}</b> {'(Break-Even Attivo 🔒)' if state.get('trailing_locked') else ''}"
            )

    else:
        # Nessuna posizione attiva: valuta APERTURA AUTOMATICA sulla Moneta #1 scelta dall'AI
        win_coin = best_a["coin"]
        win_sym = best_a["pionex_symbol"]
        win_price = best_a["price"]
        win_score = best_a["final_score"]

        if win_score >= 7 and best_a["ai_prob"] >= 0.52:
            qty_coin = round(best_a["invest_usdt"] / win_price, 4)
            ok_w, msg_w = trigger_pionex_webhook("buy", win_sym, win_price, qty_coin)
            ok_a, msg_a = pionex_spot_market_order("BUY", win_sym, amount_usdt=best_a["invest_usdt"])
            exec_note = msg_w if PIONEX_WEBHOOK_URL else msg_a

            state.update({
                "active": True,
                "coin": win_coin,
                "pionex_symbol": win_sym,
                "entry_price": win_price,
                "highest_price": win_price,
                "lower": best_a["lower"],
                "upper": best_a["upper"],
                "stop": best_a["stop_loss"],
                "grids": best_a["grids"],
                "qty_coin": qty_coin,
                "trailing_locked": False,
                "opened_at": now_str,
                "last_action": f"[{now_str}] Aperto {win_coin}/USDT a ${fmt_p(win_price)} (Voto {win_score}/10)",
            })
            auto_log.append(
                f"🚀 <b>APERTURA AUTOMATICA AI SU {win_coin}/USDT!</b> (Voto {win_score}/10 | AI {best_a['ai_prob']*100:.1f}%)\n"
                f"   • Prezzo Ingresso: <b>${fmt_p(win_price)}</b> (Qty: {qty_coin} {win_coin} / ~{best_a['invest_usdt']:.1f} USDT)\n"
                f"   • Range Impostato: <b>${fmt_p(best_a['lower'])} – ${fmt_p(best_a['upper'])}</b> ({best_a['grids']} griglie)\n"
                f"   • Stop Loss Iniziale: <b>${fmt_p(best_a['stop_loss'])}</b>\n"
                f"   • Esecuzione: {exec_note}"
            )
        else:
            auto_log.append(
                f"⏳ <b>BOT IN ATTESA DI SEGNALE</b> (Scansione 10 Cripto completata)\n"
                f"   • Migliore adesso: <b>{win_coin}/USDT</b> con Voto <b>{win_score}/10</b> ({best_a['pts_100']:.0f}/100 pt | AI {best_a['ai_prob']*100:.0f}%)\n"
                f"   • Condizione di avvio automatico: <b>Voto >= 7/10 e AI >= 52%</b>\n"
                f"   • Ultima operazione: <i>{state.get('last_action', 'Nessuna')}</i>"
            )

    save_bot_state(state)
    best_a["auto_state"] = state
    best_a["auto_log"] = auto_log
    return best_a["final_score"]


# ==============================================================================
# GENERATORE WEBAPP DASHBOARD MULTI-COIN (index.html inviato su Telegram)
# ==============================================================================

def generate_webapp_dashboard(best_a, news, final_score):
    state = best_a.get("auto_state", load_bot_state())
    all_coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in all_coins}
    now_str = news["now_rome"].strftime("%d/%m/%Y %H:%M:%S")

    score_color = "emerald" if final_score >= 7 else ("amber" if final_score >= 5 else "rose")
    mode_badge = "LIVE TRADING PIONEX" if AUTO_TRADE_ENABLED else "SIMULAZIONE PAPER TRADING"
    mode_color = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if AUTO_TRADE_ENABLED else "bg-sky-500/20 text-sky-300 border-sky-500/40"

    if state.get("active"):
        act_coin = state.get("coin", "SOL")
        act_a = coin_map.get(act_coin, best_a)
        cur_p = act_a["price"]
        entry_p = state["entry_price"]
        low_b = state["lower"]
        up_b = state["upper"]
        stop_b = state["stop"]
        pnl_pct = ((cur_p - entry_p) / entry_p) * 100.0 if entry_p > 0 else 0.0
        pnl_usdt = best_a["invest_usdt"] * (pnl_pct / 100.0)
        pnl_col = "text-emerald-400" if pnl_pct >= 0 else "text-rose-400"
        pos_pct = max(0.0, min(100.0, ((cur_p - low_b) / max(up_b - low_b, 1e-8)) * 100.0))

        pos_html = f"""
        <div class="bg-slate-800/90 border border-emerald-500/40 rounded-2xl p-6 shadow-xl">
            <div class="flex flex-wrap justify-between items-center gap-2 mb-4">
                <div class="flex items-center gap-3">
                    <span class="h-3.5 w-3.5 rounded-full bg-emerald-400 animate-ping"></span>
                    <h2 class="text-xl font-bold text-white">POSIZIONE ATTIVA SU {act_coin}/USDT</h2>
                </div>
                <span class="text-xs bg-slate-700 text-slate-300 px-3 py-1 rounded-full">Aperta il {state.get('opened_at')}</span>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
                <div class="bg-slate-900/70 p-4 rounded-xl">
                    <div class="text-xs text-slate-400">Entrata ({act_coin})</div>
                    <div class="text-xl font-bold text-white">${fmt_p(entry_p)}</div>
                </div>
                <div class="bg-slate-900/70 p-4 rounded-xl">
                    <div class="text-xs text-slate-400">Prezzo Attuale</div>
                    <div class="text-xl font-bold text-sky-400">${fmt_p(cur_p)}</div>
                </div>
                <div class="bg-slate-900/70 p-4 rounded-xl">
                    <div class="text-xs text-slate-400">PnL Stimato (50€)</div>
                    <div class="text-xl font-bold {pnl_col}">{pnl_pct:+.2f}% ({pnl_usdt:+.2f}$)</div>
                </div>
                <div class="bg-slate-900/70 p-4 rounded-xl">
                    <div class="text-xs text-slate-400">Stop Loss Dinamico</div>
                    <div class="text-xl font-bold text-amber-400">${fmt_p(stop_b)} {'🔒' if state.get('trailing_locked') else ''}</div>
                </div>
            </div>
            <div class="mb-2 flex justify-between text-xs text-slate-300">
                <span>Lower: <b>${fmt_p(low_b)}</b></span>
                <span>Posizione nel Range: <b>{pos_pct:.0f}%</b> ({state.get('grids')} Griglie)</span>
                <span>Upper: <b>${fmt_p(up_b)}</b></span>
            </div>
            <div class="w-full bg-slate-900 rounded-full h-4 p-0.5 border border-slate-700">
                <div class="bg-gradient-to-r from-sky-500 to-emerald-400 h-2.5 rounded-full" style="width: {pos_pct:.1f}%"></div>
            </div>
        </div>
        """
    else:
        pos_html = f"""
        <div class="bg-slate-800/80 border border-slate-700 rounded-2xl p-6 shadow-xl">
            <div class="flex items-center gap-3 mb-2">
                <span class="h-3 w-3 rounded-full bg-amber-400"></span>
                <h2 class="text-lg font-bold text-slate-200">NESSUN TRADE APERTO — IN ATTESA DI VOTO &ge; 7/10</h2>
            </div>
            <p class="text-sm text-slate-400 mb-3">L'AI scansiona 10 criptovalute ogni 30 minuti e sceglie automaticamente la migliore opportunità. Adesso in testa c'è <b>{best_a['coin']}/USDT</b> con voto <b>{final_score}/10</b>.</p>
            <div class="text-xs text-slate-400 bg-slate-900/70 p-3 rounded-xl border border-slate-700/60">
                <b>Range Consigliato per {best_a['coin']}/USDT:</b> ${fmt_p(best_a['lower'])} &ndash; ${fmt_p(best_a['upper'])} ({best_a['grids']} griglie, netto +{best_a['net_step_pct']:.2f}%/griglia) | <b>Stop:</b> ${fmt_p(best_a['stop_loss'])}<br>
                <b>Ultima Azione:</b> {html.escape(state.get('last_action', 'Nessuna'))}
            </div>
        </div>
        """

    # Tabella Classifica 10 Criptovalute
    ranking_rows = ""
    for idx, c in enumerate(all_coins, 1):
        sc = c["final_score"]
        badge_c = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if sc >= 7 else (
            "bg-amber-500/20 text-amber-300 border-amber-500/40" if sc >= 5 else "bg-rose-500/20 text-rose-300 border-rose-500/40"
        )
        chg_c = "text-emerald-400" if c["chg24"] >= 0 else "text-rose-400"
        crown = "👑 " if idx == 1 else f"#{idx} "
        ranking_rows += f"""
        <tr class="border-b border-slate-800/80 text-xs hover:bg-slate-800/40">
            <td class="py-3 px-3 font-bold text-white">{crown}{c['coin']}/USDT <span class="text-slate-500 font-normal">({c['name']})</span></td>
            <td class="py-3 px-3 font-semibold text-slate-200">${fmt_p(c['price'])}</td>
            <td class="py-3 px-3 font-semibold {chg_c}">{c['chg24']:+.2f}%</td>
            <td class="py-3 px-3 text-sky-400 font-bold">{c['ai_prob']*100:.1f}%</td>
            <td class="py-3 px-3 text-purple-300">{c['hurst']:.2f}</td>
            <td class="py-3 px-3 text-slate-300">${fmt_p(c['lower'])} &ndash; ${fmt_p(c['upper'])}</td>
            <td class="py-3 px-3"><span class="px-2.5 py-1 rounded-full border font-bold {badge_c}">{sc}/10 ({c['pts_100']:.0f}pt)</span></td>
        </tr>
        """

    pillars_html = "".join(
        f'<li class="bg-slate-900/60 border border-slate-700/60 rounded-xl p-3 text-sm text-slate-200">{b}</li>'
        for b in best_a["breakdown"]
    )

    history_rows = ""
    for h in state.get("history", []):
        c = "text-emerald-400" if h.get("pnl_pct", 0) >= 0 else "text-rose-400"
        h_coin = h.get("coin", "SOL")
        history_rows += f"""
        <tr class="border-b border-slate-800 text-xs">
            <td class="py-2.5 px-3 font-bold text-white">{h_coin}/USDT</td>
            <td class="py-2.5 px-3">{h.get('opened_at')} &rarr; {h.get('closed_at')}</td>
            <td class="py-2.5 px-3">${fmt_p(h.get('entry_price', 0))} &rarr; ${fmt_p(h.get('exit_price', 0))}</td>
            <td class="py-2.5 px-3 font-bold {c}">{h.get('pnl_pct', 0):+.2f}% ({h.get('pnl_usdt', 0):+.2f}$)</td>
            <td class="py-2.5 px-3 text-slate-400">{html.escape(h.get('reason', ''))}</td>
        </tr>
        """
    if not history_rows:
        history_rows = '<tr><td colspan="5" class="py-4 text-center text-slate-500 text-sm">Nessun trade chiuso nello storico recente.</td></tr>'

    html_doc = f"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Coin Neural Quant AI Bot</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans p-4 md:p-8">
    <div class="max-w-6xl mx-auto space-y-6">
        <div class="flex flex-wrap justify-between items-center gap-4 bg-slate-900/90 border border-slate-800 p-5 rounded-2xl shadow-lg">
            <div>
                <div class="flex items-center gap-2.5">
                    <span class="text-2xl">🤖</span>
                    <h1 class="text-xl md:text-2xl font-extrabold tracking-tight text-white">Multi-Coin Neural Quant Grid Bot</h1>
                </div>
                <p class="text-xs text-slate-400 mt-1">Ultimo aggiornamento: <b>{now_str}</b> (10 Cripto Scansionate dall'AI)</p>
            </div>
            <span class="text-xs font-bold px-3.5 py-1.5 rounded-full border {mode_color}">{mode_badge}</span>
        </div>

        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">👑 Vincitrice AI Attuale</div>
                <div class="text-2xl font-black text-white mt-1">{best_a['coin']}/USDT</div>
                <div class="text-xs text-slate-400 mt-1">Prezzo: ${fmt_p(best_a['price'])} ({best_a['chg24']:+.2f}%)</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Voto Migliore (#1)</div>
                <div class="text-2xl font-black text-{score_color}-400 mt-1">{final_score}/10 <span class="text-sm font-normal text-slate-400">({best_a['pts_100']:.0f}/100)</span></div>
                <div class="text-xs text-slate-400 mt-1">Soglia apertura: &ge; 7/10</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Probabilità Neurale ({best_a['coin']})</div>
                <div class="text-2xl font-black text-sky-400 mt-1">{best_a['ai_prob']*100:.1f}%</div>
                <div class="text-xs text-slate-400 mt-1">MLP: {best_a['ai_res']['mlp_prob']*100:.0f}% | k-NN: {best_a['ai_res']['knn_prob']*100:.0f}%</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">Esponente Hurst ({best_a['coin']})</div>
                <div class="text-2xl font-black text-purple-400 mt-1">{best_a['hurst']:.2f}</div>
                <div class="text-xs text-slate-400 mt-1">{'Mean-Reverting (Ottimo)' if best_a['hurst'] < 0.50 else 'Trending / Neutro'}</div>
            </div>
        </div>

        {pos_html}

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">🏆 Classifica Live Multi-Coin AI Scanner (10 Criptovalute)</h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-slate-700 text-xs text-slate-400">
                            <th class="py-2 px-3">Moneta</th>
                            <th class="py-2 px-3">Prezzo</th>
                            <th class="py-2 px-3">24h %</th>
                            <th class="py-2 px-3">AI Prob</th>
                            <th class="py-2 px-3">Hurst</th>
                            <th class="py-2 px-3">Range Consigliato</th>
                            <th class="py-2 px-3">Voto Quant</th>
                        </tr>
                    </thead>
                    <tbody>
                        {ranking_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">📊 Pagella Dettagliata 9 Pilastri per 👑 {best_a['coin']}/USDT ({best_a['pts_100']:.0f}/100 pt)</h3>
            <ul class="grid grid-cols-1 md:grid-cols-2 gap-3">
                {pillars_html}
            </ul>
        </div>

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">📜 Storico Operazioni Chiuse (Simulazione / Live)</h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-slate-700 text-xs text-slate-400">
                            <th class="py-2 px-3">Moneta</th>
                            <th class="py-2 px-3">Periodo</th>
                            <th class="py-2 px-3">Prezzo In/Out</th>
                            <th class="py-2 px-3">Risultato (PnL)</th>
                            <th class="py-2 px-3">Motivo Chiusura</th>
                        </tr>
                    </thead>
                    <tbody>
                        {history_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>"""

    try:
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(html_doc)
    except Exception as e:
        print("Errore creazione index.html:", e)


# ==============================================================================
# FORMATTAZIONE REPORT TELEGRAM E INVIO FILE WEBAPP
# ==============================================================================

def score_bar(score):
    filled = max(1, min(10, score))
    color = "🟢" if filled >= 7 else ("🟡" if filled >= 5 else "🔴")
    return color * filled + "⚪" * (10 - filled)


def format_news_section(news, news_reaction):
    lines = ["📰 <b>CALENDARIO MACRO USA &amp; REAZIONE MERCATO</b>"]

    if news["status"] == "NO_DATA":
        lines.append("⚠️ Calendario macro momentaneamente non raggiungibile.")
        return "\n".join(lines)

    if news["us_open_warning"]:
        lines.append("🔔 <b>Wall Street Open (09:30 NY)</b>: finestra di alta volatilità.")

    if news["imminent"]:
        for ev in news["imminent"]:
            mins = max(1, int(round(ev["diff_min"])))
            icon = "🔴" if ev["impact"] == "High" else "🟠"
            lines.append(
                f"🚨 <b>TRA {mins} MIN ({ev['time_str']})</b>: {icon} <b>{html.escape(ev['title'])}</b>"
            )
    elif news["recent"]:
        for ev in news["recent"]:
            mins_ago = max(1, int(round(abs(ev["diff_min"]))))
            act_txt = f" | Dati: <b>{html.escape(ev['actual'])}</b> (Prev: {html.escape(ev['forecast'] or '-')})" if ev["actual"] else ""
            lines.append(
                f"⏳ <b>USCITA {mins_ago} MIN FA ({ev['time_str']})</b>: <b>{html.escape(ev['title'])}</b>{act_txt}"
            )
        if news_reaction["calm"]:
            lines.append(
                f"🟢 <b>Reazione Mercato post-news</b>: CALMO (escursione 15m: {news_reaction['range_15m_pct']:.2f}%)."
            )
        else:
            lines.append(
                f"🔴 <b>Reazione Mercato post-news</b>: AGITATO (escursione 15m: {news_reaction['range_15m_pct']:.2f}%)."
            )
    else:
        lines.append("✅ <b>Nessuna notizia macro bloccante nell'immediato.</b>")

    if news["next_event"] and not news["imminent"]:
        ne = news["next_event"]
        ore = int(ne["diff_min"] // 60)
        minuti = int(ne["diff_min"] % 60)
        icon = "🔴" if ne["impact"] == "High" else "🟠"
        tempo_str = f"tra {ore}h {minuti}m" if ore > 0 else f"tra {minuti} min"
        lines.append(
            f"🗓️ Prossimo evento: {icon} <b>{html.escape(ne['title'])}</b> ({ne['date_str']} ore {ne['time_str']}, {tempo_str})"
        )

    return "\n".join(lines)


def format_message(best_a, news):
    now_rome = news["now_rome"].strftime("%d/%m/%Y %H:%M")
    nr = best_a["news_reaction"]
    final_score = best_a["final_score"]
    all_coins = best_a.get("all_coins", [best_a])

    news_override_reason = None
    if news["status"] == "IMMINENT":
        has_high = any(e["impact"] == "High" for e in news["imminent"])
        news_override_reason = (
            "⛔ <b>BLOCCO NEWS</b>: Notizia USA ad ALTO impatto imminente!"
            if has_high else "⚠️ <b>PRUDENZA NEWS</b>: Notizia USA a medio impatto imminente."
        )
    elif news["status"] == "COOLDOWN" and not nr["calm"]:
        news_override_reason = "⏳ <b>COOLDOWN NEWS</b>: Notizia appena uscita e mercato instabile."
    elif news["us_open_warning"] and not nr["calm"]:
        news_override_reason = "⚠️ <b>APERTURA WALL STREET</b>: Volatilità elevata sull'apertura USA."

    if final_score >= 8:
        verdict = f"🟢 <b>SEGNALE FORTE SU {best_a['coin']}/USDT: OTTIMO MOMENTO PER APRIRE</b>"
    elif final_score == 7:
        verdict = f"🟢 <b>SEGNALE POSITIVO SU {best_a['coin']}/USDT: AVVIO BOT CONSIGLIATO</b>"
    elif final_score == 6:
        verdict = f"🟡 <b>QUASI PRONTO ({best_a['coin']} 6/10): SETUP DISCRETO MA ATTESA CONSIGLIATA</b>"
    elif final_score == 5:
        verdict = "🟡 <b>NEUTRALE (5/10): MERCATO INCERTO, MEGLIO ATTENDERE</b>"
    elif final_score >= 3:
        verdict = "🟠 <b>SCONSIGLIATO: CONDIZIONI TECNICO/MACRO SFAVOREVOLI</b>"
    else:
        verdict = "🔴 <b>PERICOLO / BLOCCO: NON APRIRE IL BOT ADESSO</b>"

    # Classifica compatta delle 10 Cripto
    rank_lines = ["🏆 <b>CLASSIFICA AI MULTI-COIN (10 Cripto Scansionate)</b>"]
    for idx, c in enumerate(all_coins, 1):
        sc = c["final_score"]
        ic = "🟢" if sc >= 7 else ("🟡" if sc >= 5 else "🔴")
        crown = "👑" if idx == 1 else f"{idx}."
        rank_lines.append(
            f"{crown} {ic} <b>{c['coin']}</b>: <b>{sc}/10</b> ({c['pts_100']:.0f}pt) | "
            f"AI <b>{c['ai_prob']*100:.0f}%</b> | ${fmt_p(c['price'])} ({c['chg24']:+.1f}%)"
        )
    rank_section = "\n".join(rank_lines)

    breakdown_txt = "\n".join(f"• {item}" for item in best_a["breakdown"])
    veto_list = list(best_a["veto_reasons"])
    if news_override_reason:
        veto_list.append(news_override_reason)
    veto_txt = ("\n\n🚨 <b>FILTRI DI SICUREZZA ATTIVI</b>\n" + "\n".join(veto_list)) if veto_list else ""

    news_section = format_news_section(news, nr)
    auto_section = "\n".join(best_a.get("auto_log", []))
    mode_str = "🟢 LIVE PIONEX" if AUTO_TRADE_ENABLED else "🧪 PAPER TRADING (Simulazione)"

    return (
        f"🤖 <b>MULTI-COIN NEURAL QUANT GRID BOT</b>\n"
        f"🕐 {now_rome} (ora italiana) | Modalità: <b>{mode_str}</b>\n\n"
        f"{verdict}\n"
        f"👑 <b>Migliore Scelta AI: {best_a['coin']}/USDT ({best_a['name']})</b>\n"
        f"📊 <b>Voto Finale: {final_score}/10</b> (Score Quant: {best_a['pts_100']:.0f}/100)\n"
        f"{score_bar(final_score)}\n\n"
        f"⚙️ <b>STATO GESTIONE AUTONOMA</b>\n"
        f"{auto_section}\n\n"
        f"{rank_section}\n\n"
        f"{news_section}\n\n"
        f"⚙️ <b>PARAMETRI PIONEX PER 👑 {best_a['coin']}/USDT ({INVEST_EUR}€ ≈ {best_a['invest_usdt']:.2f} USDT)</b>\n"
        f"• Prezzo Attuale {best_a['coin']}: <b>${fmt_p(best_a['price'])}</b> (VWAP 24h: ${fmt_p(best_a['vwap_24h'])})\n"
        f"• Lower Price: <b>${fmt_p(best_a['lower'])}</b>\n"
        f"• Upper Price: <b>${fmt_p(best_a['upper'])}</b>\n"
        f"• Griglie: <b>{best_a['grids']}</b> (Arithmetic) — <i>~{best_a['usdt_per_grid']:.2f} USDT/griglia</i>\n"
        f"• Profitto netto/griglia (post-fee): <b>+{best_a['net_step_pct']:.2f}%</b>\n"
        f"• Stop Loss consigliato: <b>${fmt_p(best_a['stop_loss'])}</b>\n\n"
        f"📋 <b>PAGELLA 9 PILASTRI ({best_a['coin']}/USDT)</b>\n"
        f"{breakdown_txt}"
        f"{veto_txt}"
    )


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=20,
        )
        if r.status_code != 200:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=20)
    except Exception as e:
        print("Errore invio Telegram:", e)


def send_telegram_webapp(final_score=0, best_a=None):
    """Invia il file interattivo index.html direttamente nella chat di Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not os.path.exists("index.html"):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    state = (best_a or {}).get("auto_state", {})
    win_coin = (best_a or {}).get("coin", "SOL")
    if state.get("active"):
        stato_str = f"🟢 TRADE ATTIVO SU {state.get('coin', win_coin)}/USDT"
    else:
        stato_str = f"⏳ IN ATTESA (Top AI: {win_coin} {final_score}/10)"
    caption = (
        f"🖥️ Dashboard Multi-Coin AI | {stato_str}\n"
        f"👆 Tocca/clicca sul file qui sopra per aprire la WebApp con la classifica delle 10 Cripto!"
    )
    try:
        with open("index.html", "rb") as f:
            files = {"document": ("Dashboard_AI_Bot.html", f, "text/html")}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            requests.post(url, data=data, files=files, timeout=25)
    except Exception as e:
        print("Errore invio WebApp Telegram:", e)


def main():
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    try:
        news = fetch_macro_calendar()
    except Exception as e:
        news = {
            "status": "NO_DATA",
            "events": [],
            "imminent": [],
            "recent": [],
            "today_events": [],
            "next_event": None,
            "surprise_detected": False,
            "surprise_details": [],
            "us_open_warning": False,
            "now_rome": now_rome,
            "error": str(e),
        }

    final_score = 0
    best_analysis = None
    try:
        best_analysis = scan_all_coins(news)
        final_score = execute_autonomous_cycle(best_analysis, news)
        generate_webapp_dashboard(best_analysis, news, final_score)
        msg = format_message(best_analysis, news)
    except Exception as e:
        msg = (
            f"⚠️ <b>MULTI-COIN AI PIONEX BOT</b>\n"
            f"🕐 {now_rome.strftime('%d/%m/%Y %H:%M')} (ora italiana)\n\n"
            f"Errore analisi ({type(e).__name__}): {html.escape(str(e))}"
        )

    send_telegram(msg)
    if best_analysis is not None:
        send_telegram_webapp(final_score, best_analysis)


if __name__ == "__main__":
    main()
